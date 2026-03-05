# -*- coding: utf-8 -*-
import json
import logging
import re
import urllib.request
import urllib.error
from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """
Eres un experto en contabilidad guatemalteca y reconocimiento de documentos fiscales.
Analiza el documento adjunto y extrae la información en formato JSON EXCLUSIVAMENTE.

REGLAS:
1. Responde ÚNICAMENTE con JSON — sin texto previo, sin bloques markdown.
2. Si un campo no se puede determinar con certeza, usa null.
3. Para facturas FEL guatemaltecas extrae el UUID/número de autorización SAT.
4. Cantidades y precios deben ser números (float), no strings.
5. El NIT exactamente como aparece (con guión si lo tiene).
6. Cada línea de detalle por separado.

Devuelve este esquema JSON exacto:
{
  "vendor_name": "Nombre completo del proveedor",
  "vendor_nit": "NIT con guión",
  "vendor_address": "Dirección o null",
  "invoice_number": "Número de factura",
  "fel_uuid": "UUID SAT o null",
  "fel_serie": "Serie DTE o null",
  "invoice_date": "YYYY-MM-DD",
  "currency": "GTQ",
  "subtotal_before_tax": 0.00,
  "tax_amount": 0.00,
  "tax_rate_percent": 12,
  "total_amount": 0.00,
  "notes": "Observaciones o null",
  "lines": [
    {
      "description": "Descripción del producto",
      "product_code": "Código o null",
      "quantity": 1.0,
      "unit_of_measure": "unidad o null",
      "unit_price": 0.00,
      "line_total": 0.00
    }
  ]
}
"""

MATCHING_PROMPT_TEMPLATE = """Eres un experto en inventario para un restaurante guatemalteco (Kesiyos).
Haz coincidir líneas de una factura con productos del ERP usando razonamiento semántico.

CATÁLOGO ODOO (id | código | nombre | categoría | UOM compra):
{product_catalog}

LÍNEAS DE FACTURA:
{invoice_lines}

INSTRUCCIONES:
- Usa sinónimos, marcas, abreviaciones, equivalencias ES/EN.
- Criterios de confianza:
  "high"   (≥85): Auto-mapear sin revisión.
  "medium" (50-84): Sugerencia, requiere revisión.
  "low"    (<50): No mapear.
  "none"   (0): Sin similar en catálogo.
- Si confidence es "low" o "none": product_odoo_id = null.

Responde SOLO con el array JSON (sin texto, sin markdown):
[
  {{
    "line_index": 0,
    "invoice_description": "descripción original",
    "product_odoo_id": 42,
    "product_odoo_name": "Nombre en Odoo",
    "confidence": "high",
    "confidence_score": 92,
    "reason": "Razón en español",
    "suggested_new_product_name": null
  }}
]

IMPORTANTE: Si no hay match (low/none), llena "suggested_new_product_name" con el nombre
limpio ideal para crear el producto en Odoo (ej: "Aceite de Cocina Canola").
"""


class PurchaseAIProductWizard(models.TransientModel):
    """
    Mini-wizard that pops up when the user clicks 🆕 on a line.
    Asks for product type and category before creating.
    """
    _name = 'purchase.ai.product.wizard'
    _description = 'Crear Producto desde Factura IA'

    line_id = fields.Many2one(
        'purchase.ai.wizard.line', string='Línea', ondelete='cascade'
    )
    product_name = fields.Char(string='Nombre del Producto', required=True)
    product_type = fields.Selection([
        ('consu',   '📦 Consumible (sin rastreo de stock)'),
        ('product', '🏭 Almacenable (con stock)'),
        ('service', '🔧 Servicio'),
    ], string='Tipo de Producto', required=True, default='consu')
    categ_id = fields.Many2one(
        'product.category', string='Categoría', required=True,
        default=lambda self: self.env.ref(
            'product.product_category_all', raise_if_not_found=False
        ),
    )
    uom_id = fields.Many2one('uom.uom', string='Unidad de Medida')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        # Pre-fill name and uom from the line
        line_id = self.env.context.get('default_line_id')
        if line_id:
            line = self.env['purchase.ai.wizard.line'].browse(line_id)
            res['product_name'] = line.suggested_product_name or line.description or ''
            if line.uom_id:
                res['uom_id'] = line.uom_id.id
        return res

    def action_confirm_create(self):
        """Create the product and assign it back to the wizard line."""
        self.ensure_one()
        name = self.product_name.strip()
        if not name:
            raise UserError(_('El nombre del producto no puede estar vacío.'))

        # Duplicate check
        existing = self.env['product.product'].search(
            [('name', '=ilike', name), ('purchase_ok', '=', True)], limit=1
        )
        if existing:
            self.line_id.product_id = existing.id
            self.line_id.match_confidence = 'manual'
            self.line_id.match_reason = 'Producto existente encontrado al intentar crear'
        else:
            uom = self.uom_id or self.env.ref(
                'uom.product_uom_unit', raise_if_not_found=False
            )
            product = self.env['product.product'].create({
                'name':        name,
                'type':        self.product_type,
                'categ_id':    self.categ_id.id,
                'purchase_ok': True,
                'sale_ok':     False,
                'uom_id':      uom.id if uom else False,
                'uom_po_id':   uom.id if uom else False,
            })
            self.line_id.product_id = product.id
            self.line_id.match_confidence = 'created'
            self.line_id.match_reason = (
                'Creado: %s · %s' % (
                    dict(self._fields['product_type'].selection).get(self.product_type, ''),
                    self.categ_id.name,
                )
            )

        # Return to the main wizard
        return self.line_id.wizard_id._reopen()


class PurchaseAIWizardLine(models.TransientModel):
    _name = 'purchase.ai.wizard.line'
    _description = 'AI Invoice Line'

    wizard_id = fields.Many2one('purchase.ai.wizard', ondelete='cascade')

    description = fields.Char(string='Descripción (Factura)')
    product_code = fields.Char(string='Código Proveedor')
    quantity = fields.Float(string='Cantidad', default=1.0)
    uom_id = fields.Many2one('uom.uom', string='Unidad')
    unit_price = fields.Float(string='Precio Unit.', digits=(16, 4))
    line_total = fields.Float(
        string='Subtotal', compute='_compute_line_total', store=True
    )
    tax_ids = fields.Many2many(
        'account.tax', string='Impuestos',
        domain=[('type_tax_use', '=', 'purchase')],
    )
    product_id = fields.Many2one(
        'product.product', string='Producto Odoo',
        domain=[('purchase_ok', '=', True)],
    )
    match_confidence = fields.Selection([
        ('high',    '✅ Alta'),
        ('medium',  '⚠️ Media'),
        ('low',     '❓ Baja'),
        ('none',    '❌ Sin match'),
        ('manual',  '✋ Manual'),
        ('created', '🆕 Creado'),
    ], string='Confianza', default='none', readonly=True)
    match_score = fields.Integer(string='%', readonly=True)
    match_reason = fields.Char(string='Razón', readonly=True)
    suggested_product_name = fields.Char(string='Nombre sugerido')
    needs_product = fields.Boolean(
        string='Sin producto', compute='_compute_needs_product', store=True
    )

    @api.depends('product_id')
    def _compute_needs_product(self):
        for line in self:
            line.needs_product = not line.product_id

    @api.depends('quantity', 'unit_price')
    def _compute_line_total(self):
        for line in self:
            line.line_total = line.quantity * line.unit_price

    @api.onchange('product_id')
    def _onchange_product_id_manual(self):
        if self.product_id:
            self.match_confidence = 'manual'
            self.match_reason = 'Seleccionado manualmente'
            if not self.uom_id and self.product_id.uom_po_id:
                self.uom_id = self.product_id.uom_po_id

    def action_create_product(self):
        """
        Open the mini product wizard to ask type + category before creating.
        """
        self.ensure_one()
        return {
            'type':      'ir.actions.act_window',
            'name':      '🆕 Crear Producto',
            'res_model': 'purchase.ai.product.wizard',
            'view_mode': 'form',
            'target':    'new',
            'context': {
                'default_line_id':      self.id,
                'default_product_name': self.suggested_product_name or self.description or '',
                'default_uom_id':       self.uom_id.id if self.uom_id else False,
            },
        }


class PurchaseAIWizard(models.TransientModel):
    """
    4-stage pipeline:
      1. upload  — drop the document
      2. review  — AI extracts + matches; user corrects vendor/products
      3. approve — final checklist before committing
      4. done    — PO created in DRAFT state (user confirms manually)
    """
    _name = 'purchase.ai.wizard'
    _description = 'AI Invoice → Purchase Order Wizard'

    state = fields.Selection([
        ('upload',  '1. Subir'),
        ('review',  '2. Revisar'),
        ('approve', '3. Aprobar'),
        ('done',    '4. Completado'),
    ], default='upload', string='Etapa')

    document_file = fields.Binary(string='Factura / Recibo', attachment=False)
    document_filename = fields.Char(string='Archivo')
    document_mimetype = fields.Char(compute='_compute_mimetype', store=True)

    @api.depends('document_filename')
    def _compute_mimetype(self):
        ext_map = {
            'pdf':  'application/pdf',
            'jpg':  'image/jpeg', 'jpeg': 'image/jpeg',
            'png':  'image/png',  'webp': 'image/webp',
        }
        for rec in self:
            ext = (rec.document_filename or '').rsplit('.', 1)[-1].lower()
            rec.document_mimetype = ext_map.get(ext, 'application/octet-stream')

    ai_raw_json      = fields.Text(readonly=True)
    ai_matching_json = fields.Text(readonly=True)
    ai_error_message = fields.Char(readonly=True)
    matching_summary = fields.Html(readonly=True)

    vendor_nit      = fields.Char(string='NIT Proveedor')
    vendor_name_raw = fields.Char(string='Nombre según Factura')
    vendor_id = fields.Many2one(
        'res.partner', string='Proveedor en Odoo',
        domain=[('supplier_rank', '>', 0)],
    )
    vendor_state = fields.Selection([
        ('found',      '✅ Encontrado por NIT'),
        ('name_match', '⚠️ Coincidencia por nombre'),
        ('not_found',  '❌ No encontrado — crear'),
        ('created',    '🆕 Creado'),
        ('manual',     '✋ Selección manual'),
    ], string='Estado Proveedor', readonly=True)
    vendor_address = fields.Char(string='Dirección (según factura)')

    invoice_number      = fields.Char(string='Número de Factura')
    fel_uuid            = fields.Char(string='UUID FEL / Autorización SAT')
    fel_serie           = fields.Char(string='Serie DTE')
    invoice_date        = fields.Date(string='Fecha de Factura')
    currency_id = fields.Many2one(
        'res.currency', string='Moneda',
        default=lambda self: self.env['res.currency'].search(
            [('name', '=', 'GTQ')], limit=1
        ),
    )
    subtotal_before_tax = fields.Float(string='Subtotal s/IVA', digits=(16, 2))
    tax_amount          = fields.Float(string='IVA',            digits=(16, 2))
    total_amount        = fields.Float(string='Total',          digits=(16, 2))
    notes               = fields.Text(string='Observaciones')

    line_ids = fields.One2many('purchase.ai.wizard.line', 'wizard_id', string='Líneas')

    approve_vendor_ok  = fields.Boolean(string='✅ Proveedor verificado')
    approve_lines_ok   = fields.Boolean(string='✅ Líneas y productos verificados')
    approve_amounts_ok = fields.Boolean(string='✅ Montos verificados')
    approve_notes      = fields.Text(string='Notas de aprobación (opcional)')

    all_lines_have_product = fields.Boolean(compute='_compute_readiness')
    unmatched_count = fields.Integer(compute='_compute_readiness')

    @api.depends('line_ids', 'line_ids.product_id')
    def _compute_readiness(self):
        for rec in self:
            without = rec.line_ids.filtered(lambda l: not l.product_id)
            rec.unmatched_count = len(without)
            rec.all_lines_have_product = len(without) == 0

    purchase_order_id = fields.Many2one('purchase.order', readonly=True)

    # ════════════════════════════════════════════════════════════════
    # STAGE 1 → 2
    # ════════════════════════════════════════════════════════════════
    def action_analyze_with_ai(self):
        self.ensure_one()
        if not self.document_file:
            raise UserError(_('Sube un documento primero.'))

        api_key = self._get_api_key()
        model   = self._get_model()

        _logger.info('Kesiyos AI P1: extracting %s', self.document_filename)
        raw = self._call_claude_api(api_key, {
            'model': model, 'max_tokens': 2048,
            'messages': [{'role': 'user', 'content': [
                self._build_document_block(),
                {'type': 'text', 'text': EXTRACTION_PROMPT},
            ]}],
        })
        self.ai_raw_json = raw
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise UserError(_('IA devolvió JSON inválido:\n%s') % raw)

        self._populate_header(data)
        invoice_lines = data.get('lines') or []
        line_vals = self._build_line_vals(invoice_lines)

        catalog = self._get_product_catalog()
        _logger.info('Kesiyos AI P2: matching %d lines vs %d products',
                     len(invoice_lines), len(catalog))
        if catalog and invoice_lines:
            try:
                matches = self._run_matching(api_key, model, invoice_lines, catalog)
                self.ai_matching_json = json.dumps(matches, ensure_ascii=False, indent=2)
                line_vals = self._apply_matches(line_vals, matches)
            except Exception as e:
                _logger.warning('Matching failed: %s', e)
                self.ai_error_message = 'Matching falló: %s' % e

        self.line_ids = line_vals
        self.matching_summary = self._summary_html(self.line_ids)
        self.state = 'review'
        return self._reopen()

    # ════════════════════════════════════════════════════════════════
    # Vendor helpers
    # ════════════════════════════════════════════════════════════════
    def action_lookup_vendor_by_nit(self):
        self.ensure_one()
        nit = (self.vendor_nit or '').strip()
        if not nit:
            raise UserError(_('Ingresa el NIT del proveedor primero.'))

        clean = nit.replace('-', '').replace(' ', '')
        partner = self.env['res.partner'].search(
            [('vat', 'in', [nit, clean]), ('supplier_rank', '>', 0)], limit=1
        )
        if partner:
            self.vendor_id    = partner.id
            self.vendor_state = 'found'
            return self._reopen()

        if self.vendor_name_raw:
            partner = self.env['res.partner'].search(
                [('name', 'ilike', self.vendor_name_raw),
                 ('supplier_rank', '>', 0)], limit=1
            )
            if partner:
                self.vendor_id    = partner.id
                self.vendor_state = 'name_match'
                return self._reopen()

        self.vendor_id    = False
        self.vendor_state = 'not_found'
        return self._reopen()

    def action_create_vendor(self):
        self.ensure_one()
        name = self.vendor_name_raw or ''
        if not name:
            raise UserError(_('No hay nombre de proveedor disponible.'))
        if self.vendor_id:
            raise UserError(_('Ya hay un proveedor asignado.'))

        nit   = (self.vendor_nit or '').strip()
        clean = nit.replace('-', '').replace(' ', '')

        if nit:
            existing = self.env['res.partner'].search(
                [('vat', 'in', [nit, clean])], limit=1
            )
            if existing:
                self.vendor_id    = existing.id
                self.vendor_state = 'found'
                return self._reopen()

        partner = self.env['res.partner'].create({
            'name':          name,
            'vat':           nit or False,
            'street':        self.vendor_address or False,
            'is_company':    True,
            'supplier_rank': 1,
            'customer_rank': 0,
        })
        self.vendor_id    = partner.id
        self.vendor_state = 'created'
        return self._reopen()

    @api.onchange('vendor_id')
    def _onchange_vendor_id(self):
        if self.vendor_id and self.vendor_state not in ('found', 'name_match', 'created'):
            self.vendor_state = 'manual'

    # ════════════════════════════════════════════════════════════════
    # STAGE 2 → 3
    # ════════════════════════════════════════════════════════════════
    def action_proceed_to_approve(self):
        self.ensure_one()
        errors = []
        if not self.vendor_id:
            errors.append(_('• Falta el Proveedor.'))
        if not self.invoice_date:
            errors.append(_('• Falta la Fecha de Factura.'))
        if not self.line_ids:
            errors.append(_('• No hay líneas de detalle.'))
        if errors:
            raise ValidationError('\n'.join(errors))

        self.state = 'approve'
        return self._reopen()

    # ════════════════════════════════════════════════════════════════
    # STAGE 3 → 2
    # ════════════════════════════════════════════════════════════════
    def action_go_back_to_review(self):
        self.ensure_one()
        self.state = 'review'
        return self._reopen()

    # ════════════════════════════════════════════════════════════════
    # STAGE 3 → 4  — PO created in DRAFT (not confirmed)
    # ════════════════════════════════════════════════════════════════
    def action_approve_and_create_po(self):
        self.ensure_one()

        if not (self.approve_vendor_ok and self.approve_lines_ok and self.approve_amounts_ok):
            raise ValidationError(_(
                'Debes marcar los tres checks de aprobación antes de confirmar.\n'
                'Revisa: proveedor, líneas y montos.'
            ))
        if not self.vendor_id:
            raise ValidationError(_('Selecciona un proveedor antes de aprobar.'))

        tax  = self._default_tax()
        misc = self._misc_product()

        po_lines = []
        for line in self.line_ids:
            product = line.product_id or misc
            uom     = line.uom_id or product.uom_po_id or product.uom_id
            taxes   = line.tax_ids or (tax if tax else self.env['account.tax'])
            po_lines.append((0, 0, {
                'product_id':   product.id,
                'name':         line.description or product.name,
                'product_qty':  line.quantity,
                'product_uom':  uom.id if uom else False,
                'price_unit':   line.unit_price,
                'taxes_id':     [(6, 0, taxes.ids)],
                'date_planned': fields.Date.today(),
            }))

        po = self.env['purchase.order'].create({
            'partner_id':  self.vendor_id.id,
            'partner_ref': self.invoice_number or False,
            'date_order':  fields.Datetime.now(),
            'currency_id': self.currency_id.id if self.currency_id else False,
            'notes':       self._po_notes(),
            'order_line':  po_lines,
        })

        # Attach original document
        if self.document_file and self.document_filename:
            self.env['ir.attachment'].create({
                'name':      self.document_filename,
                'datas':     self.document_file,
                'res_model': 'purchase.order',
                'res_id':    po.id,
                'mimetype':  self.document_mimetype,
            })

        # ── DRAFT — user confirms manually from the PO ──
        # po.button_confirm() is intentionally NOT called here.
        _logger.info('Kesiyos AI: PO %s created in DRAFT for %s', po.name, self.vendor_id.name)

        self.purchase_order_id = po.id
        self.state = 'done'
        return self._reopen()

    def action_open_po(self):
        self.ensure_one()
        return {
            'type':      'ir.actions.act_window',
            'res_model': 'purchase.order',
            'res_id':    self.purchase_order_id.id,
            'view_mode': 'form',
            'target':    'current',
        }

    # ════════════════════════════════════════════════════════════════
    # Product catalog + matching
    # ════════════════════════════════════════════════════════════════
    def _get_product_catalog(self):
        products = self.env['product.product'].search(
            [('purchase_ok', '=', True), ('active', '=', True)],
            order='name asc', limit=800,
        )
        return [{
            'id':       p.id,
            'code':     p.default_code or '',
            'name':     p.name,
            'category': p.categ_id.name if p.categ_id else '',
            'uom':      p.uom_po_id.name if p.uom_po_id else '',
        } for p in products]

    def _run_matching(self, api_key, model, invoice_lines, catalog):
        catalog_str = '\n'.join(
            '{id} | {code} | {name} | {category} | {uom}'.format(**p)
            for p in catalog
        )
        lines_str = '\n'.join(
            '[{i}] {desc} | qty: {qty} | precio: {price}'.format(
                i=i,
                desc=l.get('description', ''),
                qty=l.get('quantity', 1),
                price=l.get('unit_price', 0),
            )
            for i, l in enumerate(invoice_lines)
        )
        prompt = MATCHING_PROMPT_TEMPLATE.format(
            product_catalog=catalog_str, invoice_lines=lines_str
        )
        raw = self._call_claude_api(api_key, {
            'model': model, 'max_tokens': 2048,
            'messages': [{'role': 'user', 'content': prompt}],
        })
        try:
            result = json.loads(raw)
            return result if isinstance(result, list) else []
        except json.JSONDecodeError:
            return []

    def _populate_header(self, data):
        self.vendor_nit      = data.get('vendor_nit') or ''
        self.vendor_name_raw = data.get('vendor_name') or ''
        self.vendor_address  = data.get('vendor_address') or ''
        self.invoice_number  = data.get('invoice_number') or ''
        self.fel_uuid        = data.get('fel_uuid') or ''
        self.fel_serie       = data.get('fel_serie') or ''
        self.notes           = data.get('notes') or ''

        raw_date = data.get('invoice_date')
        if raw_date:
            try:
                self.invoice_date = datetime.strptime(raw_date[:10], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                self.invoice_date = False
        else:
            self.invoice_date = False

        self.subtotal_before_tax = float(data.get('subtotal_before_tax') or 0)
        self.tax_amount          = float(data.get('tax_amount') or 0)
        self.total_amount        = float(data.get('total_amount') or 0)

        if self.vendor_nit:
            nit   = self.vendor_nit.strip()
            clean = nit.replace('-', '').replace(' ', '')
            partner = self.env['res.partner'].search(
                [('vat', 'in', [nit, clean]), ('supplier_rank', '>', 0)], limit=1
            )
            if partner:
                self.vendor_id    = partner.id
                self.vendor_state = 'found'
            elif self.vendor_name_raw:
                partner = self.env['res.partner'].search(
                    [('name', 'ilike', self.vendor_name_raw),
                     ('supplier_rank', '>', 0)], limit=1
                )
                if partner:
                    self.vendor_id    = partner.id
                    self.vendor_state = 'name_match'
                else:
                    self.vendor_id    = False
                    self.vendor_state = 'not_found'
            else:
                self.vendor_id    = False
                self.vendor_state = 'not_found'

    def _build_line_vals(self, invoice_lines):
        tax = self._default_tax()
        result = []
        for line in invoice_lines:
            uom_name = (line.get('unit_of_measure') or '').strip().lower()
            uom = False
            if uom_name:
                uom = self.env['uom.uom'].search(
                    [('name', 'ilike', uom_name)], limit=1
                )
            lv = {
                'description':           line.get('description') or '',
                'product_code':          line.get('product_code') or '',
                'quantity':              float(line.get('quantity') or 1),
                'unit_price':            float(line.get('unit_price') or 0),
                'match_confidence':      'none',
                'match_score':           0,
                'match_reason':          '',
                'suggested_product_name': '',
            }
            if uom: lv['uom_id']  = uom.id
            if tax: lv['tax_ids'] = [(6, 0, tax.ids)]
            result.append((0, 0, lv))
        return result

    def _apply_matches(self, line_vals, matches):
        for m in matches:
            idx = m.get('line_index')
            if idx is None or idx >= len(line_vals):
                continue
            lv = line_vals[idx][2]
            confidence = m.get('confidence', 'none')
            score      = m.get('confidence_score', 0)
            reason     = m.get('reason', '')
            prod_id    = m.get('product_odoo_id')
            suggested  = m.get('suggested_new_product_name') or ''

            lv['match_confidence']      = confidence
            lv['match_score']           = score
            lv['match_reason']          = reason
            lv['suggested_product_name'] = suggested

            if prod_id and confidence in ('high', 'medium'):
                product = self.env['product.product'].browse(prod_id).exists()
                if product:
                    lv['product_id'] = product.id
                    if not lv.get('uom_id') and product.uom_po_id:
                        lv['uom_id'] = product.uom_po_id.id
        return line_vals

    def _summary_html(self, lines):
        high   = len(lines.filtered(lambda l: l.match_confidence == 'high'))
        medium = len(lines.filtered(lambda l: l.match_confidence == 'medium'))
        low    = len(lines.filtered(lambda l: l.match_confidence in ('low', 'none')))
        total  = len(lines)
        return (
            '<div style="padding:8px;background:#f0f4ff;border-radius:4px;">'
            f'<b>Matching IA:</b> '
            f'<span style="color:#28a745">✅ {high} alta</span> · '
            f'<span style="color:#ffc107">⚠️ {medium} media</span> · '
            f'<span style="color:#dc3545">❌ {low} sin match</span> '
            f'/ {total} líneas totales'
            '</div>'
        )

    def _default_tax(self):
        param = self.env['ir.config_parameter'].sudo().get_param(
            'kesiyos_purchase_ai.default_tax_id'
        )
        if param:
            try:
                return self.env['account.tax'].browse(int(param)).exists()
            except (ValueError, TypeError):
                pass
        return self.env['account.tax'].search(
            [('type_tax_use', '=', 'purchase'), ('amount', '=', 12),
             ('active', '=', True)], limit=1
        )

    def _po_notes(self):
        parts = []
        if self.notes:               parts.append(self.notes)
        if self.vendor_nit:          parts.append('NIT: ' + self.vendor_nit)
        if self.fel_uuid:            parts.append('UUID FEL: ' + self.fel_uuid)
        if self.fel_serie:           parts.append('Serie FEL: ' + self.fel_serie)
        if self.approve_notes:       parts.append('Aprobación: ' + self.approve_notes)
        if self.subtotal_before_tax: parts.append('Subtotal s/IVA: Q %.2f' % self.subtotal_before_tax)
        if self.tax_amount:          parts.append('IVA: Q %.2f' % self.tax_amount)
        return '\n'.join(parts)

    def _misc_product(self):
        p = self.env['product.product'].search(
            [('default_code', '=', 'KES-MISC')], limit=1
        )
        if not p:
            p = self.env['product.product'].create({
                'name':        'Compra Miscelánea / Genérico',
                'default_code': 'KES-MISC',
                'type':        'service',
                'purchase_ok': True,
                'sale_ok':     False,
            })
        return p

    def _get_api_key(self):
        k = self.env['ir.config_parameter'].sudo().get_param(
            'kesiyos_purchase_ai.claude_api_key'
        )
        if not k:
            raise UserError(_(
                'Falta la Claude API Key.\n'
                'Ve a Configuración → Técnico → Parámetros del Sistema.'
            ))
        return k

    def _get_model(self):
        return self.env['ir.config_parameter'].sudo().get_param(
            'kesiyos_purchase_ai.ai_model', 'claude-sonnet-4-5'
        )

    def _build_document_block(self):
        data = self.document_file
        if isinstance(data, bytes):
            data = data.decode('utf-8')
        mt = self.document_mimetype
        if mt == 'application/pdf':
            return {'type': 'document', 'source': {'type': 'base64', 'media_type': mt, 'data': data}}
        if mt in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
            return {'type': 'image', 'source': {'type': 'base64', 'media_type': mt, 'data': data}}
        raise UserError(_('Formato no soportado: %s') % self.document_filename)

    def _call_claude_api(self, api_key, payload):
        url = 'https://api.anthropic.com/v1/messages'
        headers = {
            'Content-Type':      'application/json',
            'x-api-key':         api_key,
            'anthropic-version': '2023-06-01',
        }
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method='POST'
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                rd = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            _logger.error('Claude HTTP %s: %s', e.code, body)
            raise UserError(_('Error Claude API (HTTP %s):\n%s') % (e.code, body))
        except urllib.error.URLError as e:
            raise UserError(_('Error de red: %s') % str(e.reason))

        try:
            text = rd['content'][0]['text']
        except (KeyError, IndexError):
            raise UserError(_('Respuesta inesperada de Claude: %s') % str(rd))

        text = re.sub(r'^```(?:json)?\s*', '', text.strip())
        text = re.sub(r'\s*```$', '', text.strip())
        return text.strip()

    def _reopen(self):
        return {
            'type':      'ir.actions.act_window',
            'res_model': self._name,
            'res_id':    self.id,
            'view_mode': 'form',
            'target':    'new',
            'flags':     {'mode': 'edit'},
        }