from django.contrib import admin

from .models import RFQ, RFQProduct


class RFQProductInline(admin.TabularInline):
    model = RFQProduct
    extra = 0


@admin.register(RFQ)
class RFQAdmin(admin.ModelAdmin):
    # Display fields in the admin list view
    list_display = (
        'rfq_no',
        'mail_date',
        'customer',
        'quotation_email_sent',
        'email_sent_date',
        'customer_confirmed',
        'created_at'
    )
    
    # Search fields for admin search functionality
    search_fields = ('rfq_no', 'customer__customer_name', 'enquiry_details', 'remarks')
    
    # Filter options in the admin sidebar
    list_filter = ('mail_date', 'customer', 'quotation_email_sent', 'customer_confirmed')
    
    # Read-only fields that should not be edited directly in admin
    readonly_fields = ('email_sent_date', 'quotation_due_date', 'created_at', 'updated_at')
    
    # Organize fields in the detail view
    fieldsets = (
        ('RFQ Information', {
            'fields': ('rfq_no', 'mail_date', 'customer', 'enquiry_details', 'remarks', 'attachment')
        }),
        ('Email Follow-up Alert System', {
            'fields': (
                'quotation_email_sent',
                'email_sent_date',
                'quotation_due_date',
                'customer_confirmed'
            ),
            'description': 'Tracks quotation email status and customer confirmation. Auto-managed by the system.'
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    inlines = [RFQProductInline]
