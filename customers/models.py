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
    email = models.CharField(max_length=500, blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    user_detail = models.ForeignKey('accounts.UserDetail', on_delete=models.SET_NULL, blank=True, null=True, related_name='customers', verbose_name="User Detail")
    contact_person = models.CharField(max_length=255, blank=True, null=True, verbose_name="User Name")
    contact_number = models.CharField(max_length=500, blank=True, null=True, verbose_name="User Number")
    user_mail_id = models.CharField(max_length=500, blank=True, null=True, verbose_name="User Mail ID")
    address = models.TextField(blank=True, null=True)
    gstin = models.CharField(max_length=15, blank=True, null=True, verbose_name="GSTIN")
    state_code = models.CharField(max_length=20, blank=True, null=True, verbose_name="State Code")
    is_sez = models.CharField(max_length=3, choices=SEZ_CHOICES, default='No', verbose_name="SEZ")
    payment_terms = models.CharField(max_length=255, blank=True, null=True, verbose_name="Payment Terms")
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def user_display_name(self):
        if self.user_detail:
            return self.user_detail.user_name
        return self.contact_person or ''

    @property
    def kind_attention_display(self):
        if self.user_detail:
            if self.user_detail.user_designation:
                return f"{self.user_detail.user_name} ({self.user_detail.user_designation})"
            return self.user_detail.user_name
        if self.contact_person:
            try:
                from accounts.models import UserDetail
                ud = UserDetail.objects.filter(user_name__iexact=self.contact_person.strip()).first()
                if ud and ud.user_designation:
                    return f"{ud.user_name} ({ud.user_designation})"
            except Exception:
                pass
            return self.contact_person
        return ''

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
