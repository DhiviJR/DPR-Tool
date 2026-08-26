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


from email_classifier.models import EmailRecord


def fetch_all_messages(offset=0, limit=25):
    """Return one small, read-only batch of Inbox messages.

    Uses a fast header-first check to skip downloading full bodies for
    emails that already exist in the database.
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

        status, result = client.search(None, 'ALL')
        if status != 'OK':
            raise RuntimeError('Unable to search Inbox emails.')

        message_numbers = list(reversed(result[0].split()))
        batch = message_numbers[offset:offset + limit]

        if not batch:
            return [], len(message_numbers)

        # 1. Fast Header-Only fetch for batch
        headers_info = []
        try:
            status, data = client.fetch(b','.join(batch), '(BODY.PEEK[HEADER])')
            if status == 'OK' and data:
                for item in data:
                    if isinstance(item, tuple) and len(item) == 2 and item[1]:
                        msg = email.message_from_bytes(item[1])
                        msg_id = _decode(msg.get('Message-ID'))
                        uid_val = (msg_id or hashlib.sha256(item[1]).hexdigest())[:255]
                        num = item[0].split()[0]
                        headers_info.append({
                            'num': num,
                            'uid': uid_val,
                            'sender': _decode(msg.get('From')),
                            'subject': _decode(msg.get('Subject')) or '(No subject)',
                            'date': _parse_date(_decode(msg.get('Date'))),
                        })
        except Exception:
            pass

        # 2. Skip emails already stored in DB
        if headers_info:
            uids_in_batch = [h['uid'] for h in headers_info]
            existing_uids = set(EmailRecord.objects.filter(imap_uid__in=uids_in_batch).values_list('imap_uid', flat=True))
            new_headers = [h for h in headers_info if h['uid'] not in existing_uids]

            if not new_headers:
                # All emails in batch already imported! Fast return!
                return [], len(message_numbers)

            # 3. Fetch body ONLY for brand new emails
            messages = []
            for h in new_headers:
                try:
                    b_text = ''
                    if h['num']:
                        st, d = client.fetch(h['num'], '(BODY.PEEK[])')
                        if st == 'OK' and d and d[0] and isinstance(d[0], tuple) and len(d[0]) > 1:
                            m = email.message_from_bytes(d[0][1])
                            b_text = _body(m)
                    messages.append({
                        'uid': h['uid'],
                        'sender': h['sender'],
                        'subject': h['subject'],
                        'body': b_text,
                        'date': h['date'],
                    })
                except Exception:
                    pass
            return messages, len(message_numbers)

        # Fallback to full fetch if header fetch was empty
        messages = []
        for message_number in batch:
            status, data = client.fetch(message_number, '(BODY.PEEK[])')
            if status != 'OK' or not data or not data[0]:
                continue
            raw_message = data[0][1]
            message = email.message_from_bytes(raw_message)
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
