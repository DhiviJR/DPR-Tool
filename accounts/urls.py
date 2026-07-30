from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('dpr-view/', views.dpr_view, name='dpr_view'),
    path('customer-po-product-details/', views.customer_po_product_details, name='customer_po_product_details'),
    path('supplier-po-product-details/', views.supplier_po_product_details, name='supplier_po_product_details'),
    path('rfq/', views.rfq_details, name='rfq_details'),
    path('rfq/<int:rfq_id>/quotation/download/', views.rfq_quotation_download, name='rfq_quotation_download'),
    path('dpr/<int:dpr_id>/products/', views.dpr_products, name='dpr_products'),
    path('dpr/<int:dpr_id>/documents/download/', views.dpr_documents_download, name='dpr_documents_download'),
    path('dpr/<int:dpr_id>/generate-po/', views.generate_supplier_po, name='generate_supplier_po'),
    path('dpr/<int:dpr_id>/send-po-email/', views.send_supplier_po_email, name='send_supplier_po_email'),
    path('customer-product/<int:product_id>/status/', views.customer_product_status_update, name='customer_product_status_update'),
    path('customer-product/<int:product_id>/invoice/', views.generate_customer_invoice, name='generate_customer_invoice'),
    path('customer-product/<int:product_id>/invoice-modal-data/', views.customer_invoice_modal_data, name='customer_invoice_modal_data'),
    path('customer-product/<int:product_id>/supplier-status/', views.supplier_status_details, name='supplier_status_details'),
    path('supplier-product/<int:supplier_product_id>/status/', views.supplier_product_status_update, name='supplier_product_status_update'),
    path('supplier-product/<int:supplier_product_id>/expected-date/', views.supplier_product_expected_date_update, name='supplier_product_expected_date_update'),
    path('dpr/<int:dpr_id>/check-po-date/', views.check_po_date_status, name='check_po_date_status'),
    path('dpr/<int:dpr_id>/save-po-confirmation/', views.save_po_confirmation_date, name='save_po_confirmation_date'),
    path('dpr/<int:dpr_id>/edit/', views.customer_order_edit, name='customer_order_edit'),
    path('dpr/<int:dpr_id>/supplier/', views.dpr_supplier, name='dpr_supplier'),
    path('dpr/<int:dpr_id>/status/', views.dpr_status_update, name='dpr_status_update'),

    path('login/', views.user_login, name='login'),
    path('logout/', views.user_logout, name='logout'),
    path('register/', views.register, name='register'),

    path('customer-order/', views.customer_order, name='customer_order'),
    path('customer-details/', views.customer_details, name='customer_details'),
    path('supplier-details/', views.supplier_details, name='supplier_details'),
    path('add-customer/',views.add_customer,name='add_customer'
),
    path('add-supplier/', views.add_supplier, name='add_supplier'),
    path('get-customer-quotations/', views.get_customer_quotations, name='get_customer_quotations'),
    path('check-customer-po-number/', views.check_customer_po_number, name='check_customer_po_number'),
]
