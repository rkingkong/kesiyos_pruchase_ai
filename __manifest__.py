# -*- coding: utf-8 -*-
{
    'name': 'Kesiyos - AI Purchase Invoice Scanner',
    'version': '17.0.3.0.0',
    'category': 'Purchase',
    'summary': 'Scan FEL invoices with Claude AI → Draft Purchase Orders',
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