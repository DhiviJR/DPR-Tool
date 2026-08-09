import imaplib
import email
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
import uuid
import re
import logging
from datetime import datetime

from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone
from rfq.models import RFQ, RFQEmailMessage

logger = logging.getLogger(__name__)


def _clean_header_str(raw_header):
    if not raw_header:
        return ""
    decoded_parts = decode_header(raw_header)
    result = []
    for content, encoding in decoded_parts:
        if isinstance(content, bytes):
            try:
                result.append(content.decode(encoding or 'utf-8', errors='replace'))
            except Exception:
                result.append(content.decode('latin-1', errors='replace'))
        else:
            result.append(str(content))
    return "".join(result).strip()


def _html_to_plain_text(html):
    """Convert HTML to plain readable text, preserving line structure."""
    import html as html_lib
    # Replace block-level tags with newlines before stripping
    html = re.sub(r'(?i)<br\s*/?>', '\n', html)
    html = re.sub(r'(?i)</?p[^>]*>', '\n', html)
    html = re.sub(r'(?i)</?div[^>]*>', '\n', html)
    html = re.sub(r'(?i)</?tr[^>]*>', '\n', html)
    html = re.sub(r'(?i)</?li[^>]*>', '\n• ', html)
    html = re.sub(r'(?i)</?h[1-6][^>]*>', '\n', html)
    # Remove style and script blocks entirely
    html = re.sub(r'(?is)<style[^>]*>.*?</style>', '', html)
    html = re.sub(r'(?is)<script[^>]*>.*?</script>', '', html)
    # Strip remaining HTML tags
    html = re.sub(r'<[^>]+>', '', html)
    # Decode HTML entities (&amp; &lt; &gt; &nbsp; etc.)
    html = html_lib.unescape(html)
    # Collapse excessive whitespace but preserve newlines
    lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in html.splitlines()]
    # Remove consecutive blank lines (more than 2 in a row)
    result_lines = []
    blank_count = 0
    for line in lines:
        if line == '':
            blank_count += 1
            if blank_count <= 2:
                result_lines.append(line)
        else:
            blank_count = 0
            result_lines.append(line)
    return '\n'.join(result_lines).strip()


def _decode_payload(part):
    """Decode a message part payload using its charset, handling QP/base64."""
    try:
        payload_bytes = part.get_payload(decode=True)  # handles base64 and quoted-printable
        if payload_bytes is None:
            # Fallback: get raw string payload (may still be QP-encoded)
            raw = part.get_payload()
            if isinstance(raw, str):
                import quopri
                cte = str(part.get('Content-Transfer-Encoding', '')).strip().lower()
                if cte == 'quoted-printable':
                    try:
                        payload_bytes = quopri.decodestring(raw.encode('latin-1'))
                    except Exception:
                        return raw
                elif cte == 'base64':
                    import base64
                    try:
                        payload_bytes = base64.decodebytes(raw.encode('ascii'))
                    except Exception:
                        return raw
                else:
                    return raw
            else:
                return ''
        charset = part.get_content_charset() or 'utf-8'
        try:
            return payload_bytes.decode(charset, errors='replace')
        except (LookupError, UnicodeDecodeError):
            return payload_bytes.decode('utf-8', errors='replace')
    except Exception:
        return ''


def _extract_email_body(msg):
    """Extract best readable body from an email.message.Message object."""
    plain_body = ''
    html_body = ''

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get('Content-Disposition', ''))
            if 'attachment' in disposition:
                continue
            if content_type == 'text/plain' and not plain_body:
                plain_body = _decode_payload(part)
            elif content_type == 'text/html' and not html_body:
                html_body = _decode_payload(part)
    else:
        content_type = msg.get_content_type()
        if content_type == 'text/html':
            html_body = _decode_payload(msg)
        else:
            plain_body = _decode_payload(msg)

    if plain_body:
        # Clean up quoted-printable soft line breaks that slipped through
        plain_body = re.sub(r'=\r?\n', '', plain_body)
        # Collapse lines that are just URL continuations (very long single tokens)
        lines = plain_body.splitlines()
        cleaned = []
        for line in lines:
            stripped = line.strip()
            # If a line is purely a very long URL fragment (no spaces, >120 chars), skip it
            if len(stripped) > 150 and ' ' not in stripped and stripped.startswith('http'):
                if cleaned:
                    cleaned[-1] = cleaned[-1].rstrip() + ' [link]'
                continue
            cleaned.append(line)
        plain_body = '\n'.join(cleaned)
        # Remove consecutive blank lines > 2
        plain_body = re.sub(r'\n{3,}', '\n\n', plain_body)
        return plain_body.strip()

    if html_body:
        return _html_to_plain_text(html_body)

    return ''




def send_threaded_rfq_email(
    rfq,
    subject,
    body,
    to_emails,
    cc_emails=None,
    attachments=None,
    parent_message_id=None
):
    """
    Sends an outgoing email using EmailMessage, injecting In-Reply-To and References
    headers if parent_message_id is supplied, and records the message in RFQEmailMessage.
    """
    to_list = [e.strip() for e in (to_emails or []) if e and e.strip()]
    cc_list = [e.strip() for e in (cc_emails or []) if e and e.strip()]

    domain = settings.DEFAULT_FROM_EMAIL.split('@')[-1] if '@' in settings.DEFAULT_FROM_EMAIL else 'mbt-corporation.com'
    msg_id = f"<{uuid.uuid4().hex}.rfq-{rfq.id}@{domain}>"

    headers = {
        'Message-ID': msg_id,
    }

    references_str = ""
    if parent_message_id:
        parent_msg_id_clean = parent_message_id.strip()
        if not parent_msg_id_clean.startswith('<'):
            parent_msg_id_clean = f"<{parent_msg_id_clean}>"
        headers['In-Reply-To'] = parent_msg_id_clean

        parent_record = RFQEmailMessage.objects.filter(message_id=parent_message_id).first()
        if parent_record and parent_record.references:
            references_str = f"{parent_record.references} {parent_msg_id_clean}".strip()
        else:
            references_str = parent_msg_id_clean
        headers['References'] = references_str

        if not subject.lower().startswith('re:'):
            subject = f"Re: {subject}"

    email_obj = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to_list,
        cc=cc_list,
        headers=headers
    )

    attachment_names_list = []
    if attachments:
        for att in attachments:
            if isinstance(att, tuple):
                email_obj.attach(*att)
                attachment_names_list.append(att[0])
            elif hasattr(att, 'name') and hasattr(att, 'read'):
                filename = att.name
                content = att.read()
                content_type = getattr(att, 'content_type', 'application/octet-stream')
                email_obj.attach(filename, content, content_type)
                attachment_names_list.append(filename)

    email_obj.send(fail_silently=False)

    # Record sent email in DB
    email_record = RFQEmailMessage.objects.create(
        rfq=rfq,
        message_id=msg_id,
        in_reply_to=headers.get('In-Reply-To'),
        references=references_str or None,
        sender=settings.DEFAULT_FROM_EMAIL,
        recipients=", ".join(to_list),
        cc_recipients=", ".join(cc_list) if cc_list else "",
        subject=subject,
        body=body,
        direction='OUT',
        sent_at=timezone.now(),
        has_attachments=bool(attachment_names_list),
        attachment_names=", ".join(attachment_names_list) if attachment_names_list else ""
    )

    return email_record


def sync_rfq_inbox(rfq=None):
    """
    Connects to IMAP server, fetches inbox messages, and matches them to RFQs.
    Returns (synced_count, error_message).
    """
    imap_host = getattr(settings, 'EMAIL_IMAP_HOST', 'imap.hostinger.com')
    imap_port = int(getattr(settings, 'EMAIL_IMAP_PORT', 993))
    imap_user = getattr(settings, 'EMAIL_HOST_USER', '')
    imap_password = getattr(settings, 'EMAIL_HOST_PASSWORD', '')

    if not imap_user or not imap_password:
        return 0, "Hostinger email credentials not configured in settings."

    synced_count = 0
    mail = None

    try:
        mail = imaplib.IMAP4_SSL(imap_host, imap_port, timeout=15)
        mail.login(imap_user, imap_password)
        mail.select('INBOX')

        if rfq and rfq.rfq_no:
            search_query = f'OR SUBJECT "{rfq.rfq_no}" BODY "{rfq.rfq_no}"'
            status, data = mail.search(None, search_query)
            if status != 'OK' or not data[0]:
                status, data = mail.search(None, 'ALL')
        else:
            status, data = mail.search(None, 'ALL')

        if status != 'OK' or not data[0]:
            return 0, None

        msg_nums = data[0].split()
        recent_nums = msg_nums[-50:]

        for num in recent_nums:
            status, msg_data = mail.fetch(num, '(RFC822)')
            if status != 'OK' or not msg_data:
                continue

            raw_email = msg_data[0][1]
            if not isinstance(raw_email, bytes):
                continue

            msg = email.message_from_bytes(raw_email)

            message_id = _clean_header_str(msg.get('Message-ID'))
            if not message_id:
                message_id = f"<imap-{num.decode()}@{imap_host}>"
            
            if not message_id.startswith('<'):
                message_id = f"<{message_id}>"

            if RFQEmailMessage.objects.filter(message_id=message_id).exists():
                continue

            subject = _clean_header_str(msg.get('Subject'))
            from_str = _clean_header_str(msg.get('From'))
            to_str = _clean_header_str(msg.get('To'))
            cc_str = _clean_header_str(msg.get('Cc'))
            in_reply_to = _clean_header_str(msg.get('In-Reply-To'))
            references = _clean_header_str(msg.get('References'))

            date_str = msg.get('Date')
            sent_at = timezone.now()
            if date_str:
                try:
                    dt = parsedate_to_datetime(date_str)
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt)
                    sent_at = dt
                except Exception:
                    pass

            body = _extract_email_body(msg)

            matched_rfq = rfq
            if not matched_rfq:
                match = re.search(r'RFQ-\d{4}-\d+', f"{subject} {body}", re.IGNORECASE)
                if match:
                    rfq_no_found = match.group(0).upper()
                    matched_rfq = RFQ.objects.filter(rfq_no__iexact=rfq_no_found).first()

            if not matched_rfq and in_reply_to:
                parent = RFQEmailMessage.objects.filter(message_id=in_reply_to).first()
                if parent:
                    matched_rfq = parent.rfq

            if not matched_rfq:
                sender_email = parseaddr(from_str)[1].lower().strip()
                if sender_email:
                    matched_rfq = RFQ.objects.filter(customer__email__iexact=sender_email).first()

            if not matched_rfq:
                continue

            has_attachments = False
            attachment_names = []
            if msg.is_multipart():
                for part in msg.walk():
                    disposition = str(part.get("Content-Disposition"))
                    if "attachment" in disposition:
                        has_attachments = True
                        filename = part.get_filename()
                        if filename:
                            attachment_names.append(_clean_header_str(filename))

            RFQEmailMessage.objects.create(
                rfq=matched_rfq,
                message_id=message_id,
                in_reply_to=in_reply_to or None,
                references=references or None,
                sender=from_str,
                recipients=to_str,
                cc_recipients=cc_str,
                subject=subject or "(No Subject)",
                body=body,
                direction='IN',
                sent_at=sent_at,
                has_attachments=has_attachments,
                attachment_names=", ".join(attachment_names) if attachment_names else ""
            )
            synced_count += 1

        mail.logout()
        return synced_count, None

    except imaplib.IMAP4.error as err:
        logger.warning(f"IMAP Auth/Search Error: {err}")
        return 0, f"IMAP Error: {err}"
    except Exception as exc:
        logger.warning(f"IMAP Sync Error: {exc}")
        return 0, f"Sync Error: {str(exc)}"
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass
