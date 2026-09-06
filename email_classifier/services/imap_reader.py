import email
import hashlib
import imaplib
import re
from email.header import decode_header
from email.utils import parsedate_to_datetime

from django.conf import settings
from django.utils import timezone


import datetime
from email.utils import parsedate_to_datetime, parsedate_tz, mktime_tz


def _parse_date(date_str):
    if not date_str:
        return timezone.now()

    raw = str(date_str).strip()
    # Strip comments in parentheses, e.g. "Mon, 31 Aug 2026 15:48:46 +0530 (IST)" -> "Mon, 31 Aug 2026 15:48:46 +0530"
    clean = re.sub(r'\s*\([^)]*\)', '', raw).strip()

    # 1. Standard library parsedate_to_datetime on cleaned string
    try:
        dt = parsedate_to_datetime(clean)
        if dt is not None:
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            return dt
    except Exception:
        pass

    # 2. Try raw uncleaned string
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_current_timezone())
            return dt
    except Exception:
        pass

    # 3. Try parsedate_tz
    try:
        t = parsedate_tz(clean) or parsedate_tz(raw)
        if t:
            stamp = mktime_tz(t)
            dt = datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc)
            return dt
    except Exception:
        pass

    # 4. Fallback regex match for "DD Month YYYY HH:MM:SS"
    try:
        m = re.search(r'(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?', clean)
        if m:
            day, month_str, year, hr, mn, sec = m.groups()
            sec = sec or '00'
            dt_str = f"{day} {month_str} {year} {hr}:{mn}:{sec}"
            dt_naive = datetime.datetime.strptime(dt_str, '%d %b %Y %H:%M:%S')
            return timezone.make_aware(dt_naive, timezone.utc)
    except Exception:
        pass

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

        # 1. Fast Header-Only fetch for batch in single request
        headers_info = []
        try:
            status, data = client.fetch(b','.join(batch), '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE CONTENT-TYPE CONTENT-DISPOSITION)])')
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

        # 2. Skip emails already stored in DB (newest-first)
        if headers_info:
            headers_info.sort(key=lambda x: x['date'] or timezone.now(), reverse=True)
            uids_in_batch = [h['uid'] for h in headers_info]
            existing_uids = set(EmailRecord.objects.filter(imap_uid__in=uids_in_batch).values_list('imap_uid', flat=True))
            new_headers = [h for h in headers_info if h['uid'] not in existing_uids]

            if not new_headers:
                # All emails in batch already imported! Fast return!
                return [], len(message_numbers)

            # 3. Fetch body & attachments ONLY for brand new emails in fast batch chunks
            messages = []
            num_to_header = {h['num']: h for h in new_headers if h.get('num')}
            num_list = list(num_to_header.keys())

            FETCH_CHUNK = 50
            for i in range(0, len(num_list), FETCH_CHUNK):
                chunk = num_list[i:i + FETCH_CHUNK]
                try:
                    st, d = client.fetch(b','.join(chunk), '(BODY.PEEK[])')
                    if st == 'OK' and d:
                        for item in d:
                            if isinstance(item, tuple) and len(item) == 2 and item[1]:
                                num_match = re.search(rb'^\d+', item[0])
                                num_val = num_match.group(0) if num_match else b''
                                h = num_to_header.get(num_val)
                                if h and isinstance(item[1], bytes):
                                    m = email.message_from_bytes(item[1])
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
                    # Fallback to individual fetch if batch fetch fails on a specific server
                    for n in chunk:
                        h = num_to_header.get(n)
                        if not h:
                            continue
                        try:
                            st, d = client.fetch(n, '(BODY.PEEK[])')
                            if st == 'OK' and d and d[0] and isinstance(d[0], tuple) and len(d[0]) > 1:
                                m = email.message_from_bytes(d[0][1])
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
            return messages, len(message_numbers)

        return [], len(message_numbers)
    finally:
        if close_at_end and client:
            try:
                client.logout()
            except Exception:
                pass
