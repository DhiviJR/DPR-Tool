import re
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from .forms import EmailPasteForm, ReviewForm
from .models import EmailRecord
from .services.classifier_service import get_classifier
from .services.imap_reader import fetch_all_messages


NON_CUSTOMER_SENDER_PATTERNS = [
    '@mesinstruments.co.in', '@mesinstruments.com',
    '@mail.instagram.com', '@facebookmail.com', '@alerts.actcorp.in',
    '@egsindia.com', '@mail.sap.com', '@meshmixmedia.com'
]

@login_required
def dashboard(request):
    selected_category = request.GET.get('category', '')
    if selected_category == 'ENQUIRY':
        selected_category = 'QUOTATION_REQUEST'

    non_cust_q = Q()
    for pat in NON_CUSTOMER_SENDER_PATTERNS:
        non_cust_q |= Q(sender__icontains=pat)

    records = EmailRecord.objects.order_by('-received_at', '-id')
    total_emails_count = records.count()

    if selected_category == 'QUOTATION_REQUEST':
        records = records.filter(
            (Q(ai_category__in=['QUOTATION_REQUEST', 'ENQUIRY']) & ~non_cust_q) |
            Q(final_category__in=['QUOTATION_REQUEST', 'ENQUIRY'])
        )
    elif selected_category == 'CUSTOMER_ORDER':
        records = records.filter(
            (Q(ai_category='CUSTOMER_ORDER') & ~non_cust_q) |
            Q(final_category='CUSTOMER_ORDER')
        )
    elif selected_category in EmailRecord.Category.values:
        records = records.filter(Q(ai_category=selected_category) | Q(final_category=selected_category))
    else:
        selected_category = ''

    counts_raw = {item['ai_category']: item['total'] for item in EmailRecord.objects.values('ai_category').annotate(total=Count('id'))}
    category_order = [
        'CUSTOMER_ORDER',
        'PAYMENT_INVOICE',
        'QUOTATION_REQUEST',
        'OTHERS',
        'SUPPORT_COMPLAINT',
    ]
    counts = {cat: counts_raw.get(cat, 0) for cat in category_order}

    enquiry_qs = EmailRecord.objects.filter(
        (Q(ai_category__in=['QUOTATION_REQUEST', 'ENQUIRY']) & ~non_cust_q) |
        Q(final_category__in=['QUOTATION_REQUEST', 'ENQUIRY'])
    )
    enquiry_total_count = enquiry_qs.count()
    enquiry_added_count = enquiry_qs.filter(is_added_to_rfq=True).count()
    enquiry_pending_count = enquiry_total_count - enquiry_added_count
    counts['QUOTATION_REQUEST'] = enquiry_total_count

    cust_order_qs = EmailRecord.objects.filter(
        (Q(ai_category='CUSTOMER_ORDER') & ~non_cust_q) |
        Q(final_category='CUSTOMER_ORDER')
    )
    counts['CUSTOMER_ORDER'] = cust_order_qs.count()

    return render(request, 'email_classifier/dashboard.html', {
        'records': records[:100],
        'counts': counts,
        'selected_category': selected_category,
        'total_emails_count': total_emails_count,
        'enquiry_total_count': enquiry_total_count,
        'enquiry_added_count': enquiry_added_count,
        'enquiry_pending_count': enquiry_pending_count,
    })


@login_required
def toggle_enquiry_status(request, record_id):
    if request.method == 'POST':
        record = get_object_or_404(EmailRecord, pk=record_id)
        record.is_added_to_rfq = not record.is_added_to_rfq
        record.save(update_fields=['is_added_to_rfq'])
        if record.is_added_to_rfq:
            messages.success(request, f'Enquiry "{record.subject[:50]}" marked as Added to RFQ.')
        else:
            messages.info(request, f'Enquiry "{record.subject[:50]}" marked as Pending addition.')
    referer = request.META.get('HTTP_REFERER')
    if referer:
        return redirect(referer)
    return redirect('email_classifier:dashboard')


@login_required
def sync_inbox(request):
    if request.method == 'POST':
        try:
            messages_list, inbox_total = fetch_all_messages(offset=0, limit=25)
            classifier = get_classifier()
            saved = 0
            skipped = 0
            
            uids_in_batch = [m['uid'] for m in messages_list if m.get('uid')]
            existing_uids = set(EmailRecord.objects.filter(imap_uid__in=uids_in_batch).values_list('imap_uid', flat=True))

            for message in messages_list:
                if message['uid'] in existing_uids:
                    skipped += 1
                    continue
                try:
                    result = classifier.classify(
                        subject=message['subject'], body=message['body'], sender=message['sender'],
                    )
                    EmailRecord.objects.create(
                        sender=message['sender'], subject=message['subject'], body=message['body'],
                        source='imap', imap_uid=message['uid'],
                        received_at=message.get('date'),
                        ai_category=result['category'],
                        confidence=result['confidence'],
                        reason=result['reason'],
                        important_details=result['important_details'],
                    )
                    saved += 1
                except Exception:
                    pass
            messages.success(request, f'Inbox synced successfully. {saved} new email(s) imported from info@mesinstruments.co.in.')
        except Exception as exc:
            messages.error(request, f'Inbox sync failed: {exc}')
    return redirect('email_classifier:dashboard')


@login_required
def classify_email(request):
    if request.method == 'POST':
        form = EmailPasteForm(request.POST)
        if form.is_valid():
            try:
                result = get_classifier().classify(**form.cleaned_data)
            except Exception as exc:
                messages.error(request, f'Could not classify the email: {exc}')
            else:
                EmailRecord.objects.create(
                    **form.cleaned_data,
                    ai_category=result['category'],
                    confidence=result['confidence'],
                    reason=result['reason'],
                    important_details=result['important_details'],
                )
                messages.success(request, 'Email classified and saved.')
                return redirect('email_classifier:dashboard')
    else:
        form = EmailPasteForm()
    return render(request, 'email_classifier/classify.html', {'form': form})


@login_required
def review_email(request, record_id):
    record = get_object_or_404(EmailRecord, pk=record_id)
    if request.method == 'POST':
        form = ReviewForm(request.POST, instance=record)
        if form.is_valid():
            form.save()
            messages.success(request, 'Email review saved successfully.')
            return redirect('email_classifier:dashboard')
    else:
        form = ReviewForm(instance=record)
    return render(request, 'email_classifier/review.html', {'form': form, 'record': record})


def _extract_product_name(subject, body):
    subject = (subject or '').strip()
    body = (body or '').strip()

    # 1. Search for explicit 'Product Name :' or 'Product :' or 'Item :' in body
    prod_match = re.search(r'(?i)(?:product\s*name|product\s*description|item\s*description|item|part\s*name)\s*[:\-]\s*([^\r\n]+)', body)
    if prod_match:
        extracted = prod_match.group(1).strip()
        extracted = re.sub(r'^[\>\*\#\-\s]+', '', extracted).strip()
        if len(extracted) >= 2:
            return extracted

    # 2. Search body for 'quotation for <product>' or 'quote for <product>' or 'price for <product>'
    for_match = re.search(r'(?i)(?:quotation|quote|price|enquiry|rfq)\s+(?:for|of)\s+([^\r\n,.]+)', body)
    if for_match:
        extracted = for_match.group(1).strip()
        extracted = re.sub(r'^[\>\*\#\-\s]+', '', extracted).strip()
        if len(extracted) >= 3 and extracted.lower() not in ('below', 'following', 'attached', 'mentioned', 'us', 'me'):
            return extracted

    # 3. Clean subject
    clean_subj = subject
    clean_subj = re.sub(r'^(re|fwd|fw)[:\-\s]+', '', clean_subj, flags=re.IGNORECASE).strip()
    clean_subj = re.sub(r'^(rfq|enquiry|inquiry)[:\-\s]+', '', clean_subj, flags=re.IGNORECASE).strip()
    clean_subj = re.sub(r'^(?:[Øø]?\s*\d+(?:\.\d+)?(?:\s*mm|\s*cm)?[\s\-_]+)+', '', clean_subj, flags=re.IGNORECASE).strip()

    generic_terms = {'inquiry', 'enquiry', 'rfq', 'quote', 'quotation', 'price', 'request', 'quotation request', 'price details', 'need price', 'details'}
    if clean_subj.lower() not in generic_terms and len(clean_subj) >= 3:
        return clean_subj

    # 4. Search body lines for product keywords
    for line in body.splitlines():
        line_clean = re.sub(r'^\s*[\>\*\#\-\u2022\d\.\)]+\s*', '', line).strip()
        if not line_clean or line_clean.lower() in ('sir', 'dear sir', 'thanks & regards', 'regards'):
            continue
        if re.search(r'(?i)\b(gauge|plug|ring|unit|spc|lvdt|air|tpg|trg|apg|arg|comparator|pin|block|stand|amc|service|spares)\b', line_clean):
            if not line_clean.lower().startswith('kindly') and not line_clean.lower().startswith('please'):
                return line_clean

    return clean_subj or subject


def _extract_product_type(pname, body_text):
    text = f"{pname} {body_text}".lower()
    if 'air plug' in text or 'apg' in text: return 'APG'
    if 'air ring' in text or 'arg' in text: return 'ARG'
    if 'multi-gauge' in text or 'multi gauge' in text: return 'Multi-Gauge'
    if 'unit std air' in text or 'air unit' in text or 'air gauge unit' in text: return 'unit Std Air'
    if 'unit spc air' in text: return 'unit SPC Air'
    if 'unit std lvdt' in text or 'lvdt' in text: return 'unit Std lvdt'
    if 'amc' in text: return 'AMC'
    if 'service' in text: return 'Service'
    if 'spares' in text or 'spare' in text: return 'Spares'
    if 'tpg' in text or 'thread plug' in text: return 'TPG'
    if 'trg' in text or 'thread ring' in text: return 'TRG'
    if 'ppg' in text or 'plain plug' in text: return 'PPG'
    if 'prg' in text or 'plain ring' in text: return 'PRG'
    return ''

def _extract_qty_and_unit(body_text):
    qty = 1
    unit = "No's"
    qty_match = re.search(r'(?i)(?:qty|quantity|nos|num|count)\s*[:\-=\s]+(\d+)', body_text)
    if qty_match:
        qty = int(qty_match.group(1))
    else:
        num_match = re.search(r'\b(\d+)\s*(?:nos|no\'s|sets?|pcs|pieces?)\b', body_text, flags=re.IGNORECASE)
        if num_match:
            qty = int(num_match.group(1))

    if re.search(r'(?i)\b(set|sets)\b', body_text):
        unit = 'Set'
    return qty, unit

def _extract_product_remarks(body_text):
    specs = []
    for line in body_text.splitlines():
        clean = re.sub(r'^\s*[\>\*\#\-\u2022]+\s*', '', line).strip()
        if any(clean.lower().startswith(k) for k in ('size', 'bore', 'jet', 'gauge type', 'accessories', 'along with', 'details')):
            specs.append(clean)
    return ' | '.join(specs) if specs else ''


def _get_clean_email_body(body_text):
    if not body_text:
        return ''
    split_patterns = [
        r'(?i)\r?\n\s*On\s+.*wrote:\s*',
        r'(?i)\r?\n\s*---+\s*Original Message\s*---+',
        r'(?i)\r?\n\s*From:\s+.*',
    ]
    clean_body = body_text
    for pat in split_patterns:
        clean_body = re.split(pat, clean_body)[0]
    return clean_body.strip()


INTERNAL_DOMAINS = {'mesinstruments.co.in', 'mesinstruments.com'}

PUBLIC_DOMAINS = {
    'gmail', 'yahoo', 'outlook', 'hotmail', 'rediffmail', 'mesinstruments',
    'instagram', 'facebook', 'twitter', 'linkedin', 'google', 'apple',
    'actcorp', 'sap', 'alerts', 'facebookmail'
}

GENERIC_SENDER_WORDS = {
    'info', 'sales', 'support', 'admin', 'contact', 'no-reply', 'noreply',
    'purchase', 'purchasedept', 'sew', 'mail', 'service', 'team', 'materials',
    'npd team hosur', 'npd', 'mfg tech', 'quality', 'stdroomunit2', 'vvp - std'
}

GENERIC_COMPANY_WORDS = {
    'engineering', 'solutions', 'works', 'industries', 'technologies', 'pvt',
    'private', 'limited', 'ltd', 'enterprises', 'tools', 'gauges', 'machining',
    'automation', 'mfg', 'manufacturing', 'forgings', 'auto', 'motors', 'group',
    'india', 'systems', 'components', 'products', 'services', 'corp', 'corporation'
}

def _is_own_company(text):
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in ('metrology', 'genau', 'mesinstruments', 'etrology engineering', 'mes_q', 'quotation_mes'))

def _clean_company_name(cname):
    if not cname:
        return ''
    clean = cname.strip(' "\'#*>-_\t\r\n')
    clean = re.sub(r'^(?:po\s+items\s+pending\s+supplies|new\s+po\s*\d*|po\s*copy|po\s*no\s*\d*|for|m/s|m/s\.|m_s_|request|requst|requirement|quote|quotation|rfq|enquiry|inquiry)[:\-\s_]+', '', clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r'_(Quotation|MES|Quote).*', '', clean, flags=re.IGNORECASE).replace('_', ' ').strip()
    clean = re.sub(r'[\-\s]+(Padappai|Ranipet|Hosur|Chennai|Plant\s*\d*)$', '', clean, flags=re.IGNORECASE).strip()
    if _is_own_company(clean):
        return ''
    return clean

def _clean_sender_name(sender_name):
    if not sender_name:
        return ''
    name = sender_name.strip(' "\'')
    name = re.sub(r'^(re|fwd|fw)[:\-\s]+', '', name, flags=re.IGNORECASE).strip()
    name = re.sub(r'^\d+[\s\-_]+', '', name).strip()
    name = re.sub(r'^(?:materials[-\d]*|dept|purchase|sales|quality|npd)\s+', '', name, flags=re.IGNORECASE).strip()

    parts = re.split(r'\s*[\-\|/()\[\]]\s*', name)
    if parts:
        first = parts[0].strip()
        if len(first) >= 2 and first.lower() not in GENERIC_SENDER_WORDS:
            return first
    return name if name.lower() not in GENERIC_SENDER_WORDS else ''

def _extract_person_name(body_text):
    if not body_text:
        return ''
    clean_body = _get_clean_email_body(body_text)
    lines = clean_body.splitlines()
    in_sig = False
    for line in lines:
        clean = line.strip(' *,#>-_\t\r\n\'"[]()')
        if re.search(r'(?i)(?:thanks\s*&\s*regards|regards|best\s*regards|warm\s*regards|cheers|thanks)', clean):
            in_sig = True
            continue
        if in_sig:
            if not clean or clean.startswith('http') or clean.startswith('www') or '@' in clean or 'image' in clean.lower() or clean.lower().startswith(('wrote:', 'on ', 're:', 'fwd:', 'for ')):
                continue
            if not any(c.isalpha() for c in clean):
                continue
            if re.search(r'[\:\=\<\>\|]', clean) or clean.startswith('--') or clean.startswith('__'):
                continue
            if re.search(r'(?i)\b(gauge|material|carbide|dia|drawing|spec|pcs|nos|no|serial|sl|part|item|concept|head\s+office|address|street|road|city|state|pin|gst|phone|tel|mob|fax|email|bank|bankers|necessary|quote|offer|rfq|po|note|disclaimer|confidential)\b', clean):
                continue
            if re.search(r'(?i)\b(manager|lead|engineer|dept|department|quality|npd|purchase|sales|sourcing|technologies|solutions|works|ltd|pvt|private|limited|industries|inc|corp|llp|team|office|co\.?)\b', clean):
                continue
            if len(clean) >= 2 and len(clean) <= 35:
                return clean
    return ''

def _extract_company_and_region(body_text, subject_text, sender_name, sender_email):
    clean_body = _get_clean_email_body(body_text)
    company_name = ''
    region = ''
    combined_text = f"{sender_name or ''}\n{subject_text or ''}\n{clean_body}"

    regions = ['Hosur', 'Chennai', 'Coimbatore', 'Bangalore', 'Hyderabad', 'Pune', 'Delhi', 'Trichy', 'Madurai', 'Kolkata', 'Mumbai']
    for reg in regions:
        if re.search(rf'\b{reg}\b', combined_text, re.IGNORECASE):
            region = reg
            break

    company_keywords = r'(?:Engineering\s+Solutions|Engineering\s+Works|Industries|Technologies|Pvt\s+Ltd|Private\s+Limited|Limited|Ltd|Enterprises|Solutions|Tools|Gauges|Machining|Automation|Works|Mfg|Manufacturing|Forgings)'
    for line in combined_text.splitlines():
        line_clean = line.strip(' *,#>\r\n\t')
        comp_match = re.search(rf'([A-Za-z0-9\s&\._\-]{{3,50}}\b{company_keywords}\b)', line_clean, re.IGNORECASE)
        if comp_match:
            cname = _clean_company_name(comp_match.group(1))
            if cname and not cname.lower().startswith(('i have', 'we are', 'please', 'kindly', 'dear', 'thanks', 'regards', 'our', 'your', 'this', 'on ')):
                company_name = cname
                break

    domain_fallback = False
    if not company_name and sender_email and '@' in sender_email:
        domain = sender_email.split('@')[1].split('.')[0]
        if domain.lower() not in PUBLIC_DOMAINS:
            company_name = domain.replace('-', ' ').replace('_', ' ').title()
            domain_fallback = True

    return company_name, region, domain_fallback


GENERIC_SUBJECTS = {
    'inquiry', 'enquiry', 'rfq', 'quote', 'quotation', 'price', 'request',
    'quotation request', 'price details', 'need price', 'details', 'open order',
    'order', 're', 'fwd', 'fw', 'need quote', 'concept', 'reg', 'request for quotation'
}

def _simplify_product_title(text):
    if not text:
        return ''
    t = text.strip(' "\'#*>-_\t\r\n')

    dia_match = re.search(r'(?i)(?:dia|Ø|size|size\s*:)\s*[Øø]?\s*(\d+(?:\.\d+)?)', t)
    dia_str = f" Ø{dia_match.group(1)}mm" if dia_match else ""

    if re.search(r'(?i)\bcarbide\s+air\s+plug\b|\bcarbide\s+air\s+plug\s+gauge\b', t):
        return f"Carbide Air Plug Gauge{dia_str}"
    if re.search(r'(?i)\bair\s+ring\s+gauge\b|\barg\b', t):
        return f"Air Ring Gauge{dia_str}"
    if re.search(r'(?i)\bair\s+plug\s+gauge\b|\bapg\b', t):
        return f"Air Plug Gauge{dia_str}"
    if re.search(r'(?i)\bthread\s+plug\s+gauge\b|\btpg\b', t):
        return "Thread Plug Gauge"
    if re.search(r'(?i)\bthread\s+ring\s+gauge\b|\btrg\b', t):
        return "Thread Ring Gauge"
    if re.search(r'(?i)\bplain\s+plug\s+gauge\b|\bppg\b', t):
        return "Plain Plug Gauge"
    if re.search(r'(?i)\bplain\s+ring\s+gauge\b|\bprg\b', t):
        return "Plain Ring Gauge"
    if re.search(r'(?i)\bmulti[- ]gauge\b', t):
        return "Multi-Gauge"
    if re.search(r'(?i)\bcomparator\s+stand\b', t):
        return "Comparator Stand"
    if re.search(r'(?i)\bair\s+(?:gauge\s+)?unit\b', t):
        return "Air Gauge Unit"
    if re.search(r'(?i)\bspc\s+(?:gauge|unit)\b|\bspc\b', t):
        return "SPC Gauge"

    clean_t = re.sub(r'^(re|fwd|fw|rfq|enquiry|inquiry)[:\-\s_]+', '', t, flags=re.IGNORECASE).strip()
    return clean_t[:50].strip()

def _extract_product_name(subject, body):
    subject = (subject or '').strip()
    body = (body or '').strip()

    # 1. Search full body for explicit 'Product Name :' or 'Product :' or 'Item :'
    prod_match = re.search(r'(?i)(?:product\s*name|product\s*description|item\s*description|part\s*name)\s*[:\-]\s*([^\r\n]+)', body)
    if prod_match:
        extracted = prod_match.group(1).strip(' *,#>\r\n\t')
        if len(extracted) >= 2:
            return _simplify_product_title(extracted)

    # 2. Search body for 'quotation for <product>' or 'requirement of <product>'
    for_match = re.search(r'(?i)(?:quotation|quote|price|enquiry|rfq|requirement|req)\s+(?:for|of)\s+([^\r\n,.]+)', f"{subject}\n{body}")
    if for_match:
        extracted = for_match.group(1).strip(' *,#>\r\n\t')
        if len(extracted) >= 3 and not any(w in extracted.lower() for w in ('below', 'following', 'attached', 'mentioned', 'us', 'me', 'the mentioned rfq')):
            return _simplify_product_title(extracted)

    # 3. Search body lines for product keywords (Air Plug Gauge, Air Ring Gauge, LVDT, etc.)
    for line in body.splitlines():
        line_clean = re.sub(r'^\s*[\>\*\#\-\u2022\d\.\)]+\s*', '', line).strip()
        if not line_clean or line_clean.lower().startswith(('sir', 'dear sir', 'thanks', 'regards', 'kindly', 'please')):
            continue
        if re.search(r'(?i)\b(gauge|plug|ring|unit|spc|lvdt|air|tpg|trg|apg|arg|comparator|pin|block|stand|amc|service|spares)\b', line_clean):
            title = _simplify_product_title(line_clean)
            if title and title.lower() not in GENERIC_SUBJECTS and not title.lower().startswith(('for ', 'below', '<!doctype')):
                return title

    # 4. Check clean subject if not generic and not company name
    clean_subj = re.sub(r'^(re|fwd|fw|rfq|enquiry|inquiry)[:\-\s_]+', '', subject, flags=re.IGNORECASE).strip()
    company_or_doc = re.search(r'(?:Engineering|Forgings|Industries|Technologies|Pvt\s+Ltd|Private\s+Limited|Ltd|Quotation_MES|MES_Q|PO\s+Copy|Purchase\s+Order)', clean_subj, flags=re.IGNORECASE)
    
    if clean_subj.lower() not in GENERIC_SUBJECTS and not company_or_doc and len(clean_subj) >= 3:
        return _simplify_product_title(clean_subj)

    return 'Air Plug Gauge'


@login_required
def add_rfq_from_email(request, record_id):
    from urllib.parse import urlencode
    from django.urls import reverse
    from email.utils import parseaddr
    import re
    from customers.models import Customer
    from django.utils import timezone

    record = get_object_or_404(EmailRecord, pk=record_id)
    sender_raw = record.sender or ''
    sender_name, sender_email = parseaddr(sender_raw)
    sender_email = sender_email.strip().lower()
    clean_sname = _clean_sender_name(sender_name)

    ext_person_name = _extract_person_name(record.body)
    ext_company_name, ext_region, is_domain_fb = _extract_company_and_region(record.body, record.subject, sender_name, sender_email)

    if ext_person_name:
        target_cust_name = ext_person_name
    elif ext_company_name and not is_domain_fb:
        target_cust_name = ext_company_name
    elif clean_sname:
        target_cust_name = clean_sname
    elif ext_company_name:
        target_cust_name = ext_company_name
    else:
        target_cust_name = clean_sname or 'New Customer'

    customer = None
    if sender_email and not any(sender_email.endswith('@' + d) for d in INTERNAL_DOMAINS):
        customer = Customer.objects.filter(email__iexact=sender_email).first()

    if not customer and target_cust_name:
        customer = Customer.objects.filter(customer_name__iexact=target_cust_name).first()

    if not customer and target_cust_name:
        customer = Customer.objects.filter(customer_name__icontains=target_cust_name).first()

    if not customer and clean_sname:
        customer = Customer.objects.filter(customer_name__icontains=clean_sname).first()

    if not customer and ext_person_name:
        customer = Customer.objects.filter(customer_name__icontains=ext_person_name).first()

    if not customer and target_cust_name:
        words = [w for w in target_cust_name.split() if len(w) >= 2 and w.lower() not in GENERIC_COMPANY_WORDS and w.lower() not in GENERIC_SENDER_WORDS]
        if words:
            sig_phrase = ' '.join(words[:2])
            customer = Customer.objects.filter(customer_name__icontains=sig_phrase).first()

    if not customer and clean_sname:
        for word in clean_sname.split():
            if len(word) >= 3 and word.lower() not in GENERIC_SENDER_WORDS and word.lower() not in GENERIC_COMPANY_WORDS:
                customer = Customer.objects.filter(customer_name__icontains=word).first()
                if customer:
                    break

    phone_match = re.search(r'\b[6-9]\d{9}\b', f"{record.subject} {record.body}")
    extracted_phone = phone_match.group(0) if phone_match else ''

    if customer:
        updated = False
        if ext_person_name and customer.customer_name != ext_person_name:
            customer.customer_name = ext_person_name
            updated = True
        elif customer.customer_name.lower() in ('new customer', 'unknown', 'sew', '') and target_cust_name:
            customer.customer_name = target_cust_name
            updated = True
        if ext_region and not customer.region:
            customer.region = ext_region
            updated = True
        if updated:
            customer.save()
    else:
        customer = Customer.objects.create(
            customer_name=target_cust_name or 'New Customer',
            email=sender_email if not any(sender_email.endswith('@' + d) for d in INTERNAL_DOMAINS) else None,
            phone_number=extracted_phone or None,
            region=ext_region or None,
            state_code='33' if ext_region in ('Chennai', 'Hosur') else None,
            is_sez='No',
        )

    clean_product_name = _extract_product_name(record.subject, record.body)
    product_type = _extract_product_type(clean_product_name, record.body)
    qty, unit = _extract_qty_and_unit(record.body)
    product_remarks = _extract_product_remarks(record.body)

    mail_date_str = record.received_at.strftime('%Y-%m-%d') if record.received_at else timezone.localdate().strftime('%Y-%m-%d')

    if customer:
        url = reverse('rfq_details')
        params = urlencode({
            'add_rfq': '1',
            'from_email_id': record.id,
            'customer_id': customer.id,
            'mail_date': mail_date_str,
            'enquiry_details': record.subject,
            'region': customer.region or ext_region or '',
            'product_name': clean_product_name,
            'product_type': product_type,
            'quantity': qty,
            'unit': unit,
            'product_remarks': product_remarks,
        })
        return redirect(f"{url}?{params}")

    clean_product_name = _extract_product_name(record.subject, record.body)
    product_type = _extract_product_type(clean_product_name, record.body)
    qty, unit = _extract_qty_and_unit(record.body)
    product_remarks = _extract_product_remarks(record.body)

    mail_date_str = record.received_at.strftime('%Y-%m-%d') if record.received_at else timezone.localdate().strftime('%Y-%m-%d')

    if customer:
        url = reverse('rfq_details')
        params = urlencode({
            'add_rfq': '1',
            'from_email_id': record.id,
            'customer_id': customer.id,
            'mail_date': mail_date_str,
            'enquiry_details': record.subject,
            'product_name': clean_product_name,
            'product_type': product_type,
            'quantity': qty,
            'unit': unit,
            'product_remarks': product_remarks,
        })
        return redirect(f"{url}?{params}")
    else:
        phone_match = re.search(r'\b[6-9]\d{9}\b', f"{record.subject} {record.body}")
        extracted_phone = phone_match.group(0) if phone_match else ''

        cust_name = sender_name.strip()
        if not cust_name and sender_email:
            parts = sender_email.split('@')
            domain = parts[1].split('.')[0]
            cust_name = domain.replace('-', ' ').replace('_', ' ').title()

        url = reverse('customer_details')
        params = urlencode({
            'add_customer': '1',
            'from_email_id': record.id,
            'customer_name': cust_name or 'New Customer',
            'email': sender_email,
            'phone_number': extracted_phone,
            'mail_date': mail_date_str,
            'enquiry_details': record.subject,
            'product_name': clean_product_name or record.subject,
            'product_type': product_type,
            'quantity': qty,
            'unit': unit,
            'product_remarks': product_remarks,
        })
        messages.info(request, f'Customer not found for "{sender_email or record.sender}". Known details auto-filled below. Add customer to proceed with RFQ.')
        return redirect(f"{url}?{params}")


@login_required
def add_po_from_email(request, record_id):
    from urllib.parse import urlencode
    from django.urls import reverse
    from email.utils import parseaddr
    import re
    from customers.models import Customer
    from django.utils import timezone

    record = get_object_or_404(EmailRecord, pk=record_id)
    sender_raw = record.sender or ''
    sender_name, sender_email = parseaddr(sender_raw)
    sender_email = sender_email.strip().lower()
    clean_sname = _clean_sender_name(sender_name)

    ext_person_name = _extract_person_name(record.body)
    ext_company_name, ext_region, is_domain_fb = _extract_company_and_region(record.body, record.subject, sender_name, sender_email)

    if ext_person_name:
        target_cust_name = ext_person_name
    elif ext_company_name and not is_domain_fb:
        target_cust_name = ext_company_name
    elif clean_sname:
        target_cust_name = clean_sname
    elif ext_company_name:
        target_cust_name = ext_company_name
    else:
        target_cust_name = clean_sname or 'New Customer'

    customer = None
    if sender_email and not any(sender_email.endswith('@' + d) for d in INTERNAL_DOMAINS):
        customer = Customer.objects.filter(email__iexact=sender_email).first()

    if not customer and target_cust_name:
        customer = Customer.objects.filter(customer_name__iexact=target_cust_name).first()

    if not customer and target_cust_name:
        customer = Customer.objects.filter(customer_name__icontains=target_cust_name).first()

    if not customer and clean_sname:
        customer = Customer.objects.filter(customer_name__icontains=clean_sname).first()

    if not customer and ext_person_name:
        customer = Customer.objects.filter(customer_name__icontains=ext_person_name).first()

    if not customer and target_cust_name:
        words = [w for w in target_cust_name.split() if len(w) >= 2 and w.lower() not in GENERIC_COMPANY_WORDS and w.lower() not in GENERIC_SENDER_WORDS]
        if words:
            sig_phrase = ' '.join(words[:2])
            customer = Customer.objects.filter(customer_name__icontains=sig_phrase).first()

    if not customer and clean_sname:
        for word in clean_sname.split():
            if len(word) >= 3 and word.lower() not in GENERIC_SENDER_WORDS and word.lower() not in GENERIC_COMPANY_WORDS:
                customer = Customer.objects.filter(customer_name__icontains=word).first()
                if customer:
                    break

    phone_match = re.search(r'\b[6-9]\d{9}\b', f"{record.subject} {record.body}")
    extracted_phone = phone_match.group(0) if phone_match else ''

    if customer:
        updated = False
        if ext_person_name and customer.customer_name != ext_person_name:
            customer.customer_name = ext_person_name
            updated = True
        elif customer.customer_name.lower() in ('new customer', 'unknown', 'sew', '') and target_cust_name:
            customer.customer_name = target_cust_name
            updated = True
        if ext_region and not customer.region:
            customer.region = ext_region
            updated = True
        if updated:
            customer.save()
    else:
        customer = Customer.objects.create(
            customer_name=target_cust_name or 'New Customer',
            email=sender_email if not any(sender_email.endswith('@' + d) for d in INTERNAL_DOMAINS) else None,
            phone_number=extracted_phone or None,
            region=ext_region or None,
            state_code='33' if ext_region in ('Chennai', 'Hosur') else None,
            is_sez='No',
        )

    po_date_str = record.received_at.strftime('%Y-%m-%d') if record.received_at else timezone.localdate().strftime('%Y-%m-%d')

    po_no = ''
    po_matches = re.findall(r'(?i)(?:po\s*no|po\s*num|po\s*number|po\s*#|purchase\s*order\s*no|po\s*copy|po|por)[:\-\#\s]+([A-Za-z0-9\/\-_]{3,30})', f"{record.subject} {record.body}")
    for match in po_matches:
        match_clean = match.strip(' .,;:')
        if any(char.isdigit() for char in match_clean) and match_clean.lower() not in ('copy', 'items', 'pending', 'printout', 'for', 'copy-reg'):
            po_no = match_clean
            break

    clean_product_name = _extract_product_name(record.subject, record.body)
    product_type = _extract_product_type(clean_product_name, record.body)
    qty, unit = _extract_qty_and_unit(record.body)
    product_remarks = _extract_product_remarks(record.body)

    rate = ''
    rate_match = re.search(r'(?i)(?:rate|price|unit\s*price|val|value|amount)\s*[:\-=\s]+\$?₹?\s*(\d+(?:\.\d+)?)', record.body or '')
    if rate_match:
        rate = rate_match.group(1)

    url = reverse('customer_order')
    params = urlencode({
        'add_po': '1',
        'from_email_id': record.id,
        'customer_id': customer.id,
        'region': customer.region or ext_region or '',
        'po_number': po_no,
        'po_date': po_date_str,
        'product_name': clean_product_name,
        'product_type': product_type,
        'quantity': qty,
        'unit': unit,
        'rate': rate,
        'product_remarks': product_remarks,
    })
    return redirect(f"{url}?{params}")


