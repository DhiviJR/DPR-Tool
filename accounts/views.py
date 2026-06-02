from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from .models import CustomUser
from django.contrib.auth.decorators import login_required
from customers.models import Customer
from suppliers.models import Supplier
from dpr.models import DPR
from products.models import CustomerProduct, SupplierProduct
from django.http import HttpResponse, JsonResponse, Http404
from django.db.models import Sum, Case, When, Value, IntegerField
from django.db.models.functions import Coalesce
from decimal import Decimal, InvalidOperation
from django.utils import timezone
from datetime import timedelta
from io import BytesIO
from pathlib import PurePath
from zipfile import ZIP_DEFLATED, ZipFile
import re


def _pct(part, whole):
    return round((part * 100) / whole) if whole else 0


def _resolve_po_number(confirmation_type, po_number_raw):
    if confirmation_type == 'Customer PO':
        po_number = (po_number_raw or '').strip()
        if not po_number:
            return None, 'PO Number is required when Order Confirmation is Customer PO.'
        return po_number, None
    return None, None


def _validate_po_value_matches_total(po_value_raw, product_names, values):
    try:
        po_value = Decimal(str(po_value_raw or '0'))
    except InvalidOperation:
        return False, 'PO Value must be a valid number.'

    total_value = Decimal('0.00')
    for i, product_name in enumerate(product_names):
        if not product_name.strip():
            continue
        raw_value = values[i] if i < len(values) else '0'
        try:
            total_value += Decimal(str(raw_value or '0'))
        except InvalidOperation:
            return False, f'Invalid value for product in row {i + 1}.'

    po_normalized = po_value.quantize(Decimal('0.01'))
    total_normalized = total_value.quantize(Decimal('0.01'))
    if po_normalized != total_normalized:
        return False, (
            f'PO Value ({po_normalized}) must equal Total Value ({total_normalized}).'
        )
    return True, None


def _sync_dpr_supplier_qty_ordered(dpr):
    total_supplier_quantity = SupplierProduct.objects.filter(
        customer_product__dpr=dpr
    ).aggregate(
        total=Coalesce(Sum('quantity'), 0)
    )['total']

    if dpr.supplier_qty_ordered != total_supplier_quantity:
        dpr.supplier_qty_ordered = total_supplier_quantity
        dpr.save(update_fields=['supplier_qty_ordered'])

    return total_supplier_quantity


def _sync_dpr_customer_qty_delivered(dpr):
    total_customer_delivered = CustomerProduct.objects.filter(
        dpr=dpr
    ).aggregate(
        total=Coalesce(Sum('quantity_delivered'), 0)
    )['total']

    if dpr.customer_qty_delivered != total_customer_delivered:
        dpr.customer_qty_delivered = total_customer_delivered
        dpr.save(update_fields=['customer_qty_delivered'])

    return total_customer_delivered


def _sync_dpr_supplier_qty_received(dpr):
    total_supplier_received = SupplierProduct.objects.filter(
        customer_product__dpr=dpr
    ).aggregate(
        total=Coalesce(Sum('quantity_received'), 0)
    )['total']

    if dpr.supplier_qty_received != total_supplier_received:
        dpr.supplier_qty_received = total_supplier_received
        dpr.save(update_fields=['supplier_qty_received'])

    return total_supplier_received


def _get_validity_state(validity_date, today):
    if not validity_date:
        return ''
    if validity_date < today:
        return 'expired'
    if validity_date <= today + timedelta(days=5):
        return 'due_soon'
    return ''


def _get_status_validity_row_class(status, validity_state):
    if status == 'delivered':
        return 'table-success'
    if status == 'cancelled':
        return 'table-secondary'
    if validity_state == 'expired':
        return 'table-danger'
    if validity_state == 'due_soon':
        return 'table-warning'
    return ''


def _get_status_validity_filter_state(status, validity_state):
    if status == 'delivered':
        return 'closed'
    if status == 'cancelled':
        return 'cancelled'
    if validity_state == 'expired':
        return 'expired'
    if validity_state == 'due_soon':
        return 'due_soon'
    if status == 'partially_delivered':
        return 'partially_closed'
    return 'pending'


def _get_dpr_row_class(filter_state):
    if filter_state == 'completed':
        return 'dpr-row-completed'
    if filter_state == 'after_due':
        return 'dpr-row-after-due'
    if filter_state == 'due_soon':
        return 'dpr-row-due-soon'
    if filter_state == 'supplier_order_pending':
        return 'dpr-row-supplier-order-pending'
    if filter_state == 'mail_alert':
        return 'dpr-row-mail-alert'
    if filter_state == 'qty_matched':
        return 'dpr-row-qty-matched'
    return ''


def user_login(request):
    error = None
    username = ''

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')

        user = authenticate(username=username, password=password)

        if user is not None:
            login(request, user)
            return redirect('dashboard')
        else:
            error = 'Invalid username or password.'

    return render(request, 'login.html', {
        'error': error,
        'username': username,
    })



def user_logout(request):
    logout(request)
    return redirect('login')



def register(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user_type = request.POST.get('user_type')

        CustomUser.objects.create_user(
            username=username,
            password=password,
            user_type=user_type
        )

        return redirect('login')

    return render(request, 'register.html')

@login_required
def dashboard(request):
    today = timezone.localdate()
    within_7_days = today + timedelta(days=5)

    total_dpr_count = DPR.objects.count()
    pending_mail_confirmation_count = DPR.objects.filter(
        confirmation_type__icontains='mail'
    ).count()

    total_customer_products = CustomerProduct.objects.count()
    customer_within_7_days_count = CustomerProduct.objects.filter(
        dpr__po_validity__gte=today,
        dpr__po_validity__lte=within_7_days
    ).count()
    customer_expired_count = CustomerProduct.objects.filter(
        dpr__po_validity__lt=today
    ).count()
    customer_delivered_count = CustomerProduct.objects.filter(status='delivered').count()
    customer_partial_count = CustomerProduct.objects.filter(status='partially_delivered').count()
    customer_cancelled_count = CustomerProduct.objects.filter(status='cancelled').count()
    customer_pending_count = CustomerProduct.objects.filter(status__isnull=True).count()

    total_supplier_products = SupplierProduct.objects.count()
    supplier_within_7_days_count = SupplierProduct.objects.filter(
        po_validity__gte=today,
        po_validity__lte=within_7_days
    ).count()
    supplier_expired_count = SupplierProduct.objects.filter(
        po_validity__lt=today
    ).count()
    supplier_delivered_count = SupplierProduct.objects.filter(status='delivered').count()
    supplier_partial_count = SupplierProduct.objects.filter(status='partially_delivered').count()
    supplier_cancelled_count = SupplierProduct.objects.filter(status='cancelled').count()
    supplier_pending_count = SupplierProduct.objects.filter(status__isnull=True).count()

    customer_delivery_pct = _pct(customer_delivered_count, total_customer_products)
    supplier_delivery_pct = _pct(supplier_delivered_count, total_supplier_products)
    mail_confirmation_pct = _pct(pending_mail_confirmation_count, total_dpr_count)
    total_products = total_customer_products + total_supplier_products
    total_delivered = customer_delivered_count + supplier_delivered_count
    overall_delivery_pct = _pct(total_delivered, total_products)
    supplier_order_pending_difference = max(
        total_customer_products - total_supplier_products,
        0
    )

    return render(request, 'dashboard.html', {
        'total_dpr_count': total_dpr_count,
        'total_customer_products': total_customer_products,
        'total_supplier_products': total_supplier_products,
        'pending_mail_confirmation_count': pending_mail_confirmation_count,
        'customer_within_7_days_count': customer_within_7_days_count,
        'customer_expired_count': customer_expired_count,
        'customer_delivered_count': customer_delivered_count,
        'customer_partial_count': customer_partial_count,
        'customer_cancelled_count': customer_cancelled_count,
        'customer_pending_count': customer_pending_count,
        'supplier_within_7_days_count': supplier_within_7_days_count,
        'supplier_expired_count': supplier_expired_count,
        'supplier_delivered_count': supplier_delivered_count,
        'supplier_partial_count': supplier_partial_count,
        'supplier_cancelled_count': supplier_cancelled_count,
        'supplier_pending_count': supplier_pending_count,
        'customer_delivery_pct': customer_delivery_pct,
        'supplier_delivery_pct': supplier_delivery_pct,
        'mail_confirmation_pct': mail_confirmation_pct,
        'overall_delivery_pct': overall_delivery_pct,
        'supplier_order_pending_difference': supplier_order_pending_difference,
    })


@login_required
def dpr_view(request):
    confirmation_filter = request.GET.get('confirmation')
    case_filter = request.GET.get('case', '')
    dpr_queryset = DPR.objects.select_related('customer').order_by('-created_at')
    is_mail_filter = confirmation_filter == 'mail'
    if is_mail_filter:
        dpr_queryset = dpr_queryset.filter(confirmation_type__icontains='mail')

    dprs = list(dpr_queryset)

    customer_qty_map = {
        row['dpr_id']: row['total']
        for row in CustomerProduct.objects.values('dpr_id').annotate(
            total=Coalesce(Sum('quantity_ordered'), 0)
        )
    }

    supplier_qty_map = {
        row['customer_product__dpr_id']: row['total']
        for row in SupplierProduct.objects.values('customer_product__dpr_id').annotate(
            total=Coalesce(Sum('quantity'), 0)
        )
    }

    today = timezone.localdate()
    po_alert_date = today - timedelta(days=3)
    for dpr in dprs:
        dpr.total_quantity_ordered = customer_qty_map.get(dpr.id, 0)
        dpr.supplier_quantity_ordered = supplier_qty_map.get(dpr.id, 0)
        if dpr.supplier_qty_ordered != dpr.supplier_quantity_ordered:
            dpr.supplier_qty_ordered = dpr.supplier_quantity_ordered
            dpr.save(update_fields=['supplier_qty_ordered'])
        conf = (dpr.confirmation_type or '').lower()
        dpr.is_alert_row = (
            dpr.po_date is not None
            and 'mail' in conf
            and dpr.po_date < po_alert_date
        )
        dpr.validity_state = _get_validity_state(dpr.po_validity, today)
        if (
            dpr.total_quantity_ordered > 0
            and dpr.total_quantity_ordered == dpr.customer_qty_delivered
            and dpr.supplier_quantity_ordered == dpr.supplier_qty_received
        ):
            dpr.filter_state = 'completed'
        elif dpr.validity_state == 'expired':
            dpr.filter_state = 'after_due'
        elif dpr.validity_state == 'due_soon':
            dpr.filter_state = 'due_soon'
        elif (
            dpr.po_date is not None
            and dpr.po_date <= today
            and dpr.total_quantity_ordered > dpr.supplier_quantity_ordered
        ):
            dpr.filter_state = 'supplier_order_pending'
        elif dpr.is_alert_row:
            dpr.filter_state = 'mail_alert'
        elif (
            dpr.total_quantity_ordered > 0
            and dpr.supplier_quantity_ordered == dpr.total_quantity_ordered
        ):
            dpr.filter_state = 'qty_matched'
        else:
            dpr.filter_state = 'normal'
        dpr.row_class = _get_dpr_row_class(dpr.filter_state)

    return render(
        request,
        'dpr_view.html',
        {
            'dprs': dprs,
            'is_mail_filter': is_mail_filter,
            'case_filter': case_filter,
        }
    )


@login_required
def dpr_products(request, dpr_id):
    try:
        dpr = DPR.objects.get(pk=dpr_id)
    except DPR.DoesNotExist:
        raise Http404

    products = CustomerProduct.objects.filter(dpr=dpr)
    data = [
        {
            'product_name': p.product_name,
            'quantity_ordered': p.quantity_ordered,
            'value': str(p.value),
            'remarks': p.remarks or '',
        }
        for p in products
    ]
    return JsonResponse({'dpr_serial': dpr.serial_number, 'products': data})


@login_required
def dpr_documents_download(request, dpr_id):
    try:
        dpr = DPR.objects.get(pk=dpr_id)
    except DPR.DoesNotExist:
        raise Http404

    customer_products = CustomerProduct.objects.filter(dpr=dpr)
    supplier_products = SupplierProduct.objects.filter(
        customer_product__dpr=dpr
    ).select_related('customer_product', 'supplier')

    archive = BytesIO()
    added_files = 0

    def add_file(zip_file, file_field, archive_name):
        nonlocal added_files
        if not file_field or not file_field.name:
            return
        try:
            with file_field.storage.open(file_field.name, 'rb') as source:
                zip_file.writestr(archive_name, source.read())
                added_files += 1
        except FileNotFoundError:
            return

    with ZipFile(archive, 'w', ZIP_DEFLATED) as zip_file:
        add_file(
            zip_file,
            dpr.quotation_attachment,
            f'quotation/{PurePath(dpr.quotation_attachment.name).name}'
            if dpr.quotation_attachment else ''
        )
        add_file(
            zip_file,
            dpr.po_attachment,
            f'customer_po/{PurePath(dpr.po_attachment.name).name}'
            if dpr.po_attachment else ''
        )

        for product in customer_products:
            add_file(
                zip_file,
                product.attachment,
                f'customer_products/{product.id}_{PurePath(product.attachment.name).name}'
                if product.attachment else ''
            )
            add_file(
                zip_file,
                product.invoice_dc_attachment,
                f'invoice_dc/{product.id}_{PurePath(product.invoice_dc_attachment.name).name}'
                if product.invoice_dc_attachment else ''
            )

        for supplier_product in supplier_products:
            add_file(
                zip_file,
                supplier_product.po_attachment,
                (
                    f'supplier_po/{supplier_product.id}_'
                    f'{PurePath(supplier_product.po_attachment.name).name}'
                )
                if supplier_product.po_attachment else ''
            )

        if not added_files:
            zip_file.writestr(
                'README.txt',
                f'No documents are currently attached to {dpr.serial_number}.'
            )

    archive.seek(0)
    response = HttpResponse(archive.getvalue(), content_type='application/zip')
    response['Content-Disposition'] = (
        f'attachment; filename="{dpr.serial_number}_documents.zip"'
    )
    return response


@login_required
def customer_po_product_details(request):
    validity_filter = request.GET.get('validity')
    today = timezone.localdate()
    within_7_days = today + timedelta(days=5)

    products = CustomerProduct.objects.select_related(
        'dpr',
        'dpr__customer'
    ).annotate(
        status_rank=Case(
            When(status__isnull=True, then=Value(0)),
            When(status='partially_delivered', then=Value(0)),
            default=Value(1),
            output_field=IntegerField()
        )
    )

    if validity_filter == 'within7':
        products = products.filter(
            dpr__po_validity__gte=today,
            dpr__po_validity__lte=within_7_days
        )
    elif validity_filter == 'expired':
        products = products.filter(dpr__po_validity__lt=today)

    products = list(products.order_by('status_rank', 'dpr__po_validity', 'id'))
    for product in products:
        product.validity_state = _get_validity_state(product.dpr.po_validity, today)
        product.row_class = _get_status_validity_row_class(
            product.status,
            product.validity_state
        )
        product.filter_state = _get_status_validity_filter_state(
            product.status,
            product.validity_state
        )

    return render(
        request,
        'customer_po_product_details.html',
        {
            'products': products,
        }
    )


@login_required
def customer_product_status_update(request, product_id):
    if request.method != 'POST':
        raise Http404
    try:
        customer_product = CustomerProduct.objects.select_related('dpr').get(pk=product_id)
    except CustomerProduct.DoesNotExist:
        raise Http404

    status = request.POST.get('status', '').strip() or None
    if status not in ('delivered', 'partially_delivered', 'cancelled', None):
        return JsonResponse({'status': 'error', 'message': 'Invalid status'}, status=400)

    customer_product.status = status
    if status == 'delivered':
        invoice_dc_number = request.POST.get('invoice_dc_number', '').strip()
        invoice_dc_attachment = request.FILES.get('invoice_dc_attachment')
        if not invoice_dc_number:
            return JsonResponse({'status': 'error', 'message': 'Invoice/DC number is required'}, status=400)
        if not invoice_dc_attachment and not customer_product.invoice_dc_attachment:
            return JsonResponse({'status': 'error', 'message': 'Invoice/DC attachment is required'}, status=400)
        customer_product.quantity_delivered = customer_product.quantity_ordered
        customer_product.invoice_dc_number = invoice_dc_number
        if invoice_dc_attachment:
            customer_product.invoice_dc_attachment = invoice_dc_attachment
    elif status == 'cancelled':
        customer_product.quantity_delivered = 0
        customer_product.invoice_dc_number = None
        customer_product.invoice_dc_attachment = None
    elif status == 'partially_delivered':
        delivered_qty_raw = request.POST.get('quantity_delivered', '').strip()
        if not delivered_qty_raw:
            return JsonResponse({'status': 'error', 'message': 'Quantity delivered is required'}, status=400)
        try:
            delivered_qty = int(delivered_qty_raw)
        except ValueError:
            return JsonResponse({'status': 'error', 'message': 'Quantity delivered must be a number'}, status=400)
        if delivered_qty <= 0 or delivered_qty >= customer_product.quantity_ordered:
            return JsonResponse({
                'status': 'error',
                'message': f'Quantity delivered must be greater than 0 and less than quantity ordered ({customer_product.quantity_ordered})'
            }, status=400)
        customer_product.quantity_delivered = delivered_qty
        customer_product.invoice_dc_number = None
        customer_product.invoice_dc_attachment = None
    else:
        customer_product.quantity_delivered = 0
        customer_product.invoice_dc_number = None
        customer_product.invoice_dc_attachment = None

    customer_product.save(update_fields=[
        'status',
        'quantity_delivered',
        'invoice_dc_number',
        'invoice_dc_attachment'
    ])
    _sync_dpr_customer_qty_delivered(customer_product.dpr)
    return JsonResponse({'status': 'ok'})


@login_required
def supplier_status_details(request, product_id):
    try:
        customer_product = CustomerProduct.objects.select_related('dpr').get(pk=product_id)
    except CustomerProduct.DoesNotExist:
        raise Http404

    supplier_rows = SupplierProduct.objects.filter(
        customer_product=customer_product
    ).select_related('supplier')

    data = [
        {
            'supplier_name': row.supplier.supplier_name,
            'quantity': row.quantity,
            'po_number': row.po_number,
            'po_value': str(row.po_value),
            'po_date': row.po_date.strftime('%Y-%m-%d') if row.po_date else '-',
            'po_validity': row.po_validity.strftime('%Y-%m-%d') if row.po_validity else '-',
        }
        for row in supplier_rows
    ]

    return JsonResponse({
        'product_name': customer_product.product_name,
        'dpr_serial': customer_product.dpr.serial_number,
        'supplier_rows': data,
    })


@login_required
def supplier_po_product_details(request):
    validity_filter = request.GET.get('validity')
    today = timezone.localdate()
    within_7_days = today + timedelta(days=5)

    supplier_products = SupplierProduct.objects.select_related(
        'customer_product',
        'customer_product__dpr',
        'customer_product__dpr__customer',
        'supplier'
    ).annotate(
        status_rank=Case(
            When(status__isnull=True, then=Value(0)),
            When(status='partially_delivered', then=Value(0)),
            default=Value(1),
            output_field=IntegerField()
        )
    )

    if validity_filter == 'within7':
        supplier_products = supplier_products.filter(
            po_validity__gte=today,
            po_validity__lte=within_7_days
        )
    elif validity_filter == 'expired':
        supplier_products = supplier_products.filter(po_validity__lt=today)

    supplier_products = list(supplier_products.order_by('status_rank', 'po_validity', 'id'))
    for supplier_product in supplier_products:
        supplier_product.validity_state = _get_validity_state(
            supplier_product.po_validity,
            today
        )
        supplier_product.row_class = _get_status_validity_row_class(
            supplier_product.status,
            supplier_product.validity_state
        )
        supplier_product.filter_state = _get_status_validity_filter_state(
            supplier_product.status,
            supplier_product.validity_state
        )

    return render(
        request,
        'supplier_po_product_details.html',
        {'supplier_products': supplier_products}
    )


@login_required
def supplier_product_status_update(request, supplier_product_id):
    if request.method != 'POST':
        raise Http404
    try:
        supplier_product = SupplierProduct.objects.select_related(
            'customer_product__dpr'
        ).get(pk=supplier_product_id)
    except SupplierProduct.DoesNotExist:
        raise Http404

    status = request.POST.get('status', '').strip() or None
    if status not in ('delivered', 'partially_delivered', 'cancelled', None):
        return JsonResponse({'status': 'error', 'message': 'Invalid status'}, status=400)

    supplier_product.status = status
    if status == 'delivered':
        supplier_product.quantity_received = supplier_product.quantity
    elif status == 'cancelled':
        supplier_product.quantity_received = 0
    elif status == 'partially_delivered':
        qty_raw = request.POST.get('quantity_received', '').strip()
        if not qty_raw:
            return JsonResponse({'status': 'error', 'message': 'Quantity received is required'}, status=400)
        try:
            qty_received = int(qty_raw)
        except ValueError:
            return JsonResponse({'status': 'error', 'message': 'Quantity received must be a number'}, status=400)
        if qty_received <= 0 or qty_received >= supplier_product.quantity:
            return JsonResponse({
                'status': 'error',
                'message': f'Quantity received must be greater than 0 and less than quantity ordered ({supplier_product.quantity})'
            }, status=400)
        supplier_product.quantity_received = qty_received
    else:
        supplier_product.quantity_received = 0

    supplier_product.save(update_fields=['status', 'quantity_received'])
    _sync_dpr_supplier_qty_received(supplier_product.customer_product.dpr)
    return JsonResponse({'status': 'ok'})


@login_required
def supplier_product_expected_date_update(request, supplier_product_id):
    if request.method != 'POST':
        raise Http404
    try:
        supplier_product = SupplierProduct.objects.get(pk=supplier_product_id)
    except SupplierProduct.DoesNotExist:
        raise Http404

    expected_date = request.POST.get('expected_date', '').strip() or None
    supplier_product.expected_date = expected_date
    supplier_product.save(update_fields=['expected_date'])
    return JsonResponse({'status': 'ok'})


@login_required
def check_po_date_status(request, dpr_id):
    """Check if po_date exists for a DPR when Customer PO is selected"""
    if request.method != 'GET':
        raise Http404
    try:
        dpr = DPR.objects.get(pk=dpr_id)
    except DPR.DoesNotExist:
        raise Http404

    has_po_date = dpr.po_date is not None
    po_confirmation_date = dpr.po_confirmation_date.strftime('%Y-%m-%d') if dpr.po_confirmation_date else None

    return JsonResponse({
        'status': 'ok',
        'has_po_date': has_po_date,
        'po_date': dpr.po_date.strftime('%Y-%m-%d') if has_po_date else None,
        'po_confirmation_date': po_confirmation_date,
        'today': timezone.localdate().strftime('%Y-%m-%d')
    })


@login_required
def save_po_confirmation_date(request, dpr_id):
    """Save po_confirmation_date when user confirms via modal"""
    if request.method != 'POST':
        raise Http404
    try:
        dpr = DPR.objects.get(pk=dpr_id)
    except DPR.DoesNotExist:
        raise Http404

    po_confirmation_date = request.POST.get('po_confirmation_date', '').strip() or None
    if not po_confirmation_date:
        return JsonResponse({'status': 'error', 'message': 'PO confirmation date is required'}, status=400)

    dpr.po_confirmation_date = po_confirmation_date
    dpr.save(update_fields=['po_confirmation_date'])
    return JsonResponse({'status': 'ok'})


@login_required
def customer_order_edit(request, dpr_id):
    try:
        dpr = DPR.objects.get(pk=dpr_id)
    except DPR.DoesNotExist:
        raise Http404

    customers = Customer.objects.all()

    if request.method == 'POST':
        customer_id = request.POST.get('customer')
        customer = Customer.objects.get(id=customer_id)
        region = request.POST.get('region')
        if customer.region != region:
            messages.error(request, 'Select a customer from the chosen region.')
            products = CustomerProduct.objects.filter(dpr=dpr)
            return render(request, 'customer_order.html', {
                'customers': customers,
                'dpr': dpr,
                'products': products,
                'is_edit': True,
            })

        dpr.customer = customer
        dpr.quotation_number = request.POST.get('quotation_number')
        dpr.quotation_value = request.POST.get('quotation_value') or None
        dpr.quotation_attachment = request.FILES.get('quotation_attachment') or dpr.quotation_attachment
        confirmation_type = request.POST.get('confirmation_type')
        po_number, po_number_error = _resolve_po_number(
            confirmation_type,
            request.POST.get('po_number')
        )
        if po_number_error:
            messages.error(request, po_number_error)
            products = CustomerProduct.objects.filter(dpr=dpr)
            return render(request, 'customer_order.html', {
                'customers': customers,
                'dpr': dpr,
                'products': products,
                'is_edit': True,
            })

        product_names = request.POST.getlist('product_name[]')
        values = request.POST.getlist('value[]')
        _, po_value_error = _validate_po_value_matches_total(
            request.POST.get('po_value'),
            product_names,
            values
        )
        if po_value_error:
            messages.error(request, po_value_error)
            products = CustomerProduct.objects.filter(dpr=dpr)
            return render(request, 'customer_order.html', {
                'customers': customers,
                'dpr': dpr,
                'products': products,
                'is_edit': True,
            })

        dpr.confirmation_type = confirmation_type
        dpr.po_number = po_number
        dpr.po_value = request.POST.get('po_value') or None
        dpr.po_validity = request.POST.get('po_validity') or None
        dpr.po_date = request.POST.get('po_date') or None
        dpr.po_attachment = request.FILES.get('po_attachment') or dpr.po_attachment
        
        # Handle PO confirmation date for Customer PO
        if confirmation_type == 'Customer PO':
            po_confirmation_date = request.POST.get('po_confirmation_date', '').strip()
            if po_confirmation_date:
                dpr.po_confirmation_date = po_confirmation_date
            elif not dpr.po_date:
                # If no po_date exists and no confirmation date provided, set to today
                dpr.po_confirmation_date = timezone.localdate()
        else:
            # For Mail Confirmation, don't modify po_confirmation_date
            pass
        
        dpr.save()

        CustomerProduct.objects.filter(dpr=dpr).delete()

        product_types = request.POST.getlist('product_type[]')
        quantities = request.POST.getlist('quantity[]')
        values = request.POST.getlist('value[]')
        remarks_list = request.POST.getlist('remarks[]')

        for i, product_name in enumerate(product_names):
            if product_name.strip() == '':
                continue
            quantity = quantities[i] if i < len(quantities) else None
            value = values[i] if i < len(values) else None
            remarks = remarks_list[i] if i < len(remarks_list) else None
            attachment = request.FILES.get(f'product_attachment_{i}')
            existing_attachment = request.POST.get(f'existing_attachment_{i}', '')
            if not attachment and existing_attachment:
                attachment = existing_attachment

            CustomerProduct.objects.create(
                dpr=dpr,
                product_name=product_name,
                product_type=product_types[i] if i < len(product_types) else None,
                quantity_ordered=quantity,
                value=value,
                remarks=remarks,
                attachment=attachment
            )

        total_value = CustomerProduct.objects.filter(dpr=dpr).aggregate(
            total=Coalesce(Sum('value'), Decimal('0.00'))
        )['total']
        total_products = CustomerProduct.objects.filter(dpr=dpr).count()
        dpr.po_value = total_value
        dpr.cust_qty_ordered = total_products
        dpr.save(update_fields=['po_value', 'cust_qty_ordered'])

        messages.success(request, 'DPR updated successfully')
        if request.POST.get('save_action') == 'supplier_order':
            return redirect('dpr_supplier', dpr_id=dpr.id)
        return redirect('dpr_view')

    products = CustomerProduct.objects.filter(dpr=dpr)
    context = {
        'customers': customers,
        'dpr': dpr,
        'products': products,
        'is_edit': True,
    }
    return render(request, 'customer_order.html', context)


@login_required
def dpr_supplier(request, dpr_id):
    try:
        dpr = DPR.objects.get(pk=dpr_id)
    except DPR.DoesNotExist:
        raise Http404

    from products.models import SupplierProduct

    products = CustomerProduct.objects.filter(dpr=dpr)
    suppliers = Supplier.objects.all()
    total_customer_quantity = products.aggregate(
        total=Coalesce(Sum('quantity_ordered'), 0)
    )['total']

    supplier_orders = SupplierProduct.objects.filter(
        customer_product__dpr=dpr
    ).select_related('customer_product', 'supplier')

    total_supplier_quantity = supplier_orders.aggregate(
        total=Coalesce(Sum('quantity'), 0)
    )['total']
    if dpr.supplier_qty_ordered != total_supplier_quantity:
        dpr.supplier_qty_ordered = total_supplier_quantity
        dpr.save(update_fields=['supplier_qty_ordered'])

    is_edit = (
        total_supplier_quantity == total_customer_quantity
        and total_customer_quantity > 0
    )

    if request.method == 'POST':
        product_ids = request.POST.getlist('product[]')
        supplier_ids = request.POST.getlist('supplier[]')
        po_values = request.POST.getlist('po_value[]')
        po_dates = request.POST.getlist('po_date[]')
        quantities = request.POST.getlist('quantity[]')
        po_numbers = request.POST.getlist('po_number[]')
        po_validities = request.POST.getlist('po_validity[]')
        supplier_product_ids = request.POST.getlist('supplier_product_id[]')
        quantity_by_product = {}
        total_entered_supplier_qty = 0

        existing_attachments = {
            str(sp.id): sp.po_attachment
            for sp in supplier_orders
        }

        for i in range(len(product_ids)):
            if not product_ids[i] or not supplier_ids[i]:
                continue

            required_values = [
                product_ids[i],
                supplier_ids[i],
                po_values[i] if i < len(po_values) else '',
                po_dates[i] if i < len(po_dates) else '',
                quantities[i] if i < len(quantities) else '',
                po_numbers[i] if i < len(po_numbers) else '',
                po_validities[i] if i < len(po_validities) else '',
            ]
            existing_id = (
                supplier_product_ids[i]
                if i < len(supplier_product_ids)
                else ''
            )
            row_attachment = request.FILES.get(f'po_attachment_{i}')

            if any(v in ('', None) for v in required_values):
                messages.error(request, f"All fields are mandatory in row {i + 1}.")
                return redirect('dpr_supplier', dpr_id=dpr.id)

            if not row_attachment and not existing_attachments.get(existing_id):
                messages.error(
                    request,
                    f"PO attachment is required in row {i + 1}."
                )
                return redirect('dpr_supplier', dpr_id=dpr.id)

            customer_product = CustomerProduct.objects.get(pk=product_ids[i])
            entered_quantity = int(quantities[i] or 0)
            total_entered_supplier_qty += entered_quantity

            if entered_quantity <= 0:
                messages.error(
                    request,
                    f"Quantity must be greater than 0 for product {customer_product.product_name}."
                )
                return redirect('dpr_supplier', dpr_id=dpr.id)

            quantity_by_product[customer_product.id] = (
                quantity_by_product.get(customer_product.id, 0) + entered_quantity
            )
            if quantity_by_product[customer_product.id] > customer_product.quantity_ordered:
                messages.error(
                    request,
                    f"Quantity cannot exceed ordered quantity ({customer_product.quantity_ordered}) for product {customer_product.product_name}."
                )
                return redirect('dpr_supplier', dpr_id=dpr.id)

            if dpr.po_validity and po_validities[i] >= dpr.po_validity.strftime('%Y-%m-%d'):
                messages.warning(
                    request,
                    f"Supplier PO validity in row {i + 1} is on/after customer PO validity ({dpr.po_validity})."
                )
                return redirect('dpr_supplier', dpr_id=dpr.id)

        if total_entered_supplier_qty > total_customer_quantity:
            messages.error(
                request,
                f"Total supplier quantity ({total_entered_supplier_qty}) cannot exceed total customer quantity ({total_customer_quantity})."
            )
            return redirect('dpr_supplier', dpr_id=dpr.id)

        SupplierProduct.objects.filter(customer_product__dpr=dpr).delete()

        for i in range(len(product_ids)):
            if not product_ids[i] or not supplier_ids[i]:
                continue

            customer_product = CustomerProduct.objects.get(pk=product_ids[i])
            supplier = Supplier.objects.get(pk=supplier_ids[i])
            existing_id = (
                supplier_product_ids[i]
                if i < len(supplier_product_ids)
                else ''
            )

            po_attachment = request.FILES.get(f'po_attachment_{i}')
            if not po_attachment and existing_id:
                po_attachment = existing_attachments.get(existing_id)

            SupplierProduct.objects.create(
                customer_product=customer_product,
                supplier=supplier,
                po_value=po_values[i] or 0,
                po_date=po_dates[i] or None,
                po_validity=po_validities[i] or None,
                quantity=quantities[i] or 0,
                po_number=po_numbers[i],
                po_attachment=po_attachment
            )

        _sync_dpr_supplier_qty_ordered(dpr)

        messages.success(
            request,
            'Supplier orders updated successfully' if is_edit else 'Supplier orders saved successfully'
        )
        return redirect('dpr_view')

    context = {
        'dpr': dpr,
        'products': products,
        'suppliers': suppliers,
        'supplier_orders': supplier_orders,
        'total_customer_quantity': total_customer_quantity,
        'total_supplier_quantity': total_supplier_quantity,
        'is_edit': is_edit,
        'is_fully_allocated': is_edit,
    }
    return render(request, 'supplier_order.html', context)


@login_required
def dpr_status_update(request, dpr_id):
    if request.method != 'POST':
        raise Http404
    try:
        dpr = DPR.objects.get(pk=dpr_id)
    except DPR.DoesNotExist:
        raise Http404
    status = request.POST.get('status', '').strip() or None
    dpr.status = status
    dpr.save(update_fields=['status'])
    return JsonResponse({'status': 'ok'})


def _validate_master_phone(phone_number):
    if phone_number and not re.fullmatch(r'\d{10}', phone_number):
        return 'Enter a valid 10-digit mobile number.'
    return None


def _validate_customer_region(region):
    if region not in ('Chennai', 'Hosur'):
        return 'Region is required.'
    return None


@login_required
def customer_details(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        customer_id = request.POST.get('customer_id')
        customer_name = request.POST.get('customer_name', '').strip()
        region = request.POST.get('region', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        address = request.POST.get('address', '').strip()

        if action in ('add', 'edit'):
            if not customer_name:
                messages.error(request, 'Customer Name is required.')
                return redirect('customer_details')
            region_error = _validate_customer_region(region)
            if region_error:
                messages.error(request, region_error)
                return redirect('customer_details')
            phone_error = _validate_master_phone(phone_number)
            if phone_error:
                messages.error(request, phone_error)
                return redirect('customer_details')

        if action == 'add':
            Customer.objects.create(
                customer_name=customer_name,
                region=region,
                phone_number=phone_number or None,
                address=address or None
            )
            messages.success(request, 'Customer added successfully.')
        elif action == 'edit':
            try:
                customer = Customer.objects.get(pk=customer_id)
            except Customer.DoesNotExist:
                raise Http404
            customer.customer_name = customer_name
            customer.region = region
            customer.phone_number = phone_number or None
            customer.address = address or None
            customer.save(update_fields=['customer_name', 'region', 'phone_number', 'address'])
            messages.success(request, 'Customer updated successfully.')
        elif action == 'delete':
            try:
                customer = Customer.objects.get(pk=customer_id)
            except Customer.DoesNotExist:
                raise Http404
            if DPR.objects.filter(customer=customer).exists():
                messages.error(
                    request,
                    'This customer is used in DPR records and cannot be deleted.'
                )
            else:
                customer.delete()
                messages.success(request, 'Customer deleted successfully.')

        return redirect('customer_details')

    customers = Customer.objects.order_by('customer_name')
    return render(request, 'customer_details.html', {'customers': customers})


@login_required
def supplier_details(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        supplier_id = request.POST.get('supplier_id')
        supplier_name = request.POST.get('supplier_name', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        address = request.POST.get('address', '').strip()

        if action in ('add', 'edit'):
            if not supplier_name:
                messages.error(request, 'Supplier Name is required.')
                return redirect('supplier_details')
            phone_error = _validate_master_phone(phone_number)
            if phone_error:
                messages.error(request, phone_error)
                return redirect('supplier_details')

        if action == 'add':
            Supplier.objects.create(
                supplier_name=supplier_name,
                phone_number=phone_number or None,
                address=address or None
            )
            messages.success(request, 'Supplier added successfully.')
        elif action == 'edit':
            try:
                supplier = Supplier.objects.get(pk=supplier_id)
            except Supplier.DoesNotExist:
                raise Http404
            supplier.supplier_name = supplier_name
            supplier.phone_number = phone_number or None
            supplier.address = address or None
            supplier.save(update_fields=['supplier_name', 'phone_number', 'address'])
            messages.success(request, 'Supplier updated successfully.')
        elif action == 'delete':
            try:
                supplier = Supplier.objects.get(pk=supplier_id)
            except Supplier.DoesNotExist:
                raise Http404
            if SupplierProduct.objects.filter(supplier=supplier).exists():
                messages.error(
                    request,
                    'This supplier is used in supplier order records and cannot be deleted.'
                )
            else:
                supplier.delete()
                messages.success(request, 'Supplier deleted successfully.')

        return redirect('supplier_details')

    suppliers = Supplier.objects.order_by('supplier_name')
    return render(request, 'supplier_details.html', {'suppliers': suppliers})

@login_required
def customer_order(request):

    customers = Customer.objects.all()

    if request.method == 'POST':

        customer_id = request.POST.get('customer')

        customer = Customer.objects.get(id=customer_id)

        region = request.POST.get('region')
        if region not in ('Chennai', 'Hosur'):
            messages.error(request, 'Region is required.')
            return render(request, 'customer_order.html', {
                'customers': customers,
                'dpr': None,
                'products': None,
                'is_edit': False,
            })
        if customer.region != region:
            messages.error(request, 'Select a customer from the chosen region.')
            return render(request, 'customer_order.html', {
                'customers': customers,
                'dpr': None,
                'products': None,
                'is_edit': False,
            })

        quotation_number = request.POST.get('quotation_number')

        quotation_value = request.POST.get('quotation_value')

        quotation_attachment = request.FILES.get(
            'quotation_attachment'
        )

        confirmation_type = request.POST.get(
            'confirmation_type'
        )

        po_number, po_number_error = _resolve_po_number(
            confirmation_type,
            request.POST.get('po_number')
        )
        if po_number_error:
            messages.error(request, po_number_error)
            return render(request, 'customer_order.html', {
                'customers': customers,
                'dpr': None,
                'products': None,
                'is_edit': False,
            })

        product_names = request.POST.getlist('product_name[]')
        values = request.POST.getlist('value[]')
        _, po_value_error = _validate_po_value_matches_total(
            request.POST.get('po_value'),
            product_names,
            values
        )
        if po_value_error:
            messages.error(request, po_value_error)
            return render(request, 'customer_order.html', {
                'customers': customers,
                'dpr': None,
                'products': None,
                'is_edit': False,
            })

        po_value = request.POST.get('po_value')

        po_validity = request.POST.get('po_validity')

        po_date = request.POST.get('po_date')

        po_attachment = request.FILES.get('po_attachment')

        # CREATE DPR

        dpr = DPR.objects.create(

            customer=customer,

            quotation_number=quotation_number,

            quotation_value=quotation_value,

            quotation_attachment=quotation_attachment,

            confirmation_type=confirmation_type,

            po_number=po_number,

            po_value=po_value,

            po_validity=po_validity,

            po_date=po_date,

            po_attachment=po_attachment
        )

        # PRODUCT DETAILS

        product_types = request.POST.getlist(
            'product_type[]'
        )

        quantities = request.POST.getlist(
            'quantity[]'
        )

        values = request.POST.getlist(
            'value[]'
        )

        remarks_list = request.POST.getlist(
            'remarks[]'
        )

        attachments = request.FILES.getlist(
            'product_attachment[]'
        )

        for i in range(len(product_names)):

            product_name = product_names[i]

            if product_name.strip() == '':
                continue

            quantity = quantities[i]

            value = values[i]

            remarks = remarks_list[i]

            attachment = None

            if i < len(attachments):
                attachment = attachments[i]

            CustomerProduct.objects.create(

                dpr=dpr,

                product_name=product_name,
                product_type=product_types[i] if i < len(product_types) else None,

                quantity_ordered=quantity,

                value=value,

                remarks=remarks,

                attachment=attachment
            )

        total_value = CustomerProduct.objects.filter(dpr=dpr).aggregate(
            total=Coalesce(Sum('value'), Decimal('0.00'))
        )['total']
        total_products = CustomerProduct.objects.filter(dpr=dpr).count()
        dpr.po_value = total_value
        dpr.cust_qty_ordered = total_products
        dpr.save(update_fields=['po_value', 'cust_qty_ordered'])

        messages.success(
            request,
            'Customer Order Saved Successfully'
        )

        if request.POST.get('save_action') == 'supplier_order':
            return redirect('dpr_supplier', dpr_id=dpr.id)
        return redirect('customer_order')

    context = {
        'customers': customers,
        'dpr': None,
        'products': None,
        'is_edit': False,
    }

    return render(
        request,
        'customer_order.html',
        context
    )

@login_required
def add_customer(request):

    if request.method == 'POST':

        customer_name = request.POST.get(
            'customer_name'
        , '').strip()

        region = request.POST.get(
            'region'
        , '').strip()

        phone_number = request.POST.get(
            'phone_number'
        , '').strip()

        address = request.POST.get(
            'address'
        , '').strip()

        if not customer_name:
            return JsonResponse({
                'status': 'error',
                'message': 'Customer Name is required'
            }, status=400)

        if region not in ('Chennai', 'Hosur'):
            return JsonResponse({
                'status': 'error',
                'message': 'Region is required',
                'field': 'region'
            }, status=400)

        if phone_number and not re.fullmatch(r'\d{10}', phone_number):
            return JsonResponse({
                'status': 'error',
                'message': 'Enter a valid 10-digit mobile number.',
                'field': 'phone_number'
            }, status=400)

        customer = Customer.objects.create(

            customer_name=customer_name,

            region=region,

            phone_number=phone_number or None,

            address=address or None
        )

        return JsonResponse({

            'status': 'success',

            'id': customer.id,

            'name': customer.customer_name,

            'region': customer.region
        })

    return JsonResponse({

        'status': 'error'
    })
    customers = Customer.objects.all()

    context = {
        'customers': customers
    }

    return render(
        request,
        'customer_order.html',
        context
    )


@login_required
def add_supplier(request):
    if request.method == 'POST':
        supplier_name = request.POST.get('supplier_name', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        address = request.POST.get('address', '').strip()

        if not supplier_name:
            return JsonResponse({
                'status': 'error',
                'message': 'Supplier Name is required'
            }, status=400)

        supplier = Supplier.objects.create(
            supplier_name=supplier_name,
            phone_number=phone_number or None,
            address=address or None
        )

        return JsonResponse({
            'status': 'success',
            'id': supplier.id,
            'name': supplier.supplier_name
        })

    return JsonResponse({'status': 'error'})
