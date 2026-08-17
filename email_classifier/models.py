from django.db import models
from django.utils import timezone


class EmailRecord(models.Model):
    class Category(models.TextChoices):
        ENQUIRY = 'ENQUIRY', 'Enquiry'
        CUSTOMER_ORDER = 'CUSTOMER_ORDER', 'Customer Order'
        QUOTATION_REQUEST = 'QUOTATION_REQUEST', 'Quotation Request'
        PAYMENT_INVOICE = 'PAYMENT_INVOICE', 'Payment / Invoice'
        SUPPORT_COMPLAINT = 'SUPPORT_COMPLAINT', 'Support / Complaint'
        OTHERS = 'OTHERS', 'Others'

    sender = models.EmailField(blank=True)
    subject = models.CharField(max_length=255)
    body = models.TextField()
    ai_category = models.CharField(max_length=30, choices=Category.choices)
    final_category = models.CharField(max_length=30, choices=Category.choices, blank=True)
    confidence = models.FloatField(default=0)
    reason = models.TextField(blank=True)
    important_details = models.TextField(blank=True)
    source = models.CharField(max_length=20, default='manual')
    imap_uid = models.CharField(max_length=255, blank=True, null=True, unique=True)
    received_at = models.DateTimeField(default=timezone.now)
    reviewed = models.BooleanField(default=False)
    is_added_to_rfq = models.BooleanField(default=False, help_text="Indicates if enquiry email has been added to RFQ system")

    @property
    def displayed_category(self):
        return self.final_category or self.ai_category

    def __str__(self):
        return self.subject
