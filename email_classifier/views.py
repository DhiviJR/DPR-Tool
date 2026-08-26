import datetime
import re
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

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

    date_filter = request.GET.get('date_filter', 'all')
    start_date = request.GET.get('start_date', '')
    end_date = request.GET.get('end_date', '')

    non_cust_q = Q()
    for pat in NON_CUSTOMER_SENDER_PATTERNS:
        non_cust_q |= Q(sender__icontains=pat)

    base_records = EmailRecord.objects.all()

    # Smart Date Filtering
    now = timezone.now()
    local_now = timezone.localtime(now)
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    if date_filter == 'today':
        base_records = base_records.filter(received_at__gte=today_start)
    elif date_filter == 'yesterday':
        yesterday_start = today_start - datetime.timedelta(days=1)
        base_records = base_records.filter(received_at__gte=yesterday_start, received_at__lt=today_start)
    elif date_filter == 'week':
        week_start = today_start - datetime.timedelta(days=today_start.weekday())
        base_records = base_records.filter(received_at__gte=week_start)
    elif date_filter == 'month':
        month_start = today_start.replace(day=1)
        base_records = base_records.filter(received_at__gte=month_start)
    elif date_filter == 'custom':
        if start_date:
            try:
                s_date = datetime.datetime.strptime(start_date, '%Y-%m-%d').date()
                s_dt = timezone.make_aware(datetime.datetime.combine(s_date, datetime.time.min))
                base_records = base_records.filter(received_at__gte=s_dt)
            except ValueError:
                pass
        if end_date:
            try:
                e_date = datetime.datetime.strptime(end_date, '%Y-%m-%d').date()
                e_dt = timezone.make_aware(datetime.datetime.combine(e_date, datetime.time.max))
                base_records = base_records.filter(received_at__lte=e_dt)
            except ValueError:
                pass

    records = base_records.order_by('-received_at', '-id')
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

    counts_raw = {item['ai_category']: item['total'] for item in base_records.values('ai_category').annotate(total=Count('id'))}
    category_order = [
        'CUSTOMER_ORDER',
        'PAYMENT_INVOICE',
        'QUOTATION_REQUEST',
        'OTHERS',
        'SUPPORT_COMPLAINT',
    ]
    counts = {cat: counts_raw.get(cat, 0) for cat in category_order}

    enquiry_qs = base_records.filter(
        (Q(ai_category__in=['QUOTATION_REQUEST', 'ENQUIRY']) & ~non_cust_q) |
        Q(final_category__in=['QUOTATION_REQUEST', 'ENQUIRY'])
    )
    enquiry_total_count = enquiry_qs.count()
    enquiry_added_count = enquiry_qs.filter(is_added_to_rfq=True).count()
    enquiry_pending_count = enquiry_total_count - enquiry_added_count
    counts['QUOTATION_REQUEST'] = enquiry_total_count

    cust_order_qs = base_records.filter(
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
        'date_filter': date_filter,
        'start_date': start_date,
        'end_date': end_date,
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
    clean = re.split(r'[\,;\s]+\bformerly\b', clean, flags=re.IGNORECASE)[0].strip()
    clean = re.split(r'\s*\(\s*formerly\b', clean, flags=re.IGNORECASE)[0].strip()
    clean = re.sub(
        r'^(?:our\s+company\s+(?:name\s+)?(?:was\s+)?(?:upgraded|changed|renamed)\s+(?:to\s+)?|company\s+name\s*:?|customer\s+name\s*:?|customer\s*:?|formerly\s+(?:known\s+as\s+)?|fka\b|aka\b|ex\s+name\s*:?|old\s+name\s*:?|new\s+name\s*:?|upgraded\s+to\s+|renamed\s+to\s+|changed\s+to\s+|to\s+|was\s+|as\s+|for\s+|from\s+|scm\s*\|\||m/s\.?|m_s_)\s*',
        '',
        clean,
        flags=re.IGNORECASE
    ).strip()
    clean = re.sub(
        r'^(?:po\s+items\s+pending\s+supplies|new\s+po\s*\d*|po\s*copy|po\s*no\s*\d*|quote|quotation|rfq|enquiry|inquiry|request|requirement|the\s+confirmation\s+from\s+us\s+pls\s+start\s+manufacturing)[:\-\s_]+',
        '',
        clean,
        flags=re.IGNORECASE
    ).strip()
    clean = re.sub(r'_(Quotation|MES|Quote).*', '', clean, flags=re.IGNORECASE).replace('_', ' ').strip()
    clean = re.sub(r'[\-\s,\.]+(Padappai|Ranipet|Hosur|Chennai|Oragadam|Mannur|Plant\s*\d*)$', '', clean, flags=re.IGNORECASE).strip()
    if _is_own_company(clean):
        return ''
    PRODUCT_NOISE = r'(?i)\b(urgent|nil\s+stock|stock|spares|air\s+plug|thread\s+plug|ring|plug|apg|tpg|trg|lvd|spc|confirmation|order|purchase)\b'
    if re.search(PRODUCT_NOISE, clean) and not re.search(r'(?i)\b(technologies|industries|solutions|works|limited|ltd|pvt|inc|corp)\b', clean):
        return ''
    if clean.lower() in ('gauges', 'tools', 'spares', 'air plug gauge', 'nil stock gauges', 'urgent & nil stock gauges'):
        return ''
    if clean.lower().startswith(('i have', 'we are', 'please', 'kindly', 'dear', 'thanks', 'regards', 'our', 'your', 'this', 'on ', 'the confirmation', 'e-mail', 'facility', 'in-house', 'taken every', 'reserves the')):
        return ''
    if len(clean) < 3 or not any(c.isalpha() for c in clean):
        return ''
    return clean

def _clean_sender_name(sender_name):
    if not sender_name:
        return ''
    name = sender_name.strip(' "\'')
    name = re.sub(r'^(re|fwd|fw)[:\-\s]+', '', name, flags=re.IGNORECASE).strip()
    name = re.sub(r'^\d+[\s\-_]+', '', name).strip()
    name = re.sub(r'^(?:materials[-\d]*|dept|purchase|sales|quality|npd)\s*', '', name, flags=re.IGNORECASE).strip()
    name = re.sub(r'^\d+[\s\-_]+', '', name).strip()
    name = re.sub(r'[\-\s]*\b\d{7,12}\b', '', name).strip()
    name = re.sub(r'[\-\s]*\b\d+\b', '', name).strip()
    name = re.sub(r'[\s\-_]*\d+$', '', name).strip()

    parts = re.split(r'\s*[\-\|/()\[\]]\s*', name)
    if parts:
        first = parts[0].strip()
        first_no_num = re.sub(r'\d+', '', first).strip(' -_')
        if len(first_no_num) >= 2 and first_no_num.lower() not in GENERIC_SENDER_WORDS:
            return first_no_num
        elif len(first) >= 2 and first.lower() not in GENERIC_SENDER_WORDS:
            return first
    name_no_num = re.sub(r'\d+', '', name).strip(' -_')
    return name_no_num if name_no_num.lower() not in GENERIC_SENDER_WORDS else ''

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
                clean_name = re.sub(r'[\-\s]*\b\d{7,12}\b', '', clean).strip(' -_')
                clean_name = re.sub(r'\d+', '', clean_name).strip(' -_')
                if len(clean_name) >= 2:
                    return clean_name
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

    # First check signature block lines (after Thanks & Regards)
    lines = clean_body.splitlines()
    in_sig = False
    sig_lines = []
    for line in lines:
        lclean = line.strip(' *,#>-_\t\r\n\'"[]()')
        if re.search(r'(?i)(?:thanks\s*&\s*regards|regards|best\s*regards|warm\s*regards|cheers|thanks)', lclean):
            in_sig = True
            continue
        if in_sig:
            if not lclean or lclean.startswith('http') or lclean.startswith('www') or '@' in lclean or 'image' in lclean.lower() or lclean.lower().startswith(('wrote:', 'on ', 're:', 'fwd:', 'for ', 'caution', 'disclaimer')):
                continue
            if '****************' in lclean or 'PRIVILEGED AND CONFIDENTIAL' in lclean:
                break
            sig_lines.append(lclean)

    for line in sig_lines:
        cname = _clean_company_name(line)
        if cname and not re.search(r'(?i)\b(manager|lead|engineer|dept|department|quality|npd|purchase|sales|sourcing|purchase\s+dept)\b', cname):
            if any(w in cname.lower() for w in ('technologies', 'industries', 'solutions', 'works', 'ltd', 'limited', 'pvt', 'inc', 'corp', 'group', 'systems', 'components', 'forgings', 'mfg', 'manufacturing')) or (sender_email and '@' in sender_email and sender_email.split('@')[1].split('.')[0].lower() in cname.lower().replace(' ', '').replace('-', '')):
                company_name = cname
                break

    if not company_name:
        company_keywords = r'(?:Engineering\s+Solutions|Engineering\s+Works|Industries|Technologies|Pvt\s+Ltd|Private\s+Limited|Limited|Ltd|Enterprises|Solutions|Tools|Gauges|Machining|Automation|Works|Mfg|Manufacturing|Forgings)'
        for line in combined_text.splitlines():
            line_clean = line.strip(' *,#>\r\n\t')
            if '****************' in line_clean or 'CAUTION - Disclaimer' in line_clean:
                break
            line_preclean = re.split(r'[\,;\s]+\bformerly\b', line_clean, flags=re.IGNORECASE)[0].strip()
            line_preclean = re.sub(r'^(?:our\s+company\s+(?:name\s+)?(?:was\s+)?(?:upgraded|changed|renamed)\s+(?:to\s+)?)\s*', '', line_preclean, flags=re.IGNORECASE).strip()
            comp_match = re.search(rf'([A-Za-z0-9\s&\._\-]{{3,50}}\b{company_keywords}\b)', line_preclean, re.IGNORECASE)
            if comp_match:
                cname = _clean_company_name(comp_match.group(1))
                if cname:
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

INVALID_TITLE_STARTS = (
    'as per', 'kindly', 'please', 'find attached', 'check our', 'we look', 'thanks',
    'regards', 'greetings', 'dear', 'on ', 'wrote:', 'http', 'www', 're:', 'fwd:',
    'with reference', 'sir', 'gentle reminder', 'requesting', 'required', 'requirement',
    'po.no', 'po no', 'best regards', 'admin', 'office', 'mobile:', 'mail:', 'pan:', 'address:'
)

PRODUCT_KEYWORDS = (
    'gauge', 'plug', 'ring', 'unit', 'spc', 'lvdt', 'air', 'tpg', 'trg', 'apg', 'arg',
    'comparator', 'pin', 'block', 'stand', 'amc', 'service', 'spares', 'carbide', 'master'
)

def _simplify_product_title(text):
    if not text:
        return ''
    t = text.strip(' "\'#*>-_\t\r\n')

    dia_match = re.search(r'(?i)(?:dia|Ø|size|size\s*:)\s*[Øø]?\s*(\d+(?:\.\d+)?)', t)
    dia_str = f" Ø{dia_match.group(1)}mm" if dia_match else ""

    if re.search(r'(?i)\bcarbide\s+air\s+plug\b|\bcarbide\s+air\s+plug\s+gauge\b', t):
        return "Carbide Air Plug Gauge"
    if re.search(r'(?i)\bair\s+ring\s+gauge\b|\barg\b', t):
        return "Air Ring Gauge"
    if re.search(r'(?i)\bair\s+plug\s+gauge\b|\bapg\b', t):
        return "Air Plug Gauge"
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
    if re.search(r'(?i)\blvdt\b', t):
        return "LVDT Gauge"
    if re.search(r'(?i)\bpin\s+gauge\b', t):
        return "Pin Gauge"

    clean_t = re.sub(r'^(re|fwd|fw|rfq|enquiry|inquiry)[:\-\s_]+', '', t, flags=re.IGNORECASE).strip()
    clean_lower = clean_t.lower()

    if any(clean_lower.startswith(w) for w in INVALID_TITLE_STARTS):
        return ''
    if any(w in clean_lower for w in ('check our', 'kindly check', 'find attached', 'purchase order', 'quote request', 'corporate video', 'instagram', 'facebook', 'linkedin', 'address', 'gstin', 'pan:', 'engineering solutions', 'private limited', 'pvt ltd', 'inspect', 'possible for', 'quotation for unit', 'opportunity', 'per month', 'units per', 'in addition', 'we also need', 'favor by', 'machine', 'dowel pin')):
        return ''

    # Only return string if line explicitly matches a known gauge product term and is short
    if any(kw in clean_lower for kw in ('air plug', 'air ring', 'thread plug', 'thread ring', 'plug gauge', 'ring gauge', 'multi-gauge', 'spc gauge', 'lvdt gauge', 'comparator stand')) and len(clean_t) <= 45:
        return clean_t[:50].strip()

    return ''

def _extract_all_products(subject, body):
    subject = (subject or '').strip()
    body = (body or '').strip()

    items = []
    lines = body.splitlines()

    for line in lines:
        line_clean = re.sub(r'^(?:>\s*)+', '', line).strip(' *,#>-_\t\r\n\'"[]()')
        if not line_clean:
            continue
        line_lower = line_clean.lower()
        if any(line_lower.startswith(w) for w in ('sir', 'dear', 'thanks', 'regards', 'kindly', 'please', 'we look', 'as discussed', 'on ', 'from:', 'sent:', 'subject:', 'to:')):
            continue
        if any(w in line_lower for w in ('check our', 'kindly check', 'find attached', 'purchase order', 'quote request', 'corporate video', 'instagram', 'facebook', 'linkedin', 'address', 'gstin', 'pan:', 'engineering solutions', 'private limited', 'pvt ltd')):
            continue

        title = _simplify_product_title(line_clean)
        if title:
            ptype = 'APG'
            if 'Air Ring' in title or 'arg' in line_lower:
                ptype = 'ARG'
            elif 'Air Plug' in title or 'apg' in line_lower:
                ptype = 'APG'
            elif 'Thread Plug' in title or 'tpg' in line_lower:
                ptype = 'TPG'
            elif 'Thread Ring' in title or 'trg' in line_lower:
                ptype = 'TRG'
            elif 'Plain Plug' in title or 'ppg' in line_lower:
                ptype = 'PPG'
            elif 'Plain Ring' in title or 'prg' in line_lower:
                ptype = 'PRG'
            elif 'SPC' in title:
                ptype = 'SPC'
            elif 'LVDT' in title:
                ptype = 'LVDT'
            elif 'Multi' in title:
                ptype = 'Multi-Gauge'

            rem_match = re.search(r'[:\-]\s*(.*)', line_clean)
            remarks = rem_match.group(1).strip() if rem_match else line_clean

            qty = 1
            qty_match = re.search(r'\b(\d+)\s*(?:nos|no|pcs|unit|units|sets|set)\b', line_clean, re.I)
            if qty_match:
                try: qty = int(qty_match.group(1))
                except Exception: pass

            items.append({
                'product_name': title,
                'product_type': ptype,
                'quantity': qty,
                'remarks': remarks
            })

    return _group_duplicate_products(items)


def _group_duplicate_products(items):
    if not items:
        return []

    grouped_map = {}
    grouped_list = []

    for item in items:
        p_name = (item.get('product_name') or '').strip()
        p_type = (item.get('product_type') or 'APG').strip()

        # Standardize product_name to canonical base name
        p_name_lower = p_name.lower()
        p_type_lower = p_type.lower()
        if 'air ring' in p_name_lower or p_type_lower in ('arg', 'sarg'):
            p_name = 'Air Ring Gauge'
            p_type = 'ARG'
        elif 'air plug' in p_name_lower or p_type_lower in ('apg', 'sapg'):
            p_name = 'Air Plug Gauge'
            p_type = 'APG'
        elif 'thread ring' in p_name_lower or p_type_lower in ('trg', 'strg'):
            p_name = 'Thread Ring Gauge'
            p_type = 'TRG'
        elif 'thread plug' in p_name_lower or p_type_lower in ('tpg', 'stpg'):
            p_name = 'Thread Plug Gauge'
            p_type = 'TPG'
        elif 'plain ring' in p_name_lower or p_type_lower in ('prg', 'sprg'):
            p_name = 'Plain Ring Gauge'
            p_type = 'PRG'
        elif 'plain plug' in p_name_lower or p_type_lower in ('ppg', 'sppg'):
            p_name = 'Plain Plug Gauge'
            p_type = 'PPG'

        p_rate = str(item.get('rate') or item.get('rate_per_unit') or '').strip()
        p_remarks = (item.get('remarks') or item.get('product_remarks') or '').strip()

        key = (p_name.lower(), p_type.lower(), p_rate)

        qty = item.get('quantity') or 1
        try:
            qty = int(qty)
        except (ValueError, TypeError):
            qty = 1

        if key in grouped_map:
            grouped_entry = grouped_map[key]
            grouped_entry['quantity'] += qty
            if p_remarks and p_remarks.lower() not in grouped_entry['_remarks_lower_set']:
                grouped_entry['_remarks_lower_set'].add(p_remarks.lower())
                if grouped_entry['remarks']:
                    if len(grouped_entry['remarks']) < 400:
                        grouped_entry['remarks'] += f"; {p_remarks}"
                else:
                    grouped_entry['remarks'] = p_remarks
        else:
            new_item = dict(item)
            new_item['product_name'] = p_name
            new_item['product_type'] = p_type
            new_item['quantity'] = qty
            new_item['remarks'] = p_remarks
            new_item['_remarks_lower_set'] = {p_remarks.lower()} if p_remarks else set()
            grouped_map[key] = new_item
            grouped_list.append(new_item)

    for item in grouped_list:
        item.pop('_remarks_lower_set', None)

    return grouped_list

def _extract_product_name(subject, body):
    all_prods = _extract_all_products(subject, body)
    if all_prods:
        return all_prods[0]['product_name']

    subject = (subject or '').strip()
    body = (body or '').strip()

    prod_match = re.search(r'(?i)(?:product\s*name|product\s*description|item\s*description|part\s*name)\s*[:\-]\s*([^\r\n]+)', body)
    if prod_match:
        extracted = prod_match.group(1).strip(' *,#>\r\n\t')
        if len(extracted) >= 2:
            t = _simplify_product_title(extracted)
            if t: return t

    for_match = re.search(r'(?i)(?:quotation|quote|price|enquiry|rfq|requirement|req)\s+(?:for|of)\s+([^\r\n,.]+)', f"{subject}\n{body}")
    if for_match:
        extracted = for_match.group(1).strip(' *,#>\r\n\t')
        if len(extracted) >= 3 and not any(w in extracted.lower() for w in ('below', 'following', 'attached', 'mentioned', 'us', 'me', 'the mentioned rfq')):
            t = _simplify_product_title(extracted)
            if t: return t

    clean_subj = re.sub(r'^(re|fwd|fw|rfq|enquiry|inquiry)[:\-\s_]+', '', subject, flags=re.IGNORECASE).strip()
    company_or_doc = re.search(r'(?:Engineering|Forgings|Industries|Technologies|Pvt\s+Ltd|Private\s+Limited|Ltd|Quotation_MES|MES_Q|PO\s+Copy|Purchase\s+Order)', clean_subj, flags=re.IGNORECASE)

    if clean_subj.lower() not in GENERIC_SUBJECTS and not company_or_doc and len(clean_subj) >= 3:
        t = _simplify_product_title(clean_subj)
        if t: return t

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

    if ext_company_name and not is_domain_fb:
        target_cust_name = ext_company_name
    elif clean_sname and clean_sname.lower() not in GENERIC_SENDER_WORDS:
        target_cust_name = clean_sname
    elif ext_person_name:
        target_cust_name = ext_person_name
    elif ext_company_name:
        target_cust_name = ext_company_name
    else:
        target_cust_name = ''

    if target_cust_name:
        clean_target = re.sub(r'[\-\s]*\b\d{7,12}\b', '', target_cust_name).strip(' -_')
        clean_target = re.sub(r'^\d+[\s\-_]+', '', clean_target).strip(' -_')
        clean_target = re.sub(r'[\s\-_]+\d+$', '', clean_target).strip(' -_')
        if clean_target:
            target_cust_name = clean_target

    customer = None
    if sender_email and not any(sender_email.endswith('@' + d) for d in INTERNAL_DOMAINS):
        customer = Customer.objects.filter(email__iexact=sender_email).first()

    if not customer and target_cust_name:
        customer = Customer.objects.filter(customer_name__iexact=target_cust_name).first()

    if not customer and ext_company_name:
        customer = Customer.objects.filter(customer_name__iexact=ext_company_name).first()

    if not customer and clean_sname:
        customer = Customer.objects.filter(customer_name__iexact=clean_sname).first()

    if not customer and target_cust_name and len(target_cust_name) >= 4:
        if target_cust_name.lower() not in GENERIC_COMPANY_WORDS and target_cust_name.lower() not in GENERIC_SENDER_WORDS:
            customer = Customer.objects.filter(customer_name__icontains=target_cust_name).first()

    if not customer and ext_company_name and len(ext_company_name) >= 4:
        if ext_company_name.lower() not in GENERIC_COMPANY_WORDS:
            customer = Customer.objects.filter(customer_name__icontains=ext_company_name).first()

    if not customer and target_cust_name:
        words = [w for w in target_cust_name.split() if len(w) >= 3 and w.lower() not in GENERIC_COMPANY_WORDS and w.lower() not in GENERIC_SENDER_WORDS]
        if len(words) >= 2:
            sig_phrase = ' '.join(words[:2])
            customer = Customer.objects.filter(customer_name__icontains=sig_phrase).first()

    phone_match = re.search(r'\b[6-9]\d{9}\b', f"{record.subject} {record.body}")
    extracted_phone = phone_match.group(0) if phone_match else ''

    import json
    all_extracted_products = _extract_all_products(record.subject, record.body)
    products_json = json.dumps(all_extracted_products) if all_extracted_products else ''

    clean_product_name = _extract_product_name(record.subject, record.body)
    product_type = _extract_product_type(clean_product_name, record.body)
    qty, unit = _extract_qty_and_unit(record.body)
    product_remarks = _extract_product_remarks(record.body)

    mail_date_str = record.received_at.strftime('%Y-%m-%d') if record.received_at else timezone.localdate().strftime('%Y-%m-%d')

    if customer:
        if ext_region and not customer.region:
            customer.region = ext_region
            customer.save(update_fields=['region'])

        url = reverse('rfq_details')
        params_dict = {
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
        }
        if products_json:
            request.session['email_prefill_products_json'] = products_json
            if len(products_json) < 800:
                params_dict['products_json'] = products_json

        return redirect(f"{url}?{urlencode(params_dict)}")
    else:
        cust_name_final = ext_company_name or target_cust_name or clean_sname or 'New Customer'
        cust_name_final = re.sub(r'[\-\s]*\b\d{7,12}\b', '', cust_name_final).strip(' -_')
        cust_name_final = re.sub(r'^\d+[\s\-_]+', '', cust_name_final).strip(' -_')
        cust_name_final = re.sub(r'[\s\-_]+\d+$', '', cust_name_final).strip(' -_')

        if products_json:
            request.session['email_prefill_products_json'] = products_json

        url = reverse('customer_details')
        cust_params = {
            'customer_name': cust_name_final or 'New Customer',
            'email': sender_email if not any(sender_email.endswith('@' + d) for d in INTERNAL_DOMAINS) else '',
            'phone_number': extracted_phone or '',
            'region': ext_region or '',
            'state_code': '33' if ext_region in ('Chennai', 'Hosur') else '',
            'from_email_id': record.id,
            'mail_date_param': mail_date_str,
            'enquiry_details_param': record.subject,
            'product_name_param': clean_product_name,
            'product_type_param': product_type,
            'quantity_param': qty,
            'unit_param': unit,
            'product_remarks_param': product_remarks,
        }
        messages.info(request, f'New customer "{cust_name_final}" detected. Please complete customer master details to proceed to Add RFQ.')
        return redirect(f"{url}?{urlencode(cust_params)}")


@login_required
def add_po_from_email(request, record_id):
    from urllib.parse import urlencode
    from django.urls import reverse
    from email.utils import parseaddr
    import re
    from customers.models import Customer
    from rfq.models import RFQ, RFQQuotation
    from django.utils import timezone
    import json

    record = get_object_or_404(EmailRecord, pk=record_id)
    sender_raw = record.sender or ''
    sender_name, sender_email = parseaddr(sender_raw)
    sender_email = sender_email.strip().lower()
    clean_sname = _clean_sender_name(sender_name)

    ext_person_name = _extract_person_name(record.body)
    ext_company_name, ext_region, is_domain_fb = _extract_company_and_region(record.body, record.subject, sender_name, sender_email)

    # Search for Quotation Number or RFQ Number in Subject/Body first
    q_match = re.search(r'MES[_\/][A-Za-z0-9_\-\/]+', f"{record.subject} {record.body}", re.IGNORECASE)
    rfq_match = re.search(r'RFQ-\d{4}-\d+', f"{record.subject} {record.body}", re.IGNORECASE)

    quotation = None
    linked_rfq = None

    if q_match:
        qno_raw = q_match.group(0).strip(' .,;:')
        quotation = RFQQuotation.objects.filter(quotation_number__iexact=qno_raw).first()
        if not quotation:
            quotation = RFQQuotation.objects.filter(quotation_number__icontains=qno_raw).first()
        if not quotation:
            clean_q = re.sub(r'[^A-Za-z0-9_]', '', qno_raw).lower()
            for q in RFQQuotation.objects.select_related('rfq__customer').all():
                if clean_q in re.sub(r'[^A-Za-z0-9_]', '', q.quotation_number).lower():
                    quotation = q
                    break
        if quotation:
            linked_rfq = quotation.rfq

    if not linked_rfq and rfq_match:
        rfq_no = rfq_match.group(0).upper()
        linked_rfq = RFQ.objects.filter(rfq_no__iexact=rfq_no).first()

    if not quotation and linked_rfq:
        quotation = RFQQuotation.objects.filter(rfq=linked_rfq).order_by('-created_at').first()

    if ext_person_name:
        target_cust_name = ext_person_name
    elif clean_sname:
        target_cust_name = clean_sname
    elif ext_company_name:
        target_cust_name = ext_company_name
    else:
        target_cust_name = 'New Customer'
    if target_cust_name:
        clean_target = re.sub(r'[\-\s]*\b\d{7,12}\b', '', target_cust_name).strip(' -_')
        clean_target = re.sub(r'^\d+[\s\-_]+', '', clean_target).strip(' -_')
        clean_target = re.sub(r'[\s\-_]+\d+$', '', clean_target).strip(' -_')
        if clean_target:
            target_cust_name = clean_target

    customer = None
    if quotation and quotation.rfq and quotation.rfq.customer:
        customer = quotation.rfq.customer
    elif linked_rfq and linked_rfq.customer:
        customer = linked_rfq.customer

    if not customer and sender_email and not any(sender_email.endswith('@' + d) for d in INTERNAL_DOMAINS):
        customer = Customer.objects.filter(email__iexact=sender_email).first()

    if not customer and target_cust_name:
        customer = Customer.objects.filter(customer_name__iexact=target_cust_name).first()

    if not customer and target_cust_name:
        customer = Customer.objects.filter(customer_name__icontains=target_cust_name).first()

    if not customer and ext_company_name:
        customer = Customer.objects.filter(customer_name__icontains=ext_company_name).first()

    if not customer and clean_sname:
        customer = Customer.objects.filter(customer_name__icontains=clean_sname).first()

    if not customer and target_cust_name:
        words = [w for w in target_cust_name.split() if len(w) >= 2 and w.lower() not in GENERIC_COMPANY_WORDS and w.lower() not in GENERIC_SENDER_WORDS]
        if words:
            sig_phrase = ' '.join(words[:2])
            customer = Customer.objects.filter(customer_name__icontains=sig_phrase).first()

    phone_match = re.search(r'\b[6-9]\d{9}\b', f"{record.subject} {record.body}")
    extracted_phone = phone_match.group(0) if phone_match else ''

    if customer:
        updated = False
        clean_existing_name = re.sub(r'[\-\s]*\b\d{7,12}\b', '', customer.customer_name).strip(' -_')
        clean_existing_name = re.sub(r'^\d+[\s\-_]+', '', clean_existing_name).strip(' -_')
        clean_existing_name = re.sub(r'[\s\-_]+\d+$', '', clean_existing_name).strip(' -_')
        if clean_existing_name and clean_existing_name != customer.customer_name:
            customer.customer_name = clean_existing_name
            updated = True
        if (customer.customer_name.lower() in ('new customer', 'unknown', 'sew', '') or 'formerly' in customer.customer_name.lower() or (target_cust_name and customer.customer_name.lower() != target_cust_name.lower() and not customer.customer_name.isupper())) and target_cust_name:
            customer.customer_name = target_cust_name
            updated = True
        if ext_region and not customer.region:
            customer.region = ext_region
            updated = True
        if updated:
            customer.save()
    else:
        cust_name_final = target_cust_name or 'New Customer'
        cust_name_final = re.sub(r'[\-\s]*\b\d{7,12}\b', '', cust_name_final).strip(' -_')
        cust_name_final = re.sub(r'^\d+[\s\-_]+', '', cust_name_final).strip(' -_')
        cust_name_final = re.sub(r'[\s\-_]+\d+$', '', cust_name_final).strip(' -_')
        customer = Customer.objects.create(
            customer_name=cust_name_final or 'New Customer',
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

    # Determine Products & Quotation Value
    products_list = []
    quotation_value_str = ''

    if linked_rfq and linked_rfq.products.exists():
        rfq_prods = linked_rfq.products.all()
        total_val = 0
        for p in rfq_prods:
            p_rate = str(p.rate_per_unit or p.value or '')
            p_val = float(p.value or 0)
            total_val += p_val
            products_list.append({
                'product_name': p.product_name,
                'product_type': p.product_type or 'APG',
                'quantity': p.quantity or 1,
                'rate': p_rate,
                'remarks': p.remarks or ''
            })
        if total_val > 0:
            quotation_value_str = f"{total_val:.2f}"
    elif quotation and quotation.products_snapshot:
        snapshot = quotation.products_snapshot
        total_val = 0
        for p in snapshot:
            p_rate = str(p.get('rate_per_unit') or p.get('value') or '')
            p_val = float(p.get('value') or 0)
            total_val += p_val
            products_list.append({
                'product_name': p.get('product_name', ''),
                'product_type': p.get('product_type', 'APG'),
                'quantity': p.get('quantity', 1),
                'rate': p_rate,
                'remarks': p.get('remarks', '')
            })
        if total_val > 0:
            quotation_value_str = f"{total_val:.2f}"

    if not products_list:
        products_list = _extract_all_products(record.subject, record.body)

    products_list = _group_duplicate_products(products_list)
    products_json = json.dumps(products_list) if products_list else ''

    clean_product_name = _extract_product_name(record.subject, record.body)
    product_type = _extract_product_type(clean_product_name, record.body)
    qty, unit = _extract_qty_and_unit(record.body)
    product_remarks = _extract_product_remarks(record.body)

    rate = ''
    rate_match = re.search(r'(?i)(?:rate|price|unit\s*price|val|value|amount)\s*[:\-=\s]+\$?₹?\s*(\d+(?:\.\d+)?)', record.body or '')
    if rate_match:
        rate = rate_match.group(1)

    url = reverse('customer_order')
    params_dict = {
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
    }
    if quotation:
        params_dict['quotation_number'] = quotation.quotation_number
    if quotation_value_str:
        params_dict['quotation_value'] = quotation_value_str
    if products_json:
        request.session[f'email_products_{record.id}'] = products_json
        request.session['email_prefill_products_json'] = products_json
        if len(products_json) < 800:
            params_dict['products_json'] = products_json

    return redirect(f"{url}?{urlencode(params_dict)}")


