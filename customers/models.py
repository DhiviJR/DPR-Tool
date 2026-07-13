from django.db import models


class Customer(models.Model):
    REGION_CHOICES = (
        ('Chennai', 'Chennai'),
        ('Hosur', 'Hosur'),
    )

    customer_name = models.CharField(max_length=255)
    region = models.CharField(max_length=20, choices=REGION_CHOICES, blank=True, null=True)
    email = models.EmailField(max_length=254, blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    gstin = models.CharField(max_length=15, blank=True, null=True, verbose_name="GSTIN")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.customer_name

