from django.db import models
from customers.models import Customer
from datetime import datetime

serial_number = models.CharField(
    max_length=50,
    unique=True,
    blank=True,
    null=True
)

class DPR(models.Model):

    serial_number = models.CharField(
        max_length=50,
        unique=True,
        blank=True,
        null=True
    )

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)

    quotation_number = models.CharField(max_length=100, blank=True, null=True)

    quotation_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True
    )

    quotation_attachment = models.FileField(
        upload_to='quotation_files/',
        blank=True,
        null=True
    )

    confirmation_type = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    po_number = models.CharField(
        max_length=100,
        blank=True,
        null=True
    )

    po_value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        blank=True,
        null=True
    )

    po_date = models.DateField(blank=True, null=True)

    po_validity = models.DateField(blank=True, null=True)

    po_attachment = models.FileField(
        upload_to='po_files/',
        blank=True,
        null=True
    )

    status = models.CharField(
        max_length=20,
        blank=True,
        null=True
    )
    cust_qty_ordered = models.IntegerField(default=0)
    supplier_qty_ordered = models.IntegerField(default=0)
    customer_qty_delivered = models.IntegerField(default=0)
    supplier_qty_received = models.IntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):

        if not self.serial_number:

            year = datetime.now().year

            last_record = DPR.objects.order_by('-id').first()

            if last_record:
                next_id = last_record.id + 1
            else:
                next_id = 1

            self.serial_number = f"DPR-{year}-{next_id:04d}"

        super().save(*args, **kwargs)

    def __str__(self):

        return self.serial_number
