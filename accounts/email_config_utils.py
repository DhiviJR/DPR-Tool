import base64
import hashlib
import imaplib
import logging
import smtplib
from cryptography.fernet import Fernet
from django.conf import settings

logger = logging.getLogger(__name__)


def _get_fernet_cipher():
    secret = getattr(settings, 'SECRET_KEY', 'default-fallback-secret-key-for-dpr-tool')
    key_32 = hashlib.sha256(secret.encode('utf-8')).digest()
    url_safe_key = base64.urlsafe_b64encode(key_32)
    return Fernet(url_safe_key)


def encrypt_secret(raw_val: str) -> str:
    if not raw_val:
        return ""
    try:
        cipher = _get_fernet_cipher()
        return cipher.encrypt(raw_val.encode('utf-8')).decode('utf-8')
    except Exception as e:
        logger.error(f"Failed to encrypt secret: {e}")
        return raw_val


def decrypt_secret(encrypted_val: str) -> str:
    if not encrypted_val:
        return ""
    try:
        cipher = _get_fernet_cipher()
        return cipher.decrypt(encrypted_val.encode('utf-8')).decode('utf-8')
    except Exception:
        # If decryption fails (e.g. text was plain-text or invalid key), return raw string
        return encrypted_val


def get_active_email_config():
    """Fetch active EmailConfiguration from DB safely without raising errors."""
    try:
        from accounts.models import EmailConfiguration
        return EmailConfiguration.objects.filter(is_active=True).first()
    except Exception:
        return None


def load_email_settings_to_runtime():
    """
    Loads active EmailConfiguration into Django settings in memory.
    This guarantees that all existing views, background tasks, and django.core.mail calls
    automatically use the admin-configured credentials without code changes.
    """
    config = get_active_email_config()
    if not config:
        return False

    decrypted_pass = config.get_password()
    if not decrypted_pass:
        return False

    try:
        settings.EMAIL_HOST = config.smtp_host or 'mail.mesinstruments.co.in'
        settings.EMAIL_PORT = int(config.smtp_port or 465)
        settings.EMAIL_HOST_USER = config.email_user or ''
        settings.EMAIL_HOST_PASSWORD = decrypted_pass
        settings.EMAIL_USE_SSL = bool(config.use_ssl)
        settings.EMAIL_USE_TLS = bool(config.use_tls)
        settings.DEFAULT_FROM_EMAIL = config.default_from_email or config.email_user or ''

        # IMAP settings for inbox sync
        settings.EMAIL_IMAP_HOST = config.imap_host or config.smtp_host or 'mail.mesinstruments.co.in'
        settings.EMAIL_IMAP_PORT = int(config.imap_port or 993)
        settings.EMAIL_IMAP_USERNAME = config.email_user or ''
        settings.EMAIL_IMAP_PASSWORD = decrypted_pass
        return True
    except Exception as e:
        logger.warning(f"Could not load email settings to runtime: {e}")
        return False


def test_smtp_connection(host, port, user, password, use_ssl=True, use_tls=False):
    """
    Tests SMTP connection and authentication.
    Returns (success: bool, message: str)
    """
    try:
        port = int(port)
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=12)
        else:
            server = smtplib.SMTP(host, port, timeout=12)
            if use_tls:
                server.starttls()
        
        server.login(user, password)
        server.quit()
        return True, "SMTP connection and login successful!"
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP Authentication Failed: Invalid Email or Password."
    except smtplib.SMTPConnectError as e:
        return False, f"SMTP Connection Failed: Unable to connect to {host}:{port} ({e})."
    except Exception as e:
        return False, f"SMTP Error: {str(e)}"


def test_imap_connection(host, port, user, password):
    """
    Tests IMAP connection and authentication for Inbox Sync.
    Returns (success: bool, message: str)
    """
    try:
        port = int(port)
        mail = imaplib.IMAP4_SSL(host, port, timeout=12)
        mail.login(user, password)
        status, _ = mail.select('INBOX', readonly=True)
        mail.logout()
        if status == 'OK':
            return True, "IMAP Inbox connection and login successful!"
        return False, f"IMAP connected, but could not select INBOX (status: {status})."
    except imaplib.IMAP4.error as e:
        return False, f"IMAP Authentication Failed: Invalid credentials ({e})."
    except Exception as e:
        return False, f"IMAP Error: {str(e)}"
