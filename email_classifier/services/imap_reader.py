import email
import hashlib
import imaplib
from email.header import decode_header
from email.utils import parsedate_to_datetime

from django.conf import settings
from django.utils import timezone


def _parse_date(date_str):
    if not date_str:
        return timezone.now()
    try:
        dt = parsedate_to_datetime(date_str)
        if dt is None:
            return timezone.now()
        return dt
    except Exception:
        return timezone.now()


def _decode(value):
    if not value:
        return ''
    parts = []
    for text, encoding in decode_header(value):
        if isinstance(text, bytes):
            parts.append(text.decode(encoding or 'utf-8', errors='replace'))
        else:
            parts.append(text)
    return ''.join(parts).strip()


def _body(message):
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() != 'text/plain' or part.get_content_disposition() == 'attachment':
                continue
            raw = part.get_payload(decode=True)
            if raw:
                return raw.decode(part.get_content_charset() or 'utf-8', errors='replace').strip()
        return ''
    raw = message.get_payload(decode=True)
    return raw.decode(message.get_content_charset() or 'utf-8', errors='replace').strip() if raw else ''


def fetch_all_messages(offset=0, limit=20):
    """Return one small, read-only batch of Inbox messages.

    Large mailboxes can close a connection if thousands of full message bodies
    are fetched in a single session. Offset and limit let the command resume
    gradually on later scheduled runs.
    """
    required = (settings.EMAIL_IMAP_HOST, settings.EMAIL_IMAP_USERNAME, settings.EMAIL_IMAP_PASSWORD)
    if not all(required):
        raise ValueError('Set EMAIL_IMAP_HOST, EMAIL_IMAP_USERNAME, and EMAIL_IMAP_PASSWORD in .env.')

    client = imaplib.IMAP4_SSL(settings.EMAIL_IMAP_HOST, settings.EMAIL_IMAP_PORT)
    try:
        client.login(settings.EMAIL_IMAP_USERNAME, settings.EMAIL_IMAP_PASSWORD)
        status, _ = client.select(settings.EMAIL_IMAP_MAILBOX, readonly=True)
        if status != 'OK':
            raise RuntimeError(f'Cannot open mailbox {settings.EMAIL_IMAP_MAILBOX}.')
        # ALL includes previous and newly received messages. BODY.PEEK and
        # readonly=True ensure that merely reading never changes the mailbox.
        # Some cPanel-style mail servers close the connection when sent the
        # UID command. Standard IMAP SEARCH/FETCH is more widely compatible.
        status, result = client.search(None, 'ALL')
        if status != 'OK':
            raise RuntimeError('Unable to search Inbox emails.')

        messages = []
        message_numbers = list(reversed(result[0].split()))
        batch = message_numbers[offset:offset + limit]
        for message_number in batch:
            status, data = client.fetch(message_number, '(BODY.PEEK[])')
            if status != 'OK' or not data or not data[0]:
                continue
            raw_message = data[0][1]
            message = email.message_from_bytes(raw_message)
            # Message-ID is stable across IMAP sequence-number changes. Use a
            # hash for emails that do not include one.
            message_id = _decode(message.get('Message-ID'))
            uid_val = (message_id or hashlib.sha256(raw_message).hexdigest())[:255]
            date_raw = _decode(message.get('Date'))
            parsed_dt = _parse_date(date_raw)
            messages.append({
                'uid': uid_val,
                'sender': _decode(message.get('From')),
                'subject': _decode(message.get('Subject')) or '(No subject)',
                'body': _body(message),
                'date': parsed_dt,
            })
        return messages, len(message_numbers)
    finally:
        try:
            client.logout()
        except Exception:
            pass
