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

from datetime import datetime
from openerp.tools import DEFAULT_SERVER_DATE_FORMAT
from openerp import fields, models, api, _, exceptions
from openerp.tools.float_utils import float_compare


class PurchaseOrderJustInTime(models.Model):
    _inherit = 'purchase.order'

    order_line = fields.One2many(states={'done': [('readonly', True)]})

    @api.multi
    def _create_stock_moves_improved(self, order, order_lines, group_id=False, picking_id=False):

        """
        Replaces the function _create_stock_moves of class purchase.order when executing function create (see lower).
        :param order: purchase.order
        :param order_lines: recordset of purchase.order.lines
        :param group_id: id of procurement.group
        :param picking_id: id of stock.picking
        """

        self.ensure_one()
        todo_moves = self.env['stock.move']
        if not group_id:
            group = self.env['procurement.group'].search([('name', '=', order.name),
                                                          ('partner_id', '=', order.partner_id.id)], limit=1)
            if not group:
                group = self.env['procurement.group'].create({'name': order.name, 'partner_id': order.partner_id.id})
            group_id = group.id

        for order_line in order_lines:
            if not order_line.product_id:
                continue

            if order_line.product_id.type in ('product', 'consu'):
                for vals in self._prepare_order_line_move(order, order_line, picking_id, group_id):
                    move = self.env['stock.move'].create(vals)
                    todo_moves |= move
        todo_moves.action_confirm()
        todo_moves.force_assign()

    @api.model
    def _prepare_order_line_move(self, order, order_line, picking_id, group_id):
        res = super(PurchaseOrderJustInTime, self)._prepare_order_line_move(order, order_line, picking_id, group_id)
        to_remove = []
        remaining_qty = order_line.product_qty
        for r in res:
            final_uom_qty = r['product_uom_qty']
            line = self.env['purchase.order.line'].browse(r['purchase_line_id'])
            qty_needed = line.product_qty - sum([x.product_uom_qty for x in line.move_ids
                                                 if x.procurement_id and x.state not in ['done', 'cancel']])
            if r.get('procurement_id'):
                procurement = self.env['procurement.order'].browse(r['procurement_id'])
                qty_ordered = sum([x.product_uom_qty for x in procurement.move_ids if x.state != 'cancel'])
                if procurement.state == 'cancel' or qty_ordered == procurement.product_qty:
                    to_remove += [r]
                else:
                    final_uom_qty = procurement.product_qty - qty_ordered
            else:
                qty_received = sum([x.product_uom_qty for x in line.move_ids if x.state == 'done'])
                if qty_received == qty_needed:
                    to_remove += [r]
                else:
                    final_uom_qty = qty_needed - qty_received
            r['product_uom_qty'] = min(final_uom_qty, remaining_qty)
            r['product_uos_qty'] = min(final_uom_qty, remaining_qty) * order_line.product_id.uos_coeff
            remaining_qty -= min(final_uom_qty, remaining_qty)
        for r in to_remove:
            res.remove(r)
        return res


class PurchaseOrderLineJustInTime(models.Model):
    _inherit = 'purchase.order.line'
    _order = 'date_planned, order_id, id'

    supplier_code = fields.Char(string="Supplier Code", compute='_compute_supplier_code')
    ack_ref = fields.Char("Acknowledge Reference", help="Reference of the supplier's last reply to confirm the delivery"
                                                        " at the planned date")
    date_ack = fields.Date("Last Acknowledge Date",
                           helps="Last date at which the supplier confirmed the delivery at the planned date.")
    opmsg_type = fields.Selection([('no_msg', "Ok"), ('late', "LATE"), ('early', "EARLY"), ('reduce', "REDUCE"),
                                   ('to_cancel', "CANCEL")], compute="_compute_opmsg", string="Message Type")
    opmsg_delay = fields.Integer("Message Delay", compute="_compute_opmsg")
    opmsg_reduce_qty = fields.Float("New target quantity after procurement cancellation", readonly=True, default=False)
    opmsg_text = fields.Char("Operational message", compute="_compute_opmsg_text", help="This field holds the "
                                                                                        "operational messages generated by the system to the operator")
    remaining_qty = fields.Float("Remaining quantity", help="Quantity not yet delivered by the supplier",
                                 compute="_get_remaining_qty", store=True)
    to_delete = fields.Boolean('True if all the procurements of the purchase order line are canceled')
    father_line_id = fields.Many2one('purchase.order.line', string="Very first line splited", readonly=True)
    children_line_ids = fields.One2many('purchase.order.line', 'father_line_id', string="Children lines")
    children_number = fields.Integer(string="Number of children", readonly=True, compute='_compute_children_number')

    @api.depends('date_planned', 'date_required', 'to_delete', 'product_qty', 'opmsg_reduce_qty')
    def _compute_opmsg(self):

        """
        Sets parameters date_planned, date_required, and opmsg_type.
        """

        for rec in self:
            if rec.date_planned and rec.date_required:
                date_planned = datetime.strptime(rec.date_planned, DEFAULT_SERVER_DATE_FORMAT)
                date_required = datetime.strptime(rec.date_required, DEFAULT_SERVER_DATE_FORMAT)
                min_late_days = int(self.env['ir.config_parameter'].get_param(
                    "purchase_procurement_just_in_time.opmsg_min_late_delay"))
                min_early_days = int(self.env['ir.config_parameter'].get_param(
                    "purchase_procurement_just_in_time.opmsg_min_early_delay"))
                if date_planned >= date_required:
                    delta = date_planned - date_required
                    if delta.days >= min_late_days:
                        rec.opmsg_type = 'late'
                        rec.opmsg_delay = delta.days
                else:
                    delta = date_required - date_planned
                    if delta.days >= min_early_days:
                        rec.opmsg_type = 'early'
                        rec.opmsg_delay = delta.days
            if rec.to_delete and rec.product_qty != 0:
                rec.opmsg_type = 'to_cancel'
            if not rec.to_delete and rec.opmsg_reduce_qty and rec.opmsg_reduce_qty < rec.product_qty:
                rec.opmsg_type = 'reduce'

    @api.depends('opmsg_type', 'opmsg_delay', 'opmsg_reduce_qty', 'product_qty', 'to_delete', 'state')
    def _compute_opmsg_text(self):

        """
        Sets parameters opmsg_type, opmsg_delay, opmsg_reduce_qty, product_qty, to_delete and state.
        """

        for rec in self:
            msg = ""
            if rec.to_delete and rec.product_qty != 0:
                msg += _("CANCEL LINE")
            if not rec.to_delete and rec.opmsg_reduce_qty and rec.opmsg_reduce_qty < rec.product_qty:
                msg += _("REDUCE QTY to %.1f %s ") % (rec.opmsg_reduce_qty, rec.product_uom.name)
            if rec.opmsg_type == 'early':
                msg += _("EARLY by %i day(s)") % rec.opmsg_delay
            elif rec.opmsg_type == 'late':
                msg += _("LATE by %i day(s)") % rec.opmsg_delay
            rec.opmsg_text = msg

    @api.multi
    @api.depends('partner_id', 'product_id')
    def _compute_supplier_code(self):
        for rec in self:
            list_supinfos = self.env['product.supplierinfo'].search(
                [('product_tmpl_id', '=', rec.product_id.product_tmpl_id.id), ('name', '=', rec.partner_id.id)]
            )
            if list_supinfos:
                rec.supplier_code = list_supinfos[0].product_code

    @api.depends('product_qty', 'move_ids', 'move_ids.product_uom_qty', 'move_ids.product_uom', 'move_ids.state')
    def _get_remaining_qty(self):

        """
        Calculates ramaining_qty
        """

        for rec in self:
            do_calculation = True
            for move in rec.move_ids:
                if move.product_uom != rec.product_uom:
                    do_calculation = False
                    break
            if do_calculation:
                delivered_qty = sum([move.product_uom_qty for move in rec.move_ids if move.state == 'done'])
                rec.remaining_qty = rec.product_qty - delivered_qty

    @api.depends('children_line_ids')
    def _compute_children_number(self):

        """
        Calculates children_number
        """
        for rec in self:
            rec.children_number = len(rec.children_line_ids)

    @api.multi
    def open_form_purchase_order_line(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'purchase.order.line',
            'name': _("Purchase Order Line: %s") % self.line_no,
            'views': [(False, "form")],
            'res_id': self.id,
            'context': {}
        }

    @api.model
    def create(self, vals):

        """
        Improves original function create. Sets automatically a line number and creates appropriate moves in a picking.
        :return: the same result as original create function
        """

        result = super(PurchaseOrderLineJustInTime, self).create(vals)
        if result.order_id.state not in ['draft', 'sent', 'bid', 'confirmed', 'done', 'cancel']:
            result.order_id.set_order_line_status('confirmed')
            if result.product_qty != 0 and not result.move_ids and not self.env.context.get('no_update_moves'):
                # We create associated moves
                order_lines = result.order_id.order_line.filtered(lambda l: l != result)
                running_moves_with_group = order_lines.mapped(lambda l: l.move_ids).filtered(
                    lambda m: m.state not in ['done', 'cancel'] and m.group_id
                )
                group_id = running_moves_with_group and running_moves_with_group[0].group_id.id or False
                result.order_id._create_stock_moves_improved(result.order_id, result, group_id=group_id)
        return result

    @api.multi
    def change_qty_or_create(self, qty, global_qty_ordered):

        """
        Used to increase quantity of a purchase order line : see comments in function update_moves.
        """

        diff = qty - global_qty_ordered
        move_without_proc_id = [x for x in self.move_ids if not x.procurement_id]
        move_with_proc_id = [x for x in self.move_ids if x.procurement_id]
        if len(move_without_proc_id + move_with_proc_id) != 0:
            if len(move_without_proc_id) == 0:
                new_move = move_with_proc_id[0].copy({'product_uom_qty': diff, 'procurement_id': False,
                                                      'purchase_line_id': self.id})
                new_move.purchase_line_id = self.id
                new_move.action_confirm()
                new_move.action_assign()
            else:
                move = move_without_proc_id[0]
                if move.state not in ['done', 'cancel']:
                    move.product_uom_qty = move.product_uom_qty + diff
                else:
                    new_move = move.copy({'state': 'draft', 'product_uom_qty': diff})
                    new_move.purchase_line_id = move.purchase_line_id
                    new_move.action_confirm()
                    new_move.action_assign()
        else:
            self.order_id._create_stock_moves_improved(self.order_id, self)

    @api.multi
    def delete_cancel_create(self, qty):

        """
        Used to decrease quantity of a purchase order line : see comments in function update_moves.
        """

        moves_without_proc_id = self.move_ids.filtered(lambda m: m.state != 'done' and not
        m.procurement_id).sorted(key=lambda m: m.product_qty, reverse=True)
        moves_with_proc_id = self.move_ids.filtered(lambda m: m.state != 'done' and m.procurement_id). \
            sorted(key=lambda m: m.product_qty, reverse=True)
        move_list = moves_without_proc_id + moves_with_proc_id
        if len(moves_without_proc_id + moves_with_proc_id) != 0:
            _sum = sum([x.product_uom_qty for x in self.move_ids if x.state != 'cancel'])
            while _sum > qty:
                if len(move_list) > 0:
                    if move_list[0].product_uom_qty > _sum - qty:
                        move_list[0].product_uom_qty -= _sum - qty
                        if move_list[0].procurement_id:
                            _sum = qty
                        else:
                            _sum -= _sum - qty
                    else:
                        if move_list[0].procurement_id:
                            _sum -= move_list[0].product_uom_qty
                            move_list[0].product_uom_qty = 0.0
                        else:
                            move_list[0].with_context({'cancel_procurement': True}).action_cancel()
                            _sum -= move_list[0].product_uom_qty
                        move_list -= move_list[0]
            if _sum < qty:
                diff = qty - _sum
                if not moves_without_proc_id:
                    new_move = moves_with_proc_id[0].copy({'product_uom_qty': diff, 'procurement_id': False,
                                                           'purchase_line_id': self.id})
                    new_move.action_confirm()
                    new_move.action_assign()
                else:
                    moves_without_proc_id[0] += diff

    @api.multi
    def update_moves(self, vals):

        """
        Updates moves associated to a purchase order line, based on the procurement order associated to this line.
        When increasing quantity: if any move is linked with no procurement, we increase its quantity. If not, we
        create a new move.
        When decreasing a quantity, we progressively delete the moves linked with no procurements, then cancel the other
        ones until the global quantity ordered is lower or equal to the new quantity. Then, if it is lower, we create a
        new move as before, to reach the appropriate quantity.
        """

        self.ensure_one()
        if vals['product_qty'] < sum([x.product_uom_qty for x in self.move_ids if x.state == 'done']):
            raise exceptions.except_orm(_('Error!'), _("Impossible to cancel moves at state done."))
        global_qty_ordered = sum(x.product_uom_qty for x in self.move_ids if x.state != 'cancel')
        if self.state == 'confirmed':
            if float_compare(vals['product_qty'], 0.0, precision_rounding=self.product_id.uom_id.rounding) == 0:
                for move in self.move_ids:
                    move.with_context({'cancel_procurement': True}).action_cancel()
            if vals['product_qty'] > 0:
                if vals.get('product_qty') > global_qty_ordered:
                    self.change_qty_or_create(vals.get('product_qty'), global_qty_ordered)
                if vals.get('product_qty') < global_qty_ordered:
                    self.delete_cancel_create(vals.get('product_qty'))
            move_to_remove = [x for x in self.move_ids if x.state == 'cancel']
            for move in move_to_remove:
                move.purchase_line_id = False
            order = self.order_id
            if len(self.move_ids) == 0:
                self.action_cancel()
            if len(order.order_line) == 0:
                order.action_cancel()

    @api.multi
    def write(self, vals):

        """
        Improves the original write function. Allows to update moves of a purchase order line even if it is confirmed.
        :return: the same result as original write function
        """

        result = super(PurchaseOrderLineJustInTime, self).write(vals)
        for rec in self:
            if 'product_qty' in vals.keys() and not self.env.context.get('no_update_moves'):
                rec.update_moves(vals)
        if vals.get('price_unit'):
            for rec in self:
                active_moves = self.env['stock.move'].search([('product_id', '=', rec.product_id.id),
                                                              ('purchase_line_id', '=', rec.id),
                                                              ('state', 'not in', ['draft', 'cancel', 'done'])])
                active_moves.write({'price_unit': vals['price_unit']})
        return result

    @api.multi
    def act_windows_view_graph(self):
        self.ensure_one()
        warehouses = self.env['stock.warehouse'].search([('company_id', '=', self.env.user.company_id.id)])
        if warehouses:
            wid = warehouses[0].id
        else:
            raise exceptions.except_orm(_("Error!"), _("Your company does not have a warehouse"))

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'stock.levels.report',
            'name': _("Stock Evolution"),
            'view_type': 'form',
            'view_mode': 'graph,tree',
            'context': {'search_default_warehouse_id': wid,
                        'search_default_product_id': self.product_id.id}
        }

    @api.multi
    def unlink(self):
        orders_to_unlink = self.env['purchase.order'].search([('id', 'in', [line.order_id.id for line in self])])
        result = super(PurchaseOrderLineJustInTime, self).unlink()
        for order in orders_to_unlink:
            if not order.order_line:
                order.unlink()
        return result