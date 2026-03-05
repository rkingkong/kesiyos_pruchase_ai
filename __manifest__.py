# -*- coding: utf-8 -*-
{
    'name': 'Kesiyos - AI Purchase Invoice Scanner',
    'version': '17.0.3.0.0',
    'category': 'Purchase',
<<<<<<< HEAD
    'summary': 'Scan FEL invoices with Claude AI → Draft Purchase Orders',
=======
    'summary': 'Scan FEL invoices & receipts with Claude AI to create confirmed Purchase Orders',
    'description': """
Kesiyos AI Purchase Scanner — Odoo 17
======================================
4-stage pipeline:
  1. Upload a scanned invoice (PDF/JPG/PNG/WEBP)
  2. Claude AI extracts: vendor, NIT, UUID FEL, lines, IVA, totals
     + semantic product matching against your catalog
  3. Review and correct: NIT-first vendor lookup, per-line product search/create
  4. Approve with 3-checkbox gate → PO confirmed immediately (no drafts)

Configuration:
  Go to Settings → Technical → Parameters → System Parameters and set:
    - kesiyos_purchase_ai.claude_api_key
    - kesiyos_purchase_ai.ai_model
    - kesiyos_purchase_ai.default_tax_id
    """,
>>>>>>> refs/remotes/origin/main
    'author': 'Kesiyos',
    'website': 'https://kesiyos.com',
    'depends': ['base', 'purchase', 'account', 'uom'],
    'data': [
        'security/ir.model.access.csv',
        'views/purchase_ai_wizard_views.xml',
        'views/purchase_ai_product_wizard_views.xml',
        'views/purchase_ai_menus.xml',
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