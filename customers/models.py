import re
from django.db import models


class Customer(models.Model):
    REGION_CHOICES = (
        ('Chennai', 'Chennai'),
        ('Hosur', 'Hosur'),
    )
    SEZ_CHOICES = (
        ('No', 'No'),
        ('Yes', 'Yes'),
    )

    customer_name = models.CharField(max_length=255)
    region = models.CharField(max_length=20, choices=REGION_CHOICES, blank=True, null=True)
    email = models.EmailField(max_length=254, blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    gstin = models.CharField(max_length=15, blank=True, null=True, verbose_name="GSTIN")
    state_code = models.CharField(max_length=20, blank=True, null=True, verbose_name="State Code")
    is_sez = models.CharField(max_length=3, choices=SEZ_CHOICES, default='No', verbose_name="SEZ")
    payment_terms = models.CharField(max_length=255, blank=True, null=True, verbose_name="Payment Terms")
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def clean_customer_name(self):
        if not self.customer_name:
            return ''
        name = self.customer_name
        name = re.split(r'[\,;\s]+\bformerly\b', name, flags=re.IGNORECASE)[0].strip()
        name = re.split(r'\s*\(\s*formerly\b', name, flags=re.IGNORECASE)[0].strip()
        name = re.sub(r'^(?:formerly\s+(?:known\s+as\s+)?|fka\b|aka\b|ex\s+name\s*:?|old\s+name\s*:?)\s*', '', name, flags=re.IGNORECASE).strip()
        name = re.sub(r'[\-\s]*\b\d{7,12}\b', '', name).strip(' -_')
        name = re.sub(r'^\d+[\s\-_]+', '', name).strip(' -_')
        name = re.sub(r'[\s\-_]+\d+$', '', name).strip(' -_')
        return name or self.customer_name

    def save(self, *args, **kwargs):
        if self.customer_name:
            name = self.clean_customer_name
            if name:
                self.customer_name = name
        if self.gstin:
            self.gstin = self.gstin.strip().upper()
        if self.is_sez:
            self.is_sez = 'Yes' if str(self.is_sez).strip().lower() == 'yes' else 'No'
        super().save(*args, **kwargs)

    def __str__(self):
        return self.clean_customer_name
