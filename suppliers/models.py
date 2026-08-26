from django.db import models


class Supplier(models.Model):
    SEZ_CHOICES = (
        ('No', 'No'),
        ('Yes', 'Yes'),
    )

    supplier_name = models.CharField(max_length=255)
    email = models.EmailField(blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    gstin = models.CharField(max_length=15, blank=True, null=True, verbose_name="GSTIN")
    state_code = models.CharField(max_length=20, blank=True, null=True, verbose_name="State Code")
    is_sez = models.CharField(max_length=3, choices=SEZ_CHOICES, default='No', verbose_name="SEZ")
    payment_terms = models.CharField(max_length=255, blank=True, null=True, verbose_name="Payment Terms")
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.gstin:
            self.gstin = self.gstin.strip().upper()
        if self.state_code:
            self.state_code = self.state_code.strip().upper()
        if self.is_sez:
            self.is_sez = 'Yes' if str(self.is_sez).strip().lower() == 'yes' else 'No'
        super().save(*args, **kwargs)

    def __str__(self):
        return self.supplier_name

