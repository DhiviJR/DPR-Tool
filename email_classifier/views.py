from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render

from .forms import EmailPasteForm, ReviewForm
from .models import EmailRecord
from .services.classifier_service import get_classifier
from .services.imap_reader import fetch_all_messages


@login_required
def dashboard(request):
    selected_category = request.GET.get('category', '')
    records = EmailRecord.objects.order_by('-received_at', '-id')
    total_emails_count = records.count()
    if selected_category in EmailRecord.Category.values:
        records = records.filter(ai_category=selected_category)
    else:
        selected_category = ''

    counts_raw = {item['ai_category']: item['total'] for item in EmailRecord.objects.values('ai_category').annotate(total=Count('id'))}
    category_order = [
        'CUSTOMER_ORDER',
        'PAYMENT_INVOICE',
        'QUOTATION_REQUEST',
        'ENQUIRY',
        'OTHERS',
        'SUPPORT_COMPLAINT',
    ]
    counts = {cat: counts_raw.get(cat, 0) for cat in category_order}

    enquiry_qs = EmailRecord.objects.filter(
        Q(ai_category='ENQUIRY') | Q(final_category='ENQUIRY')
    )
    enquiry_total_count = enquiry_qs.count()
    enquiry_added_count = enquiry_qs.filter(is_added_to_rfq=True).count()
    enquiry_pending_count = enquiry_total_count - enquiry_added_count

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
    return redirect('email_classifier:dashboard')


@login_required
def sync_inbox(request):
    if request.method == 'POST':
        try:
            completed_before = EmailRecord.objects.filter(source='imap').count()
            messages_list, inbox_total = fetch_all_messages(offset=completed_before, limit=30)
            classifier = get_classifier()
            saved = 0
            skipped = 0
            for message in messages_list:
                if EmailRecord.objects.filter(imap_uid=message['uid']).exists():
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
