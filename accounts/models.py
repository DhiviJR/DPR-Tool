from django.contrib.auth.models import AbstractUser
from django.db import models
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver


class CustomUser(AbstractUser):
    USER_TYPE_CHOICES = (
        ('admin', 'Admin'),
        ('user', 'User'),
    )

    user_type = models.CharField(max_length=20, choices=USER_TYPE_CHOICES, default='user')

    @property
    def role(self):
        if self.is_superuser:
            return UserProfile.ROLE_ADMIN
        try:
            return self.profile.role
        except Exception:
            profile, _ = UserProfile.objects.get_or_create(user=self)
            return profile.role

    @property
    def is_admin(self):
        return self.role == UserProfile.ROLE_ADMIN or self.is_superuser

    @property
    def is_sales(self):
        return self.role == UserProfile.ROLE_SALES

    @property
    def is_purchase(self):
        return self.role == UserProfile.ROLE_PURCHASE

    @property
    def is_accounts(self):
        return self.role == UserProfile.ROLE_ACCOUNTS

    def has_role(self, *allowed_roles):
        if self.is_superuser:
            return True
        return self.role in allowed_roles


class UserProfile(models.Model):
    ROLE_ADMIN = 'ADMIN'
    ROLE_SALES = 'SALES'
    ROLE_PURCHASE = 'PURCHASE'
    ROLE_ACCOUNTS = 'ACCOUNTS'

    ROLE_CHOICES = [
        (ROLE_ADMIN, 'ADMIN'),
        (ROLE_SALES, 'SALES'),
        (ROLE_PURCHASE, 'PURCHASE'),
        (ROLE_ACCOUNTS, 'ACCOUNTS'),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_ADMIN)

    def save(self, *args, **kwargs):
        if not self.pk:
            existing = UserProfile.objects.filter(user=self.user).first()
            if existing:
                existing.role = self.role
                existing.save()
                self.pk = existing.pk
                return
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} ({self.role})"


@receiver(post_save, sender=CustomUser)
def create_or_update_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)
    else:
        if hasattr(instance, 'profile'):
            instance.profile.save()


class UserDetail(models.Model):
    user_name = models.CharField(max_length=255, verbose_name="User Name")
    user_number = models.CharField(max_length=100, blank=True, null=True, verbose_name="User Number")
    user_mail_id = models.CharField(max_length=255, blank=True, null=True, verbose_name="User Mail ID")
    user_designation = models.CharField(max_length=255, blank=True, null=True, verbose_name="User Designation")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['user_name']
        verbose_name = "User Detail"
        verbose_name_plural = "User Details"

    def __str__(self):
        return f"{self.user_name} ({self.user_designation or 'No Designation'})"


class EmailConfiguration(models.Model):
    smtp_host = models.CharField(max_length=255, default='mail.mesinstruments.co.in', verbose_name="SMTP Host")
    smtp_port = models.IntegerField(default=465, verbose_name="SMTP Port")
    email_user = models.CharField(max_length=255, default='info@mesinstruments.co.in', verbose_name="Email Address / Username")
    encrypted_password = models.CharField(max_length=500, blank=True, verbose_name="Encrypted Password")
    use_ssl = models.BooleanField(default=True, verbose_name="Use SSL")
    use_tls = models.BooleanField(default=False, verbose_name="Use TLS")
    default_from_email = models.CharField(max_length=255, default='info@mesinstruments.co.in', verbose_name="Default From Email")

    imap_host = models.CharField(max_length=255, default='mail.mesinstruments.co.in', verbose_name="IMAP Host")
    imap_port = models.IntegerField(default=993, verbose_name="IMAP Port")

    is_active = models.BooleanField(default=True, verbose_name="Is Active")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Email Configuration"
        verbose_name_plural = "Email Configurations"

    def set_password(self, raw_password):
        if raw_password:
            from .email_config_utils import encrypt_secret
            self.encrypted_password = encrypt_secret(raw_password)

    def get_password(self):
        from .email_config_utils import decrypt_secret
        return decrypt_secret(self.encrypted_password)

    def __str__(self):
        return f"{self.email_user} ({self.smtp_host}:{self.smtp_port})"

