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

    # If rfq is None, attempt to resolve it from parent message or subject/body
    if not rfq and parent_message_id:
        parent_record = RFQEmailMessage.objects.filter(message_id=parent_message_id).first()
        if parent_record and parent_record.rfq:
            rfq = parent_record.rfq

    if not rfq:
        rfq = _match_email_to_rfq(
            subject=subject,
            body=body,
            from_str=settings.DEFAULT_FROM_EMAIL,
            to_str=", ".join(to_list),
            in_reply_to=parent_message_id
        )

    if not rfq:
        rfq = RFQ.objects.order_by('-id').first()

    domain = settings.DEFAULT_FROM_EMAIL.split('@')[-1] if '@' in settings.DEFAULT_FROM_EMAIL else 'mbt-corporation.com'
    rfq_tag = f"rfq-{rfq.id}" if rfq else f"msg-{uuid.uuid4().hex[:8]}"
    msg_id = f"<{uuid.uuid4().hex}.{rfq_tag}@{domain}>"

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
    email_record = None
    if rfq:
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


SPAM_DOMAINS = ['fiverr.com', 'gst.gov.in', 'atlassian.net', 'atlassian.com', 'peopleperhour.com', 'vyaparapp.in']

def _match_email_to_rfq(subject, body, from_str, to_str, in_reply_to=None, references=None, target_rfq=None):
    """
    Intelligently matches an incoming or outgoing email to an RFQ.
    """
    search_text = f"{subject or ''} {body or ''}"
    sender_email = parseaddr(from_str or '')[1].lower().strip()
    recipient_email = parseaddr(to_str or '')[1].lower().strip()

    # Skip generic marketing / spam emails unless explicitly tied to an RFQ
    if any(domain in sender_email for domain in SPAM_DOMAINS):
        return None

    # 1. Match RFQ Number (e.g. RFQ-2026-0034 or RFQ-2026-34)
    match_rfq = re.search(r'RFQ-\d{4}-\d+|RFQ-\d+', search_text, re.IGNORECASE)
    if match_rfq:
        rfq_no = match_rfq.group(0).upper()
        found = RFQ.objects.filter(rfq_no__iexact=rfq_no).first()
        if found:
            return found

    # 2. Match In-Reply-To or References message ID
    for ref in [in_reply_to, references]:
        if ref:
            clean_ref = ref.strip('<> ')
            parent = RFQEmailMessage.objects.filter(message_id__icontains=clean_ref).exclude(rfq=None).first()
            if parent and parent.rfq:
                return parent.rfq

    # 3. Match Quotation Number (e.g. MES_Q0014/26-27 or MES_Q...)
    match_quote = re.search(r'MES[_\/][A-Za-z0-9_\-\/]+', search_text, re.IGNORECASE)
    if match_quote:
        qno = match_quote.group(0)
        from rfq.models import RFQQuotation
        quotation = RFQQuotation.objects.filter(quotation_number__icontains=qno).first()
        if quotation and quotation.rfq:
            return quotation.rfq

    # 4. Customer Email Match
    from django.db.models import Q
    for email in [sender_email, recipient_email]:
        if email and 'mbt-corporation' not in email and 'hostinger' not in email:
            cust_rfqs = RFQ.objects.filter(
                Q(customer__email__iexact=email) |
                Q(enquiry_details__icontains=email) |
                Q(remarks__icontains=email)
            ).order_by('-created_at')
            if cust_rfqs.exists():
                if target_rfq and target_rfq in cust_rfqs:
                    return target_rfq
                return cust_rfqs.first()

    # 5. Customer Name Match in Subject/Body
    for rfq in RFQ.objects.select_related('customer').order_by('-created_at'):
        if rfq.customer and rfq.customer.customer_name:
            cname = rfq.customer.customer_name.strip()
            if len(cname) >= 3 and cname.lower() in search_text.lower():
                return rfq

    # 6. Target RFQ fallback if target_rfq is supplied
    if target_rfq:
        if target_rfq.customer:
            c_email = (target_rfq.customer.email or '').lower().strip()
            c_name = (target_rfq.customer.customer_name or '').lower().strip()
            if c_email and (c_email in sender_email or c_email in recipient_email or c_email in search_text.lower()):
                return target_rfq
            if c_name and len(c_name) >= 3 and (c_name in search_text.lower() or c_name in (from_str or '').lower()):
                return target_rfq
        if 'rfq' in (subject or '').lower() or (target_rfq.rfq_no and target_rfq.rfq_no.lower() in search_text.lower()):
            return target_rfq

    return None


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

        status, data = mail.search(None, 'ALL')
        if status != 'OK' or not data[0]:
            return 0, None

        msg_nums = data[0].split()
        recent_nums = msg_nums[-150:]  # Scan recent 150 emails in a single batch request

        if recent_nums:
            status, batch_headers = mail.fetch(
                b','.join(recent_nums),
                '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM TO CC IN-REPLY-TO REFERENCES DATE)])'
            )
            if status == 'OK' and batch_headers:
                for item in batch_headers:
                    if not isinstance(item, tuple) or len(item) < 2:
                        continue
                    header_info, raw_headers = item[0], item[1]
                    if not isinstance(raw_headers, bytes):
                        continue

                    num_match = re.search(rb'^\d+', header_info)
                    num = num_match.group(0) if num_match else b''

                    msg_header = email.message_from_bytes(raw_headers)

                    message_id = _clean_header_str(msg_header.get('Message-ID'))
                    if not message_id:
                        message_id = f"<imap-{num.decode()}@{imap_host}>"
                    if not message_id.startswith('<'):
                        message_id = f"<{message_id}>"
                    if len(message_id) > 250:
                        message_id = message_id[:240] + f"-{num.decode()}@msg>"

                    # If already in DB and linked, skip downloading full body
                    existing_msg = RFQEmailMessage.objects.filter(message_id=message_id).first()
                    if existing_msg and existing_msg.rfq:
                        continue

                    subject = _clean_header_str(msg_header.get('Subject'))
                    if subject and len(subject) > 490:
                        subject = subject[:490]

                    from_str = _clean_header_str(msg_header.get('From'))
                    if from_str and len(from_str) > 250:
                        from_str = from_str[:250]

                    to_str = _clean_header_str(msg_header.get('To'))
                    cc_str = _clean_header_str(msg_header.get('Cc'))
                    in_reply_to = _clean_header_str(msg_header.get('In-Reply-To'))
                    if in_reply_to and len(in_reply_to) > 250:
                        in_reply_to = in_reply_to[:240] + ">"
                    references = _clean_header_str(msg_header.get('References'))

                    # Check if headers match any RFQ before downloading full email body
                    matched_rfq = _match_email_to_rfq(
                        subject=subject,
                        body='',
                        from_str=from_str,
                        to_str=to_str,
                        in_reply_to=in_reply_to,
                        references=references,
                        target_rfq=rfq
                    )

                    # Now fetch full RFC822 message
                    status, msg_data = mail.fetch(num, '(RFC822)')
                    if status != 'OK' or not msg_data or not msg_data[0]:
                        continue
                    raw_email = msg_data[0][1]
                    if not isinstance(raw_email, bytes):
                        continue
                    msg = email.message_from_bytes(raw_email)

                    body = _extract_email_body(msg)

                    if not matched_rfq:
                        matched_rfq = _match_email_to_rfq(
                            subject=subject,
                            body=body,
                            from_str=from_str,
                            to_str=to_str,
                            in_reply_to=in_reply_to,
                            references=references,
                            target_rfq=rfq
                        )

                    if not matched_rfq and rfq:
                        matched_rfq = rfq

                    if not matched_rfq:
                        matched_rfq = RFQ.objects.order_by('-id').first()

                    if not matched_rfq:
                        continue

                    if existing_msg:
                        if existing_msg.rfq != matched_rfq:
                            existing_msg.rfq = matched_rfq
                            existing_msg.save(update_fields=['rfq'])
                            synced_count += 1
                        continue

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
