from django.db import models
from dpr.models import DPR
from suppliers.models import Supplier


class CustomerProduct(models.Model):
    PRODUCT_TYPE_CHOICES = (
        ('APG steel', 'APG steel'),
        ('ARG steel', 'ARG steel'),
        ('APG carbide', 'APG carbide'),
        ('ARG carbide', 'ARG carbide'),
        ('SAPG', 'SAPG'),
        ('SARG', 'SARG'),
        ('Multi-Gauge', 'Multi-Gauge'),
        ('unit Std Air', 'unit Std Air'),
        ('unit SPC Air', 'unit SPC Air'),
        ('unit Std lvdt', 'unit Std lvdt'),
        ('unit SPC lvdt', 'unit SPC lvdt'),
        ('AMC', 'AMC'),
        ('Service', 'Service'),
        ('Spares', 'Spares'),
        ('TPG', 'TPG'),
        ('TRG', 'TRG'),
        ('STPG', 'STPG'),
        ('STRG', 'STRG'),
        ('PPG', 'PPG'),
        ('PRG', 'PRG'),
        ('SPPG', 'SPPG'),
        ('SPRG', 'SPRG'),
    )
    STATUS_CHOICES = (
        ('delivered', 'Delivered'),
        ('invoice_pending', 'Invoice Pending'),
        ('partially_delivered', 'Partially Delivered'),
        ('cancelled', 'Cancelled'),
    )

    dpr = models.ForeignKey(DPR, on_delete=models.CASCADE)

    product_name = models.CharField(max_length=255)
    product_type = models.CharField(
        max_length=50,
        choices=PRODUCT_TYPE_CHOICES,
        blank=True,
        null=True
    )

    quantity_ordered = models.IntegerField(default=0)

    rate_per_unit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    mes_rate_per_unit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    value = models.DecimalField(max_digits=12, decimal_places=2)
    mes_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    remarks = models.TextField(blank=True, null=True)

    attachment = models.FileField(
        upload_to='product_attachments/',
        blank=True,
        null=True
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        blank=True,
        null=True,
        default=None
    )
    PAYMENT_STATUS_CHOICES = (
        ('not_received', 'Not Received'),
        ('partially_received', 'Partially Received'),
        ('amount_received', 'Amount Received'),
    )

    quantity_delivered = models.IntegerField(default=0)
    delivery_detail_type = models.CharField(max_length=20, blank=True, null=True)
    invoice_dc_number = models.CharField(max_length=150, blank=True, null=True)
    invoice_dc_attachment = models.FileField(
        upload_to='invoice_dc_attachments/',
        blank=True,
        null=True
    )
    address_attachment = models.FileField(
        upload_to='address_attachments/',
        blank=True,
        null=True
    )

    invoice_date = models.DateField(blank=True, null=True)
    payment_status = models.CharField(
        max_length=25,
        choices=PAYMENT_STATUS_CHOICES,
        default='not_received'
    )
    received_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_received_date = models.DateField(blank=True, null=True)
    payment_notes = models.TextField(blank=True, null=True)
    expected_payment_date = models.DateField(blank=True, null=True)
    follow_up_remarks = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.product_name


class SupplierProduct(models.Model):
    STATUS_CHOICES = (
        ('delivered', 'Delivered'),
        ('partially_delivered', 'Partially Delivered'),
        ('cancelled', 'Cancelled'),
    )

    customer_product = models.ForeignKey(CustomerProduct, on_delete=models.CASCADE)
    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE)

    rate_per_unit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    po_value = models.DecimalField(max_digits=12, decimal_places=2)
    po_date = models.DateField(blank=True, null=True)
    expected_date = models.DateField(blank=True, null=True)
    po_validity = models.DateField(blank=True, null=True)

    quantity = models.IntegerField(default=0)

    po_number = models.CharField(max_length=100)

    po_attachment = models.FileField(upload_to='supplier_po/', blank=True, null=True)
    po_email_sent = models.BooleanField(default=False)
    po_pdf_generated = models.BooleanField(default=False)  # True only after Generate PO & Update is clicked
    PAYMENT_STATUS_CHOICES = (
        ('not_received', 'Not Received'),
        ('partially_received', 'Partially Received'),
        ('amount_received', 'Amount Received'),
    )

    quantity_received = models.IntegerField(default=0)
    quantity_not_ok = models.IntegerField(default=0)
    not_ok_reason = models.TextField(blank=True, null=True)
    rework_sent_date = models.DateField(blank=True, null=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        blank=True,
        null=True,
        default=None
    )
    invoice_dc_number = models.CharField(max_length=150, blank=True, null=True)
    supplier_invoice_number = models.CharField(max_length=150, blank=True, null=True)
    supplier_bill_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, blank=True, null=True)
    bill_attachment = models.FileField(upload_to='supplier_bills/', blank=True, null=True)
    invoice_date = models.DateField(blank=True, null=True)
    payment_status = models.CharField(
        max_length=25,
        choices=PAYMENT_STATUS_CHOICES,
        default='not_received'
    )
    received_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_received_date = models.DateField(blank=True, null=True)
    payment_notes = models.TextField(blank=True, null=True)
    expected_payment_date = models.DateField(blank=True, null=True)
    follow_up_remarks = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.po_number


class CustomerInvoice(models.Model):
    customer_product = models.ForeignKey(CustomerProduct, on_delete=models.CASCADE, related_name='invoices')
    invoice_number = models.CharField(max_length=100)
    quantity = models.IntegerField(default=0)
    invoice_date = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.invoice_number} (Qty: {self.quantity})"

