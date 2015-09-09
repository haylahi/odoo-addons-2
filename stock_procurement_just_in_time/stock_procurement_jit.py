# -*- coding: utf8 -*-
#
# Copyright (C) 2014 NDP Systèmes (<http://www.ndp-systemes.fr>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from dateutil.relativedelta import relativedelta
import logging
import openerp.addons.decimal_precision as dp
from openerp.addons.connector.session import ConnectorSession, ConnectorSessionHandler
from openerp.addons.connector.queue.job import job
from openerp.tools import float_compare, float_round
from openerp.tools.sql import drop_view_if_exists
from openerp import fields, models, api

ORDERPOINT_CHUNK = 50

_logger = logging.getLogger(__name__)


@job
def process_orderpoints(session, model_name, ids):
    """Processes the given orderpoints."""
    _logger.info("<<Started chunk of %s orderpoints to process" % ORDERPOINT_CHUNK)
    handler = ConnectorSessionHandler(session.cr.dbname, session.uid, session.context)
    with handler.session() as s:
        for op in s.env[model_name].browse(ids):
            op.process()
        s.cr.commit()


class StockLocation(models.Model):
    _inherit = 'stock.location'

    top_parent_location_id = fields.Many2one('stock.location', string="Top Parent Location",
                                             compute='_compute_top_parent_location_id', store=True)

    @api.depends('location_id', 'usage')
    def _compute_top_parent_location_id(self):
        for rec in self:
            top_parent_location = rec
            while top_parent_location.location_id and top_parent_location.location_id.usage == 'internal':
                top_parent_location = top_parent_location.location_id
            rec.top_parent_location_id = top_parent_location


class ProcurementOrderQuantity(models.Model):
    _inherit = 'procurement.order'

    qty = fields.Float(string="Quantity", digits_compute=dp.get_precision('Product Unit of Measure'),
                       help='Quantity in the default UoM of the product', compute="_compute_qty", store=True)

    @api.multi
    @api.depends('product_qty', 'product_uom')
    def _compute_qty(self):
        uom_obj = self.env['product.uom']
        for m in self:
            qty = uom_obj._compute_qty_obj(m.product_uom, m.product_qty, m.product_id.uom_id)
            m.qty = qty

    @api.multi
    def reschedule_for_need(self, need):
        """Reschedule procurements to a given need.
        Will set the date of the procurements one second before the date of the need.
        :param need: dict
        """
        for proc in self:
            new_date = fields.Datetime.from_string(need['date']) + relativedelta(seconds=-1)
            proc.date_planned = fields.Datetime.to_string(new_date)
            _logger.debug("Rescheduled proc: %s, new date: %s" % (proc, proc.date_planned))
        self.with_context(reschedule_planned_date=True).action_reschedule()

    @api.model
    def _procure_orderpoint_confirm(self, use_new_cursor=False, company_id=False):
        """
        Create procurement based on Orderpoint

        :param bool use_new_cursor: if set, use a dedicated cursor and auto-commit after processing each procurement.
            This is appropriate for batch jobs only.
        """
        orderpoint_env = self.env['stock.warehouse.orderpoint']
        dom = company_id and [('company_id', '=', company_id)] or []
        if self.env.context.get('compute_product_ids') and not self.env.context.get('compute_all_products'):
            dom += [('product_id', 'in', self.env.context.get('compute_product_ids'))]
        orderpoint_ids = orderpoint_env.search(dom)
        while orderpoint_ids:
            orderpoints = orderpoint_ids[:ORDERPOINT_CHUNK]
            orderpoint_ids = orderpoint_ids - orderpoints
            process_orderpoints.delay(ConnectorSession.from_env(self.env), 'stock.warehouse.orderpoint',
                                      orderpoints.ids, description="Computing orderpoints %s" % orderpoints.ids)
        return {}


class StockWarehouseOrderPointJit(models.Model):
    _inherit = 'stock.warehouse.orderpoint'

    @api.multi
    @api.returns('procurement.order')
    def get_next_proc(self, need):
        """Returns the next procurement.order after this line which date is not the line's date."""
        self.ensure_one()
        next_line = self.compute_stock_levels_requirements(product_id=self.product_id.id,
                                                           location_id=self.location_id.id,
                                                           list_move_types=('existing', 'in', 'out', 'planned',),
                                                           limit=False, parameter_to_sort='date', to_reverse=False)
        next_line = [x for x in next_line if x.get('date') and x['date'] > need['date'] and x['proc_id']]
        if next_line:
            return self.env['procurement.order'].search([('id', '=', next_line[0]['proc_id'])])
        return self.env['procurement.order']

    @api.multi
    def get_next_need(self):
        """Returns a dict of stock level requirements where the stock level is below minimum qty for the product and
        the location of the orderpoint."""
        self.ensure_one()
        need = self.compute_stock_levels_requirements(product_id=self.product_id.id, location_id=self.location_id.id,
                                                      list_move_types=('out',), limit=False, parameter_to_sort='date',
                                                      to_reverse=False)
        need = [x for x in need if x['qty'] < self.product_min_qty]
        if need:
            need = need[0]
            if need.get('id') or need.get('proc_id') or need.get('product_id') or need.get('location_id') or \
                    need.get('move_type') or need.get('qty') or need.get('date') or need.get('move_qty'):
                return need
        return False

    @api.multi
    def redistribute_procurements(self, date_start, date_end, days=1):
        """Redistribute procurements related to these orderpoints between date_start and date_end.
        Procurements will be considered as over-supplying if the quantity in stock calculated 'days' after the
        procurement is above the orderpoint calculated maximum quantity. This allows not to consider movements of
        large quantities over a small period of time (that can lead to ponctual over stock) as being over supply.

        This function works by taking procurements one by one from the right. For each it checks whether the quantity
        in stock days after this procurement is above the max value. If it is, the procurement is rescheduled
        temporarily to date_end. This way, we check at which date between the procurement's original date and its
        current date the stock level falls below the minimum quantity and finally place the procurement at this date.

        :param date_start: the starting date as datetime. If False, start at the earliest available date.
        :param date_end: the ending date as datetime
        :param days: defines the number of days after a procurement at which to consider stock quantity.
        """
        for op in self:
            date_domain = [('date_planned', '<', fields.Datetime.to_string(date_end))]
            if date_start:
                date_domain += [('date_planned', '>=', fields.Datetime.to_string(date_start))]
            procs = self.env['procurement.order'].search([('product_id', '=', op.product_id.id),
                                                          ('location_id', '=', op.location_id.id),
                                                          ('state', 'in', ['confirmed', 'running'])]
                                                         + date_domain,
                                                         order="date_planned DESC")
            for proc in procs:
                stock_date = min(
                    fields.Datetime.from_string(proc.date_planned) + relativedelta(days=days),
                    date_end)
                stock_level = self.env['stock.warehouse.orderpoint'].compute_stock_levels_requirements(
                                                        product_id=proc.product_id.id, location_id=proc.location_id.id,
                                                        list_move_types=('in', 'out', 'existing', 'planned',),
                                                        parameter_to_sort='date', to_reverse=True, limit=False)
                stock_level = [x for x in stock_level if x.get('date') and x['date'] <
                               fields.Datetime.to_string(stock_date)]
                if stock_level and stock_level[0]['qty'] > op.get_max_qty(stock_date):
                    # We have too much of products: so we reschedule the procurement at end date
                    proc.date_planned = fields.Datetime.to_string(date_end + relativedelta(seconds=-1))
                    proc.with_context(reschedule_planned_date=True).action_reschedule()
                    # Then we reschedule back to the next need if any
                    need = op.get_next_need()
                    if need and fields.Datetime.from_string(need['date']) < date_end:
                        # Our rescheduling ended in creating a need before our procurement, so we move it to this date
                        proc.reschedule_for_need(need)
                    _logger.debug("Rescheduled procurement %s, new date: %s" % (proc, proc.date_planned))

    @api.multi
    def create_from_need(self, need):
        """Creates a procurement to fulfill the given need with the data calculated from the given order point.
        Will set the date of the procurement one second before the date of the need.

        :param need: the 'stock levels requirements' dictionary to fulfill
        :param orderpoint: the 'stock.orderpoint' record set with the needed date
        """
        proc_obj = self.env['procurement.order']
        for orderpoint in self:
            qty = max(orderpoint.product_min_qty,
                      orderpoint.get_max_qty(fields.Datetime.from_string(need['date']))) - need['qty']
            reste = orderpoint.qty_multiple > 0 and qty % orderpoint.qty_multiple or 0.0
            if float_compare(reste, 0.0, precision_rounding=orderpoint.product_uom.rounding) > 0:
                qty += orderpoint.qty_multiple - reste
            qty = float_round(qty, precision_rounding=orderpoint.product_uom.rounding)

            proc_vals = proc_obj._prepare_orderpoint_procurement(orderpoint, qty)
            proc_date = fields.Datetime.from_string(need['date']) + relativedelta(seconds=-1)
            proc_vals.update({
                'date_planned': fields.Datetime.to_string(proc_date)
            })
            proc = proc_obj.create(proc_vals)
            proc.run()
            _logger.debug("Created proc: %s, (%s, %s). Product: %s, Location: %s" %
                        (proc, proc.date_planned, proc.product_qty, orderpoint.product_id, orderpoint.location_id))

    @api.multi
    def get_last_scheduled_date(self):
        """Returns the last scheduled date for this order point."""
        self.ensure_one()
        last_schedule = self.env['stock.warehouse.orderpoint'].compute_stock_levels_requirements(
                                                                product_id=self.product_id.id,
                                                                location_id=self.location_id.id,
                                                                list_move_types=['in', 'out', 'existing'], limit=1,
                                                                parameter_to_sort='date', to_reverse=True)
        res = last_schedule and last_schedule[0].get('date') and \
              fields.Datetime.from_string(last_schedule[0].get('date')) or False
        return res

    @api.multi
    def remove_unecessary_procurements(self, timestamp):
        """Remove the unecessary procurements that are placed just before timestamp, and recreate one if necessary to
        match exactly this order point product_min_qty.

        :param timestamp: datetime object
        """
        for orderpoint in self:
            last_outgoing = self.env['stock.warehouse.orderpoint'].compute_stock_levels_requirements(
                                                                product_id=orderpoint.product_id.id,
                                                                location_id=orderpoint.location_id.id,
                                                                list_move_types=('out',), limit=1,
                                                                parameter_to_sort='date', to_reverse=True)
            last_outgoing = [x for x in last_outgoing if x['date'] <= fields.Datetime.to_string(timestamp)]
            # We get all procurements placed before timestamp, but after the last outgoing line sorted by inv quantity
            procs = self.env['procurement.order'].search([('product_id', '=', orderpoint.product_id.id),
                                                          ('location_id', '=', orderpoint.location_id.id),
                                                          ('state', 'not in', ['done', 'cancel']),
                                                          ('date_planned', '<=', fields.Datetime.to_string(timestamp))],
                                                         order='qty DESC')
            if last_outgoing:
                procs = procs.filtered(lambda x: x.date_planned > last_outgoing[0]['date'])
            _logger.debug("Removing not needed procurements: %s", procs.ids)
            procs.cancel()
            procs.unlink()

    @api.multi
    def process(self):
        """Process this orderpoint."""
        for op in self:
            _logger.debug("Computing orderpoint %s (%s, %s)" % (op.id, op.product_id.name, op.location_id.name))
            need = op.get_next_need()
            date_cursor = False
            while need:
                op.redistribute_procurements(date_cursor, fields.Datetime.from_string(need['date']), days=1)
                # We move the date_cursor to the need date
                date_cursor = fields.Datetime.from_string(need['date'])
                # We check if there is already a procurement in the future
                next_proc = op.get_next_proc(need)
                if next_proc:
                    # If there is a future procurement, we reschedule it (required date) to fit our need
                    next_proc.reschedule_for_need(need)
                else:
                    # Else, we create a new procurement
                    op.create_from_need(need)
                need = op.get_next_need()
            # Now we want to make sure that at the end of the scheduled outgoing moves, the stock level is
            # the minimum quantity of the orderpoint.
            last_scheduled_date = op.get_last_scheduled_date()
            if last_scheduled_date:
                date_end = last_scheduled_date + relativedelta(minutes=+1)
                op.redistribute_procurements(date_cursor, date_end)
                op.remove_unecessary_procurements(date_end)

    @api.model
    def compute_stock_levels_requirements(self, product_id, location_id, list_move_types, limit=1,
                                          parameter_to_sort='date', to_reverse=False):

        """
        Computes stock level report
        :param product_id: int
        :param location_id: int
        :param list_move_types: tuple or list of strings (move types)
        :param limit: maximum number of lines in the result
        :param parameter_to_sort: str
        :param to_reverse: bool
        :return: list of need dictionaries
        """

        # Computing the top parent location
        min_date = False
        location = self.env['stock.location'].search([('id', '=', location_id)])
        location_id = location.top_parent_location_id.id
        result = []
        intermediate_result = []
        stock_move_restricted_in = self.env['stock.move'].search([('product_id', '=', product_id),
                                                                  ('state', 'not in', ['cancel', 'done', 'draft']),
                                                                  ('location_dest_id', 'child_of', location_id)],
                                                                 order='date')
        stock_move_restricted_out = self.env['stock.move'].search([('product_id', '=', product_id),
                                                                   ('state', 'not in', ['cancel', 'done', 'draft']),
                                                                   ('location_id', 'child_of', location_id)],
                                                                  order='date')
        stock_quant_restricted = self.env['stock.quant'].search([('product_id', '=', product_id),
                                                                 ('location_id', '=', location_id)])
        procurement_order_restricted = self.env['procurement.order'].search([('product_id', '=', product_id),
                                                                             ('location_id', '=', location_id)],
                                                                            order='date_planned')
        dates = []
        if stock_move_restricted_in:
            dates += [stock_move_restricted_in[0].date]
        if stock_move_restricted_out:
            dates += [stock_move_restricted_out[0].date]
        procurement_order_restricted2 = [x for x in procurement_order_restricted if
                                         [y for y in x.move_ids if not y.id or y.state == 'draft']]
        if procurement_order_restricted2:
            dates += [min([x.date for x in procurement_order_restricted2])]
        if dates:
            min_date = min(dates)

        # existing items
        existing_qty = 0
        computed_parent_locations = []
        list_sq = stock_quant_restricted
        while list_sq:
            top_parent_location = list_sq[0].location_id.top_parent_location_id
            list_sq_in_top_location = list_sq.filtered(lambda sq: sq.location_id.top_parent_id == top_parent_location)
            existing_qty = sum([x.qty for x in list_sq_in_top_location])
            procurement_order_ordered = procurement_order_restricted.search([('state', 'not in', ['cancel', 'done'])],
                                                                                                order='date_planned').\
                                filtered(lambda po: bool([m for m in po.move_ids if not m.id or m.state == 'draft']))
            date = procurement_order_ordered[0].date_planned
            if min_date:
                date = min(date, min_date)
            intermediate_result += [{
                    'proc_id': False,
                    'location_id': top_parent_location.id,
                    'move_type': 'existing',
                    'date': date,
                    'qty': existing_qty,
                    'move_id': False,
                }]
            computed_parent_locations += [top_parent_location]
            list_sq -= list_sq_in_top_location

        # incoming items
        for sm in stock_move_restricted_in:
            if sm.location_dest_id.usage in ['internal', 'transit']:
                top_parent_location = sm.location_dest_id.top_parent_location_id
                procurement = sm.procurement_id
                date = False
                if procurement and procurement.date_planned:
                    date = procurement.date_planned
                elif sm.date:
                    date = sm.date
                intermediate_result += [{
                        'proc_id': procurement.id,
                        'location_id': top_parent_location.id,
                        'move_type': 'in',
                        'date': date,
                        'qty': sm.product_qty,
                        'move_id': sm.id,
                    }]

        # outgoing items
        for sm in stock_move_restricted_out:
            if sm.location_id.usage in ['internal', 'transit']:
                top_parent_location = sm.location_id.top_parent_location_id
                procurement = sm.procurement_id
                date = False
                if procurement and procurement.date_planned:
                    date = procurement.date_planned
                elif sm.date:
                    date = sm.date
                intermediate_result += [{
                        'proc_id': False,
                        'location_id': location_id,
                        'move_type': 'out',
                        'date': date,
                        'qty': - sm.product_qty,
                        'move_id': sm.id,
                    }]

        # planned items
        procurement_order_restricted = procurement_order_restricted.search([('state', 'not in', ['cancel', 'done'])]).\
            filtered(lambda p: not p.move_ids or bool([m for m in p.move_ids if not m.id or m.state == 'draft']))
        for po in procurement_order_restricted:
            if po.location_id.usage in ['internal', 'transit']:
                intermediate_result += [{
                        'proc_id': po.id,
                        'location_id': po.location_id.id,
                        'move_type': 'planned',
                        'date': po.date_planned,
                        'qty': po.qty,
                        'move_id': False,
                    }]

        intermediate_result = sorted(intermediate_result, key=lambda a: a['date'])
        qty = existing_qty
        for dictionary in intermediate_result:
            if dictionary['location_id'] == location_id:
                id = str(product_id) + '-' + str(dictionary['location_id']) + '-'
                if dictionary['move_id']:
                    id += str(dictionary['move_id'])
                elif dictionary['proc_id']:
                    id += str(dictionary['proc_id'])
                else:
                    id += 'existing'
                if dictionary['move_type'] != 'existing':
                    qty += dictionary['qty']
                result += [{
                    'id': id,
                    'proc_id': dictionary['proc_id'],
                    'product_id': product_id,
                    'location_id': dictionary['location_id'],
                    'move_type': dictionary['move_type'],
                    'date': dictionary['date'],
                    'qty': qty,
                    'move_qty': dictionary['qty'],
                }]

        result = sorted(result, key=lambda z: z[parameter_to_sort], reverse=to_reverse)
        result = [x for x in result if x['move_type'] in list_move_types]
        if limit:
            return result[:limit]
        else:
            return result


class StockComputeAll(models.TransientModel):
    _inherit = 'procurement.order.compute.all'

    def _get_default_product_ids(self):
        return self.env['product.product'].search([])

    compute_all = fields.Boolean(string=u"Traiter l'ensemble des produits", default=True)
    product_ids = fields.Many2many('product.product', string=u"Produits à traiter", default=_get_default_product_ids)

    @api.multi
    def procure_calculation(self):
        return super(StockComputeAll, self.with_context(compute_product_ids=self.product_ids.ids,
                                                        compute_all_products=self.compute_all)).procure_calculation()


class StockLevelsReport(models.Model):
    _name = "stock.levels.report"
    _description = "Stock Levels Report"
    _order = "date"
    _auto = False

    id = fields.Integer("ID", readonly=True)
    product_id = fields.Many2one("product.product", string="Product", index=True)
    product_categ_id = fields.Many2one("product.category", string="Product Category")
    location_id = fields.Many2one("stock.location", string="Location", index=True)
    other_location_id = fields.Many2one("stock.location", string="Origin/Destination")
    move_type = fields.Selection([('existing', 'Existing'), ('in', 'Incoming'), ('out', 'Outcoming')],
                                 string="Move Type", index=True)
    date = fields.Datetime("Date", index=True)
    qty = fields.Float("Stock Quantity", group_operator="last")
    move_qty = fields.Float("Moved Quantity")

    def init(self, cr):
        drop_view_if_exists(cr, "stock_levels_report")
        cr.execute("""
        create or replace view stock_levels_report as (
            with recursive top_parent(id, top_parent_id) as (
                    select
                        sl.id, sl.id as top_parent_id
                    from
                        stock_location sl
                        left join stock_location slp on sl.location_id = slp.id
                    where
                        sl.usage='internal' and (sl.location_id is null or slp.usage<>'internal')
                union
                    select
                        sl.id, tp.top_parent_id
                    from
                        stock_location sl, top_parent tp
                    where
                        sl.usage='internal' and sl.location_id=tp.id
            )
            select
                foo.product_id::text || '-'
                    || foo.location_id::text || '-'
                    || coalesce(foo.move_id::text, 'existing') as id,
                foo.product_id,
                pt.categ_id as product_categ_id,
                tp.top_parent_id as location_id,
                foo.other_location_id,
                foo.move_type,
                sum(foo.qty) over (partition by foo.location_id, foo.product_id order by date) as qty,
                foo.date as date,
                foo.qty as move_qty
            from
                (
                    select
                        sq.product_id as product_id,
                        sq.location_id as location_id,
                        NULL as other_location_id,
                        'existing'::text as move_type,
                        max(sq.in_date) as date,
                        sum(sq.qty) as qty,
                        NULL as move_id
                    from
                        stock_quant sq
                        left join stock_location sl on sq.location_id = sl.id
                    where
                        sl.usage = 'internal'::text or sl.usage = 'transit'::text
                    group by sq.product_id, sq.location_id
                union all
                    select
                        sm.product_id as product_id,
                        sm.location_dest_id as location_id,
                        sm.location_id as other_location_id,
                        'in'::text as move_type,
                        sm.date_expected as date,
                        sm.product_qty as qty,
                        sm.id as move_id
                    from
                        stock_move sm
                        left join stock_location sl on sm.location_dest_id = sl.id
                    where
                        (sl.usage = 'internal'::text or sl.usage = 'transit'::text)
                        and sm.state::text <> 'cancel'::text
                        and sm.state::text <> 'done'::text
                        and sm.state::text <> 'draft'::text
                union all
                    select
                        sm.product_id as product_id,
                        sm.location_id as location_id,
                        sm.location_dest_id as other_location_id,
                        'out'::text as move_type,
                        sm.date_expected as date,
                        -sm.product_qty as qty,
                        sm.id as move_id
                    from
                        stock_move sm
                        left join stock_location sl on sm.location_id = sl.id
                    where
                        (sl.usage = 'internal'::text or sl.usage = 'transit'::text)
                        and sm.state::text <> 'cancel'::text
                        and sm.state::text <> 'done'::text
                        and sm.state::text <> 'draft'::text
                ) foo
                left join product_product pp on foo.product_id = pp.id
                left join product_template pt on pp.product_tmpl_id = pt.id
                left join top_parent tp on foo.location_id = tp.id
        )
        """)