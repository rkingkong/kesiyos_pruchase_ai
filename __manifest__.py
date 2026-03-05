# -*- coding: utf-8 -*-
{
    'name': 'Kesiyos - AI Purchase Invoice Scanner',
    'version': '17.0.2.0.0',
    'category': 'Purchase',
    'summary': 'Scan FEL invoices & receipts with Claude AI to create confirmed Purchase Orders',
    'description': """
Kesiyos AI Purchase Scanner — Odoo 17
======================================
4-stage pipeline:
  1. Upload a scanned invoice (PDF/JPG/PNG/WEBP)
  2. Claude AI extracts: vendor, NIT, UUID FEL, lines, IVA, totals
     + semantic product matching against your catalog
  3. Review & correct: NIT-first vendor lookup, per-line product search/create
  4. Approve with 3-checkbox gate → PO confirmed immediately (no drafts)

Odoo 17 compatible:
  - No required= on invisible fields (fixes "Campos no válidos" client error)
  - <list> instead of <tree> in wizard One2many
  - Direct Python expressions (no attrs={} deprecated syntax)
  - action_go_back_to_review() for safe back-navigation without data loss
    """,
    'author': 'Kesiyos',
    'website': 'https://kesiyos.com',
    'depends': [
        'base',
        'purchase',
        'account',
        'uom',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/purchase_ai_wizard_views.xml',
        'views/purchase_ai_menus.xml',
        'views/res_config_settings_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'kesiyos_purchase_ai/static/src/css/wizard.css',
        ],
    },
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
