import email
import hashlib
import imaplib
import re
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


def _extract_attachments(message):
    has_att = False
    att_names = []
    if message.is_multipart():
        for part in message.walk():
            fn = part.get_filename()
            disp = str(part.get('Content-Disposition') or '')
            if fn:
                decoded_fn = ' '.join(_decode(fn).split())
                if decoded_fn and decoded_fn not in att_names:
                    att_names.append(decoded_fn)
                    has_att = True
            elif 'attachment' in disp.lower():
                has_att = True
    return has_att, att_names


def fetch_all_messages(offset=0, limit=25, mail_client=None):
    """Return one small, read-only batch of Inbox messages.

    Uses a fast header-first check to skip downloading full bodies for
    emails that already exist in the database.
    """
    required = (settings.EMAIL_IMAP_HOST, settings.EMAIL_IMAP_USERNAME, settings.EMAIL_IMAP_PASSWORD)
    if not all(required):
        raise ValueError('Set EMAIL_IMAP_HOST, EMAIL_IMAP_USERNAME, and EMAIL_IMAP_PASSWORD in .env.')

    close_at_end = False
    client = mail_client
    if client is None:
        client = imaplib.IMAP4_SSL(settings.EMAIL_IMAP_HOST, settings.EMAIL_IMAP_PORT, timeout=10)
        client.login(settings.EMAIL_IMAP_USERNAME, settings.EMAIL_IMAP_PASSWORD)
        close_at_end = True

    try:
        status, res = client.select(settings.EMAIL_IMAP_MAILBOX, readonly=True)
        if status != 'OK' or not res or not res[0]:
            raise RuntimeError(f'Cannot open mailbox {settings.EMAIL_IMAP_MAILBOX}.')

        try:
            inbox_total = int(res[0])
        except Exception:
            inbox_total = 0

        if inbox_total <= 0:
            return [], 0

        end_seq = max(1, inbox_total - offset)
        start_seq = max(1, end_seq - limit + 1)

        if start_seq > inbox_total or end_seq < 1 or start_seq > end_seq:
            return [], inbox_total

        batch_range = f"{start_seq}:{end_seq}".encode()

        # 1. Fast Header-Only fetch for batch in single request
        headers_info = []
        try:
            status, data = client.fetch(batch_range, '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE CONTENT-TYPE CONTENT-DISPOSITION)])')
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

            # 3. Fetch body & attachment metadata ONLY for brand new emails (skips heavy binary attachments)
            messages = []
            for h in new_headers:
                n = h.get('num')
                if not n:
                    continue
                try:
                    st, d = client.fetch(n, '(BODY.PEEK[HEADER] BODY.PEEK[TEXT])')
                    if st == 'OK' and d:
                        header_bytes = b''
                        text_bytes = b''
                        for item in d:
                            if isinstance(item, tuple) and len(item) == 2:
                                if b'HEADER' in item[0]:
                                    header_bytes = item[1]
                                else:
                                    text_bytes = item[1]
                        raw_bytes = header_bytes + b'\r\n\r\n' + text_bytes
                        m = email.message_from_bytes(raw_bytes)
                        b_text = _body(m)
                        has_att, att_names = _extract_attachments(m)
                        messages.append({
                            'uid': h['uid'],
                            'sender': h['sender'],
                            'subject': h['subject'],
                            'body': b_text,
                            'date': h['date'],
                            'has_attachments': has_att,
                            'attachment_names': '|||'.join(att_names),
                        })
                except Exception:
                    pass
            return messages, inbox_total

        return [], inbox_total
    finally:
        if close_at_end and client:
            try:
                client.logout()
            except Exception:
                pass
