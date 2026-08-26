from django.contrib import admin
from .models import CustomerProduct, SupplierProduct

@admin.register(CustomerProduct)
class CustomerProductAdmin(admin.ModelAdmin):
    list_display = (
        'product_name',
        'dpr',
        'status',
        'quantity_ordered',
        'quantity_delivered',
        'delivery_detail_type',
        'invoice_dc_number',
        'invoice_dc_attachment'
    )
    list_filter = ('status', 'delivery_detail_type')
    search_fields = ('product_name', 'invoice_dc_number', 'dpr__serial_number', 'dpr__customer__customer_name')

@admin.register(SupplierProduct)
class SupplierProductAdmin(admin.ModelAdmin):
    list_display = ('po_number', 'customer_product', 'supplier', 'quantity', 'quantity_received', 'status', 'po_attachment')
    search_fields = ('po_number', 'supplier__supplier_name')

