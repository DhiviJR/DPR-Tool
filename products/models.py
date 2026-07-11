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

    value = models.DecimalField(max_digits=12, decimal_places=2)

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
    quantity_delivered = models.IntegerField(default=0)
    delivery_detail_type = models.CharField(max_length=20, blank=True, null=True)
    invoice_dc_number = models.CharField(max_length=150, blank=True, null=True)
    invoice_dc_attachment = models.FileField(
        upload_to='invoice_dc_attachments/',
        blank=True,
        null=True
    )

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
    quantity_received = models.IntegerField(default=0)
    quantity_not_ok = models.IntegerField(default=0)
    not_ok_reason = models.TextField(blank=True, null=True)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        blank=True,
        null=True,
        default=None
    )

    def __str__(self):
        return self.po_number
