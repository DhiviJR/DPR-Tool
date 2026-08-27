from django.db import models
from datetime import datetime

from customers.models import Customer
from products.models import CustomerProduct
from suppliers.models import Supplier


class RFQ(models.Model):
    rfq_no = models.CharField(max_length=100, unique=True, blank=True, null=True)
    mail_date = models.DateField()
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='rfqs')
    enquiry_details = models.TextField()
    remarks = models.TextField(blank=True, null=True)
    attachment = models.FileField(upload_to='rfq_attachments/', blank=True, null=True)
    source_email = models.ForeignKey(
        'email_classifier.EmailRecord',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rfqs',
        help_text="Original email record from which this RFQ was created"
    )
    
    # Email Follow-up Alert System Fields
    # Tracks when the quotation email was sent to the customer
    email_sent_date = models.DateTimeField(null=True, blank=True, help_text="Date and time when quotation email was sent to customer")
    
    # Calculated deadline for customer quotation confirmation (email_sent_date + 3 days)
    quotation_due_date = models.DateField(null=True, blank=True, help_text="Deadline for customer to confirm quotation (email_sent_date + 3 days)")
    
    # Flag to track if quotation email has been sent
    quotation_email_sent = models.BooleanField(default=False, help_text="Indicates if quotation email has been sent to customer")
    
    # Flag to track if quotation has been prepared
    quotation_prepared = models.BooleanField(default=False, help_text="Indicates if quotation has been prepared/generated")
    
    # Flag to track if customer has confirmed the quotation
    customer_confirmed = models.BooleanField(default=False, help_text="Indicates if customer has confirmed the quotation")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-mail_date', '-created_at']

    def __str__(self):
        return self.rfq_no

    def save(self, *args, **kwargs):
        if not self.rfq_no:
            year = datetime.now().year
            last_record = RFQ.objects.order_by('-id').first()
            next_id = last_record.id + 1 if last_record else 1
            self.rfq_no = f"RFQ-{year}-{next_id:04d}"
        super().save(*args, **kwargs)


class RFQProduct(models.Model):
    rfq = models.ForeignKey(RFQ, on_delete=models.CASCADE, related_name='products')
    product_name = models.CharField(max_length=255)
    product_type = models.CharField(
        max_length=50,
        choices=CustomerProduct.PRODUCT_TYPE_CHOICES,
        blank=True,
        null=True
    )
    price_known = models.BooleanField(default=True)
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name='rfq_price_requests'
    )
    suppliers = models.ManyToManyField(
        Supplier,
        blank=True,
        related_name='rfq_multi_price_requests'
    )
    quantity = models.IntegerField(default=0)
    unit = models.CharField(max_length=20, default="No's", blank=True, null=True)
    rate_per_unit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    value = models.DecimalField(max_digits=12, decimal_places=2)
    quotation_email_sent = models.BooleanField(
        default=False,
        help_text="Indicates if quotation email has been sent for this product"
    )
    quotation_prepared = models.BooleanField(
        default=False,
        help_text="Indicates if quotation has been prepared for this product"
    )
    remarks = models.TextField(blank=True, null=True)
    product_specifications = models.JSONField(blank=True, null=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    def get_formatted_specifications(self):
        if not self.product_specifications or not isinstance(self.product_specifications, dict):
            return ""
        items = []
        for k, v in self.product_specifications.items():
            if v and str(v).strip():
                items.append(f"{k}: {v}")
        return " | ".join(items)

    @property
    def display_name(self):
        specs = self.get_formatted_specifications()
        if specs:
            return f"{self.product_name} ({specs})"
        return self.product_name

    def __str__(self):
        return f"{self.rfq.rfq_no} - {self.product_name}"


class RFQSupplierPrice(models.Model):
    product = models.ForeignKey(
        RFQProduct,
        on_delete=models.CASCADE,
        related_name='supplier_prices'
    )
    supplier = models.ForeignKey(
        Supplier,
        on_delete=models.PROTECT,
        related_name='rfq_supplier_prices'
    )
    price = models.DecimalField(max_digits=12, decimal_places=2)
    value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('product', 'supplier')
        ordering = ['supplier__supplier_name']

    def __str__(self):
        return f"{self.product} - {self.supplier}: {self.price}"


class RFQQuotation(models.Model):
    rfq = models.ForeignKey(RFQ, on_delete=models.CASCADE, related_name='quotations')
    quotation_number = models.CharField(max_length=120)
    revision_number = models.PositiveIntegerField(default=0)
    products_snapshot = models.JSONField(default=list, blank=True)
    email_sent = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ('rfq', 'quotation_number')

    def __str__(self):
        return self.quotation_number


class RFQEmailMessage(models.Model):
    DIRECTION_CHOICES = (
        ('OUT', 'Outgoing'),
        ('IN', 'Incoming'),
    )

    rfq = models.ForeignKey(RFQ, on_delete=models.CASCADE, related_name='email_messages')
    message_id = models.CharField(max_length=255, unique=True)
    in_reply_to = models.CharField(max_length=255, blank=True, null=True)
    references = models.TextField(blank=True, null=True)
    sender = models.CharField(max_length=255)
    recipients = models.TextField()
    cc_recipients = models.TextField(blank=True, null=True)
    subject = models.CharField(max_length=500)
    body = models.TextField(blank=True, null=True)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES, default='OUT')
    sent_at = models.DateTimeField()
    has_attachments = models.BooleanField(default=False)
    attachment_names = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sent_at', 'created_at']

    def __str__(self):
        return f"{self.direction} - {self.rfq.rfq_no} - {self.subject}"




