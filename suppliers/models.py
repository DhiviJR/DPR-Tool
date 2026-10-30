from django.db import models


class Supplier(models.Model):
    SEZ_CHOICES = (
        ('No', 'No'),
        ('Yes', 'Yes'),
    )

    supplier_name = models.CharField(max_length=255)
    email = models.CharField(max_length=500, blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    user_detail = models.ForeignKey('accounts.UserDetail', on_delete=models.SET_NULL, blank=True, null=True, related_name='suppliers', verbose_name="User Detail")
    user_details = models.ManyToManyField('accounts.UserDetail', blank=True, related_name='suppliers_list', verbose_name="User Details")
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
    def users_list(self):
        """Returns list of UserDetail objects; falls back to user_detail if M2M empty."""
        users = list(self.user_details.all())
        if not users and self.user_detail:
            return [self.user_detail]
        return users

    @property
    def user_ids_list(self):
        return [u.id for u in self.users_list]

    @property
    def user_display_names(self):
        users = self.users_list
        if users:
            return [u.user_name for u in users]
        if self.contact_person:
            return [p.strip() for p in self.contact_person.split(',') if p.strip()]
        return []

    @property
    def user_display_name(self):
        names = self.user_display_names
        return ', '.join(names) if names else ''

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

