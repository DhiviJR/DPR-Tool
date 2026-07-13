from django.db import models


class Supplier(models.Model):
    supplier_name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    gstin = models.CharField(max_length=15, blank=True, null=True, verbose_name="GSTIN")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.supplier_name
