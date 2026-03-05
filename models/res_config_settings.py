# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    kesiyos_claude_api_key = fields.Char(
        string='Claude API Key (Anthropic)',
        config_parameter='kesiyos_purchase_ai.claude_api_key',
        help='Your Anthropic Claude API key for invoice scanning (sk-ant-...)',
    )
    kesiyos_ai_model = fields.Selection(
        selection=[
            ('claude-opus-4-5',          'Claude Opus 4.5 (Más Preciso)'),
            ('claude-sonnet-4-5',        'Claude Sonnet 4.5 (Recomendado)'),
            ('claude-haiku-4-5-20251001','Claude Haiku 4.5 (Más Rápido)'),
        ],
        string='Modelo IA',
        default='claude-sonnet-4-5',
        config_parameter='kesiyos_purchase_ai.ai_model',
        help='Selecciona el modelo Claude para analizar facturas.',
    )
    kesiyos_default_tax_id = fields.Many2one(
        comodel_name='account.tax',
        string='Impuesto de Compra por Defecto (IVA)',
        config_parameter='kesiyos_purchase_ai.default_tax_id',
        help='Impuesto aplicado a las líneas de OC (ej. IVA 12%).',
        domain=[('type_tax_use', '=', 'purchase')],
    )
