from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from .models import CustomUser
from django.contrib.auth.decorators import login_required
from .decorators import role_required
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings
from django.core.mail import EmailMessage
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from customers.models import Customer
from suppliers.models import Supplier
from dpr.models import DPR
from products.models import CustomerProduct, SupplierProduct
from rfq.models import RFQ, RFQProduct, RFQSupplierPrice, RFQQuotation
from django.http import HttpResponse, JsonResponse, Http404
from django.db import transaction
from django.db.models import Sum, Case, When, Value, IntegerField, Q
from django.db.models.functions import Coalesce
from decimal import Decimal, InvalidOperation
from django.utils import timezone
from datetime import timedelta
from io import BytesIO
from pathlib import PurePath
from zipfile import ZIP_DEFLATED, ZipFile
from types import SimpleNamespace
import re


def _pct(part, whole):
    return round((part * 100) / whole) if whole else 0


def _get_rfq_row_alert_class(rfq):
    """
    Determine if RFQ row should be highlighted based on quotation status.

    Returns a CSS class string for row highlighting:
    - 'table-danger': Red background when any product is waiting for price details or quotation not sent after 3 days.
    - 'table-success': Green background when all products are quotation sent.
    - Empty string: No highlighting (normal row)
    """

    if not rfq.mail_date:
        return ''

    products = list(rfq.products.all())
    if not products:
        return ''

    # Check if any product is waiting for price details
    if any(not product.price_known or product.value == 0 for product in products):
        return 'table-danger'

    # Check if all products have quotation sent
    if all(product.price_known and product.value > 0 and product.quotation_email_sent for product in products):
        return 'table-success'

    # Check if quotation is not sent after 3 days from the mail date
    today = timezone.localdate()
    if (today - rfq.mail_date).days >= 3:
        return 'table-danger'

    return ''


def _resolve_po_number(confirmation_type, po_number_raw):
    if confirmation_type == 'Customer PO':
        po_number = (po_number_raw or '').strip()
        if not po_number:
            return None, 'PO Number is required when Order Confirmation is Customer PO.'
        return po_number, None
    return None, None


def _calculate_product_line_value(quantity_raw, rate_raw, row_number):
    try:
        quantity = int(quantity_raw or 0)
    except ValueError:
        raise ValueError(f'Invalid quantity for product in row {row_number}.')
    try:
        rate = Decimal(str(rate_raw or '0')).quantize(Decimal('0.01'))
    except InvalidOperation:
        raise ValueError(f'Invalid rate for product in row {row_number}.')
    return quantity, rate, (Decimal(quantity) * rate).quantize(Decimal('0.01'))


def _validate_po_value_matches_total(po_value_raw, product_names, quantities, rates):
    try:
        po_value = Decimal(str(po_value_raw or '0'))
    except InvalidOperation:
        return False, 'PO Value must be a valid number.'

    total_value = Decimal('0.00')
    for i, product_name in enumerate(product_names):
        if not product_name.strip():
            continue
        try:
            _, _, line_value = _calculate_product_line_value(
                quantities[i] if i < len(quantities) else '0',
                rates[i] if i < len(rates) else '0',
                i + 1
            )
            total_value += line_value
        except ValueError as exc:
            return False, str(exc)

    po_normalized = po_value.quantize(Decimal('0.01'))
    total_normalized = total_value.quantize(Decimal('0.01'))
    if po_normalized != total_normalized:
        return False, (
            f'PO Value ({po_normalized}) must equal Total Value ({total_normalized}).'
        )
    return True, None


def _format_money(value):
    return f"{Decimal(value or 0):,.2f}"


def _get_mes_quote_no(rfq):
    year = rfq.mail_date.year if rfq.mail_date else timezone.localdate().year
    quote_seq = RFQ.objects.filter(id__lte=rfq.id).count()
    return f"MES_Q{quote_seq:04d}/{str(year)[-2:]}-{str(year + 1)[-2:]}"


def _get_mes_enquiry_no(rfq):
    if not rfq.rfq_no:
        return ""
    parts = rfq.rfq_no.split('-')
    if len(parts) == 3 and parts[0] == 'RFQ':
        try:
            year = int(parts[1])
            seq = parts[2]
            return f"MES_RFQ{seq}/{str(year)[-2:]}-{str(year + 1)[-2:]}"
        except ValueError:
            pass
    return rfq.rfq_no


def _get_hsn_code(product):
    # Try looking up by product_type first
    prod_type = (product.product_type or '').lower().strip()
    
    type_hsn_map = {
        'apg steel': '90173029',
        'arg steel': '90173029',
        'apg carbide': '90173029',
        'arg carbide': '90173029',
        'sapg': '90173029',
        'sarg': '90173029',
        'multi-gauge': '90318000',
        'unit std air': '90173029',
        'unit spc air': '90173029',
        'unit std lvdt': '90318000',
        'unit spc lvdt': '90318000',
        'amc': '998719',
        'service': '998349',
        'spares': '90179000',
        'tpg': '90173021',
        'trg': '90173022',
        'stpg': '90173029',
        'strg': '90173029',
        'ppg': '90173021',
        'prg': '90173022',
        'sppg': '90173029',
        'sprg': '90173029',
    }
    
    if prod_type in type_hsn_map:
        return type_hsn_map[prod_type]

    # Fallback to name-based classification if product_type is not set
    name = (product.product_name or '').lower()
    if 'calibration' in name:
        return '998349'
    if 'jobwork' in name or 'labour charges' in name or 'service category' in name:
        return '998898'
    if 'plug gauge' in name:
        if 'special' in name or 'spl' in name or 'plain' not in name:
            if 'special' in name or 'spl' in name:
                return '90173029'
        return '90173021'
    if 'ring gauge' in name:
        if 'special' in name or 'spl' in name or 'plain' not in name:
            if 'special' in name or 'spl' in name:
                return '90173029'
        return '90173022'
    if 'slip gauge' in name:
        return '90173023'
    if 'snap gauge' in name:
        return '90173029'
    if 'measuring pin' in name:
        return '90173029'
    if 'dial snap' in name:
        return '90173029'
    if 'shallow gauge' in name:
        return '90173029'
    if 'three wire' in name:
        return '90173029'
    if 'height piece' in name:
        return '90173029'
    if 'width gauge' in name:
        return '90173029'
    if 'od setting' in name:
        return '90173029'
    if 'sine bar' in name:
        return '90178090'
    if 'sine centre' in name or 'sine center' in name:
        return '90178090'
    if 'air gauge unit' in name:
        return '90173029'
    if 'air plug' in name:
        return '90173029'
    if 'air ring' in name:
        return '90173029'
    if 'comparator stand' in name:
        return '90178090'
    if 'spl.gauge' in name or 'spl gauge' in name or 'special gauge' in name:
        return '90173029'
    if 'parts & accessories' in name or 'part & accessory' in name or 'accessory' in name:
        return '90179000'
    if 'scrap' in name:
        return '72044100'
    if 'other tech' in name or 'scientific' in name:
        return '998349'
    if 'service' in name:
        return '998349'
    if 'gauge' in name:
        return '90173029'
    return '90318000'



def _quote_number_base(quotation_number):
    return re.sub(r'_R\d+$', '', quotation_number or '')


def _quote_number_sequence(quotation_number):
    match = re.match(r'^MES_Q(\d{4})/(\d{2}-\d{2})(?:_R\d+)?$', quotation_number or '')
    if not match:
        return None, None
    return int(match.group(1)), match.group(2)


def _get_next_mes_quote_base_no(rfq):
    year = rfq.mail_date.year if rfq.mail_date else timezone.localdate().year
    year_suffix = f"{str(year)[-2:]}-{str(year + 1)[-2:]}"
    current_seq, _ = _quote_number_sequence(_get_mes_quote_no(rfq))
    max_seq = (current_seq or 1) - 1

    for quote_no in RFQQuotation.objects.values_list('quotation_number', flat=True):
        seq, suffix = _quote_number_sequence(quote_no)
        if suffix == year_suffix and seq:
            max_seq = max(max_seq, seq)

    return f"MES_Q{max_seq + 1:04d}/{year_suffix}"


def _format_mes_quote_no(rfq, revision_number=0, base_quote_no=None):
    base_quote_no = base_quote_no or _get_mes_quote_no(rfq)
    if revision_number:
        return f"{base_quote_no}_R{revision_number}"
    return base_quote_no


def _serialize_quotation_products(products):
    serialized = []
    for product in products:
        serialized.append({
            'product_id': product.id,
            'product_name': product.product_name,
            'product_type': product.product_type or '',
            'quantity': product.quantity,
            'rate_per_unit': str(product.rate_per_unit),
            'value': str(product.value),
            'remarks': product.remarks or '',
            'unit': getattr(product, 'unit', '') or "No's",
            'selected_supplier_name': getattr(product, 'selected_supplier_name', ''),
            'delivery_weeks': getattr(product, 'delivery_weeks', '') or '',
            'installation_charge': getattr(product, 'installation_charge', '') or '',
        })
    return serialized


def _deserialize_quotation_products(products_snapshot):
    deserialized = []
    for item in products_snapshot:
        deserialized.append(SimpleNamespace(
            id=item.get('product_id'),
            product_name=item.get('product_name'),
            product_type=item.get('product_type'),
            price_known=True,
            quantity=item.get('quantity'),
            rate_per_unit=Decimal(item.get('rate_per_unit', 0)),
            value=Decimal(item.get('value', 0)),
            remarks=item.get('remarks'),
            unit=item.get('unit', "No's"),
            selected_supplier_name=item.get('selected_supplier_name', ''),
            delivery_weeks=item.get('delivery_weeks', ''),
            installation_charge=item.get('installation_charge', ''),
        ))
    return deserialized


def _product_snapshot_ids(snapshot):
    return {
        str(product.get('product_id'))
        for product in (snapshot or [])
        if product.get('product_id') is not None
    }


def _find_latest_matching_quotation(rfq, product_ids, email_sent=None):
    selected_ids = {str(product_id) for product_id in product_ids}
    queryset = RFQQuotation.objects.filter(rfq=rfq).order_by('-revision_number', '-created_at')
    if email_sent is not None:
        queryset = queryset.filter(email_sent=email_sent)
    for quotation in queryset:
        if _product_snapshot_ids(quotation.products_snapshot) == selected_ids:
            return quotation
    return None


def _find_latest_overlapping_quotation(rfq, product_ids, email_sent=None):
    selected_ids = {str(product_id) for product_id in product_ids}
    if not selected_ids:
        return None

    queryset = RFQQuotation.objects.filter(rfq=rfq).order_by('-created_at', '-id')
    if email_sent is not None:
        queryset = queryset.filter(email_sent=email_sent)
    for quotation in queryset:
        if _product_snapshot_ids(quotation.products_snapshot) & selected_ids:
            return quotation
    return None


def _next_revision_number_for_quote_base(rfq, base_quote_no):
    latest_revision = 0
    for quote_no in RFQQuotation.objects.filter(rfq=rfq).values_list('quotation_number', flat=True):
        if _quote_number_base(quote_no) != base_quote_no:
            continue
        match = re.search(r'_R(\d+)$', quote_no or '')
        latest_revision = max(latest_revision, int(match.group(1)) if match else 0)
    return latest_revision + 1


def _create_rfq_quotation_record(rfq, products, product_ids, email_sent=False):
    overlapping_quotation = _find_latest_overlapping_quotation(rfq, product_ids)

    if overlapping_quotation:
        base_quote_no = _quote_number_base(overlapping_quotation.quotation_number)
        revision_number = _next_revision_number_for_quote_base(rfq, base_quote_no)
        quotation_number = _format_mes_quote_no(
            rfq,
            revision_number,
            base_quote_no=base_quote_no
        )
    else:
        revision_number = 0
        quotation_number = _get_mes_quote_no(rfq)
        if RFQQuotation.objects.filter(rfq=rfq).exists() or RFQQuotation.objects.filter(quotation_number=quotation_number).exists():
            quotation_number = _get_next_mes_quote_base_no(rfq)

    quotation = RFQQuotation.objects.create(
        rfq=rfq,
        quotation_number=quotation_number,
        revision_number=revision_number,
        products_snapshot=_serialize_quotation_products(products),
        email_sent=email_sent,
    )
    return quotation

def _build_selected_quotation_products(rfq, product_ids, supplier_price_ids, mes_rates=None, delivery_weeks=None, installation_charge=None):
    selected_product_ids = {
        int(product_id)
        for product_id in product_ids
        if str(product_id).isdigit()
    }
    selected_supplier_price_ids = [
        int(price_id)
        for price_id in supplier_price_ids
        if str(price_id).isdigit()
    ]

    mes_rates_by_product = {}
    if mes_rates and product_ids:
        for pid, rate in zip(product_ids, mes_rates):
            if pid and rate:
                try:
                    mes_rates_by_product[int(pid)] = Decimal(str(rate))
                except (ValueError, TypeError, InvalidOperation):
                    pass

    supplier_prices = list(
        RFQSupplierPrice.objects.select_related('product', 'supplier').filter(
            product__rfq=rfq,
            id__in=selected_supplier_price_ids
        ).order_by('product_id', 'supplier__supplier_name')
    )
    supplier_prices_by_product = {}
    for supplier_price in supplier_prices:
        selected_product_ids.add(supplier_price.product_id)
        supplier_prices_by_product.setdefault(supplier_price.product_id, []).append(supplier_price)

    products = list(
        RFQProduct.objects.filter(
            rfq=rfq,
            id__in=selected_product_ids
        ).prefetch_related('supplier_prices__supplier').order_by('id')
    )

    quotation_products = []
    for product in products:
        custom_mes_rate = mes_rates_by_product.get(product.id)
        selected_prices = supplier_prices_by_product.get(product.id, [])
        if selected_prices:
            for supplier_price in selected_prices:
                if custom_mes_rate is not None:
                    rate = custom_mes_rate
                elif product.rate_per_unit and product.rate_per_unit > 0:
                    rate = product.rate_per_unit
                else:
                    rate = supplier_price.price
                value = Decimal(product.quantity * rate)
                quotation_products.append(SimpleNamespace(
                    id=product.id,
                    product_name=product.product_name,
                    product_type=product.product_type,
                    price_known=True,
                    quotation_email_sent=product.quotation_email_sent,
                    quantity=product.quantity,
                    rate_per_unit=rate,
                    value=value,
                    remarks=product.remarks,
                    selected_supplier_name=supplier_price.supplier.supplier_name,
                    unit=getattr(product, 'unit', None) or "No's",
                    delivery_weeks=delivery_weeks,
                    installation_charge=installation_charge,
                ))
        else:
            if custom_mes_rate is not None:
                rate = custom_mes_rate
            elif product.rate_per_unit and product.rate_per_unit > 0:
                rate = product.rate_per_unit
            else:
                rate = Decimal('0.00')
            value = Decimal(product.quantity * rate)
            quotation_products.append(SimpleNamespace(
                id=product.id,
                product_name=product.product_name,
                product_type=product.product_type,
                price_known=product.price_known,
                quotation_email_sent=product.quotation_email_sent,
                quantity=product.quantity,
                rate_per_unit=rate,
                value=value,
                remarks=product.remarks,
                selected_supplier_name='',
                unit=getattr(product, 'unit', None) or "No's",
                delivery_weeks=delivery_weeks,
                installation_charge=installation_charge,
            ))

    return quotation_products, [product.id for product in products]

def _build_rfq_quotation_pdf(rfq, products, quote_no=None):
    from xml.sax.saxutils import escape as xml_escape
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.pdfgen import canvas
    from datetime import timedelta

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=14 * mm,
        leftMargin=14 * mm,
        topMargin=46 * mm,  # Increased top margin to clear canvas headers
        bottomMargin=15 * mm,
    )
    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        'MESNormal',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        alignment=TA_LEFT,
    )
    small = ParagraphStyle(
        'MESSmall',
        parent=normal,
        fontSize=7.5,
        leading=9,
    )
    title_style = ParagraphStyle(
        'MESTitle',
        parent=normal,
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=14,
        alignment=TA_CENTER,
    )
    investment_title_style = ParagraphStyle(
        'MESInvestmentTitle',
        parent=normal,
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=13,
        alignment=TA_CENTER,
    )
    terms_title_style = ParagraphStyle(
        'MESTermsTitle',
        parent=normal,
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=13,
        alignment=TA_LEFT,
    )
    centered = ParagraphStyle(
        'MESCentered',
        parent=normal,
        alignment=TA_CENTER,
    )
    right = ParagraphStyle(
        'MESRight',
        parent=normal,
        alignment=TA_RIGHT,
    )

    quote_no = quote_no or _get_mes_quote_no(rfq)
    enquiry_no = _get_mes_enquiry_no(rfq)
    quote_date = timezone.localdate().strftime('%d-%m-%Y')

    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.pages = []

        def showPage(self):
            self.pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            page_count = len(self.pages)
            for page in self.pages:
                self.__dict__.update(page)
                
                # Draw Header
                self.saveState()
                # Draw Logo Box / Image
                import os
                logo_path = os.path.join(settings.BASE_DIR, 'static', 'images', 'mes_logo.jpg')
                if os.path.exists(logo_path):
                    self.drawImage(logo_path, 14 * mm, self._pagesize[1] - 32 * mm, width=29 * mm, height=20 * mm)
                else:
                    self.setFillColor(colors.black)
                    self.rect(14 * mm, self._pagesize[1] - 32 * mm, 20 * mm, 20 * mm, fill=True, stroke=False)
                    
                    # Draw Logo Text "MES"
                    self.setFillColor(colors.white)
                    self.setFont("Helvetica-Bold", 24)
                    self.drawCentredString(14 * mm + 10 * mm, self._pagesize[1] - 32 * mm + 6 * mm, "MES")
                
                # Draw Company Info
                self.setFillColor(colors.black)
                self.setFont("Helvetica-Bold", 13)
                self.drawCentredString(self._pagesize[0] / 2.0 + 10 * mm, self._pagesize[1] - 15 * mm, "METROLOGY ENGINEERING SOLUTIONS")
                
                self.setFont("Helvetica", 8)
                self.drawCentredString(self._pagesize[0] / 2.0 + 10 * mm, self._pagesize[1] - 19 * mm, "NO.684/9, Sri Sai Jayalakshmi Complex, Maruthi Nagar,")
                self.drawCentredString(self._pagesize[0] / 2.0 + 10 * mm, self._pagesize[1] - 22 * mm, "2nd Cross, Dharga, Opposite to Sathya mess, Hosur, Krishnagiri, Tamil Nadu - 635109")
                self.drawCentredString(self._pagesize[0] / 2.0 + 10 * mm, self._pagesize[1] - 25 * mm, "Phone : +91-965-577-8807 / +91-965-577-8871")
                self.drawCentredString(self._pagesize[0] / 2.0 + 10 * mm, self._pagesize[1] - 28 * mm, "Email-ID : info@mesinstruments.co.in | Web : www.mesinstruments.co.in")
                self.drawCentredString(self._pagesize[0] / 2.0 + 10 * mm, self._pagesize[1] - 31 * mm, "GST : 33ABKFM1033E1ZS | PAN : ABKFM1033E")
                
                # Draw thick black line under header
                self.setLineWidth(1)
                self.line(14 * mm, self._pagesize[1] - 34 * mm, self._pagesize[0] - 14 * mm, self._pagesize[1] - 34 * mm)
                
                # Draw Quote No and Date under line
                self.setFillColor(colors.black)
                self.setFont("Helvetica", 9)
                self.drawString(14 * mm, self._pagesize[1] - 39 * mm, f"Quote No : {quote_no}")
                self.drawRightString(self._pagesize[0] - 14 * mm, self._pagesize[1] - 39 * mm, f"Date : {quote_date}")
                
                # Draw a thin separator line under Quote No and Date
                self.setLineWidth(0.5)
                self.line(14 * mm, self._pagesize[1] - 41 * mm, self._pagesize[0] - 14 * mm, self._pagesize[1] - 41 * mm)
                self.restoreState()

                # Draw footer page number (Page X of Y)
                self.saveState()
                self.setFont("Helvetica", 9)
                text = f"Page {self._pageNumber} of {page_count}"
                self.drawRightString(self._pagesize[0] - 14 * mm, 12 * mm, text)
                self.restoreState()
                
                super().showPage()
            super().save()

    story = []
    def pdf_text(value):
        return xml_escape(str(value or ''))

    customer = rfq.customer
    customer_address = customer.address or ''
    customer_phone = customer.phone_number or '-'

    story.append(Paragraph('<b><u>Quotation (Confidential)</u></b>', title_style))
    story.append(Spacer(1, 4))
    story.append(Paragraph(f'Quote No : {quote_no}', normal))
    story.append(Paragraph(f'Enquiry No : {enquiry_no}', normal))
    story.append(Spacer(1, 4))
    story.append(Paragraph('<b>To:</b>', normal))
    story.append(Paragraph(f'M/s. {pdf_text(customer.customer_name)}', normal))
    if customer_address:
        story.append(Paragraph(pdf_text(customer_address).replace('\n', '<br/>'), normal))
    if customer.region:
        if customer.region.lower() not in customer_address.lower():
            story.append(Paragraph(pdf_text(customer.region), normal))
            
    # Include GSTIN from customer master or fallback to address check
    customer_gstin = (getattr(customer, 'gstin', None) or '').strip()
    if customer_gstin:
        if customer_gstin.upper().startswith('GST'):
            story.append(Paragraph(pdf_text(customer_gstin), normal))
        else:
            story.append(Paragraph(f'GSTIN : {pdf_text(customer_gstin)}', normal))
    else:
        gstin_line = None
        for line in customer_address.split('\n'):
            if 'gst' in line.lower():
                gstin_line = line.strip()
                break
        if gstin_line:
            story.append(Paragraph(pdf_text(gstin_line), normal))
        else:
            story.append(Paragraph('GSTIN : -', normal))
        
    story.append(Paragraph('Kind Attension : -', normal))
    story.append(Paragraph(f'Phone :{pdf_text(customer_phone)}', normal))
    if customer.email:
        story.append(Paragraph(f'Email-ID :{pdf_text(customer.email)}', normal))

    story.append(Spacer(1, 10))
    story.append(Paragraph('Dear Sir,', normal))
    story.append(Paragraph(
        '&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;As per your enquiry, we are glad to submit our best offer. Assuring of our best and prompt services at all times.',
        normal
    ))
    story.append(Spacer(1, 10))
    story.append(Paragraph('<b><u>Your Investment</u></b>', investment_title_style))
    story.append(Spacer(1, 6))

    table_data = [[
        Paragraph('<b>S.No</b>', centered),
        Paragraph('<b>Product</b>', centered),
        Paragraph('<b>Description</b>', centered),
        Paragraph('<b>HSN/SAC</b>', centered),
        Paragraph('<b>Quantity</b>', centered),
        Paragraph('<b>Unit</b>', centered),
        Paragraph('<b>Rate</b>', centered),
        Paragraph('<b>Total(Rs.)</b>', centered),
    ]]

    subtotal = Decimal('0.00')
    for index, product in enumerate(products, start=1):
        line_total = Decimal(product.value or 0).quantize(Decimal('0.01'))
        subtotal += line_total
        description_lines = [f'<b>{pdf_text(product.product_name)}</b>']
        if product.remarks:
            description_lines.append(pdf_text(product.remarks).replace('\n', '<br/>'))
            
        hsn_code = _get_hsn_code(product)
        
        table_data.append([
            Paragraph(str(index), centered),
            Paragraph(pdf_text(product.product_type or 'P0011'), centered),
            Paragraph('<br/>'.join(description_lines), small),
            Paragraph(hsn_code, centered),
            Paragraph(str(product.quantity), centered),
            Paragraph(pdf_text(getattr(product, 'unit', None) or "No's"), centered),
            Paragraph(_format_money(product.rate_per_unit), centered),
            Paragraph(_format_money(line_total), centered),
        ])

    product_table = Table(
        table_data,
        colWidths=[10 * mm, 20 * mm, 54 * mm, 20 * mm, 16 * mm, 14 * mm, 22 * mm, 26 * mm],
        repeatRows=1,
        style=TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#a7d3ef')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 3),
            ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ])
    )
    story.append(product_table)
    story.append(Spacer(1, 8))


    story.append(Paragraph('<b><u>Our Terms & Conditions</u></b>', terms_title_style))
    story.append(Spacer(1, 4))
    
    # Calculate quotation validity date (60 days from today)
    valid_till = (timezone.localdate() + timedelta(days=60)).strftime('%d/%m/%Y')
    
    has_sapg = False
    has_carbide = False
    has_steel = False
    has_tpg_spares = False
    has_amc_service = False
    delivery_weeks = None

    for p in products:
        pt = (getattr(p, 'product_type', '') or '').strip().lower()
        if pt in ('sapg', 'sarg', 'multi-gauge', 'multi gauge'):
            has_sapg = True
            p_weeks = getattr(p, 'delivery_weeks', None)
            if p_weeks:
                delivery_weeks = p_weeks
        elif pt in ('apg carbide', 'arg carbide') or 'carbide' in pt:
            has_carbide = True
        elif pt in ('apg steel', 'arg steel') or 'steel' in pt:
            has_steel = True
        elif any(x in pt for x in ('tpg', 'trg', 'ppg', 'prg', 'spares')):
            has_tpg_spares = True
        elif any(x in pt for x in ('amc', 'service')):
            has_amc_service = True

    if has_sapg:
        weeks_val = str(delivery_weeks or '3').strip()
        delivery_str = f'Delivery : {weeks_val} Weeks'
    elif has_carbide:
        delivery_str = 'Delivery : 4 to 5 weeks'
    elif has_steel:
        delivery_str = 'Delivery : 3 to 4 weeks'
    elif has_tpg_spares:
        delivery_str = 'Delivery : 2 weeks'
    elif has_amc_service:
        delivery_str = 'Delivery : 1 to 2 weeks'
    else:
        delivery_str = 'Delivery : 3 Weeks'

    has_air_or_multi = False
    for p in products:
        pt = (getattr(p, 'product_type', '') or '').strip().lower()
        if pt in ('unit std air', 'unit spc air', 'multi-gauge', 'multi gauge'):
            has_air_or_multi = True
            break

    installation_charge_val = None
    for p in products:
        val = getattr(p, 'installation_charge', None)
        if val:
            installation_charge_val = str(val).strip()
            break

    if has_air_or_multi and installation_charge_val:
        installation_str = f'Installation Charge : Rs. {installation_charge_val}'
    else:
        installation_str = 'Installation Charge : Nil'

    terms = [
        delivery_str,
        f"Payment : {customer.payment_terms} Week{'s' if str(customer.payment_terms) != '1' else ''}" if customer.payment_terms else 'Payment : 30 Days Against Invoice',
        'Goods & Service Tax(GST) : 18% Extra as Applicable',
        'Dispatch Mode : NIL' if has_amc_service else 'Dispatch Mode : By Courier',
        'Packing & Forwarding : 2%',
        installation_str,
       'Discount : Negotiable',
        f'Quotation Validity : This offer is Valid till {valid_till}',
        'Purchase Order : Purchase Order must be send to info@mesinstruments.co.in',
        'Cancellation: Once Order confirmed, orders cannot be cancelled or altered.',
        'Force Majeure: The Company is not liable for delay or failure due to natural calamities, strikes, or transport issues.',
        'Confidentiality: All technical documents and data shared are confidential and shall not be disclosed without consent.',
        'Jurisdiction: All disputes arising out of or in connection with this Quotation shall be settled by arbitration in Chennai, India. in accordance with the Indian Arbitration & Conciliation Act rules. The decision shall be final and binding on both parties.',
    ]
    for index, term in enumerate(terms, start=1):
        story.append(Paragraph(f'{index}. {term}', small))

    doc.build(story, canvasmaker=NumberedCanvas)
    buffer.seek(0)
    return buffer


def _get_default_supplier_email_subject():
    return 'Price request for {rfq_no}'


def _get_default_supplier_email_body():
    return (
        'Dear Sir/Madam,\n\n'
        'Greetings from Metrology Engineering Solutions.\n\n'
        'We are interested in procuring the following / Attached items and request you to kindly provide your best quotation.\n\n\n'
        '" Product details "\n\n'
        '{products}\n\n'
        'We look forward to your prompt response.'
    )


def _send_rfq_supplier_price_requests(
    rfq,
    product_rows,
    subject_template=None,
    body_template=None,
    email_attachment=None,
    to_emails=None,
    cc_emails=None,
):
    supplier_product_map = {}
    for product_row in product_rows:
        if product_row.get('price_known'):
            continue
        suppliers = product_row.get('suppliers') or []
        for supplier in suppliers:
            supplier_product_map.setdefault(supplier, []).append(product_row)

    sent_count = 0
    failed_suppliers = []
    attachment_payload = None
    if email_attachment:
        attachment_payload = (
            email_attachment.name,
            email_attachment.read(),
            getattr(email_attachment, 'content_type', None) or 'application/octet-stream'
        )

    def render_template(template, context):
        rendered = template or ''
        for key, value in context.items():
            rendered = rendered.replace('{' + key + '}', str(value))
        return rendered

    def normalize_email_list(values):
        normalized = []
        seen = set()
        for value in values or []:
            email_value = (value or '').strip()
            if not email_value:
                continue
            key = email_value.lower()
            if key in seen:
                continue
            normalized.append(email_value)
            seen.add(key)
        return normalized

    selected_supplier_emails = normalize_email_list(
        supplier.email for supplier in supplier_product_map.keys() if supplier.email
    )
    selected_supplier_email_keys = {email.lower() for email in selected_supplier_emails}

    for supplier, rows in supplier_product_map.items():
        if not supplier.email:
            failed_suppliers.append(f'{supplier.supplier_name} (missing email)')
            continue

        product_lines = []
        for index, row in enumerate(rows, start=1):
            product_lines.append(
                f"{index}. {row['product_name']} | Type: {row['product_type'] or '-'} | "
                f"Qty: {row['quantity']} | Remarks: {row['remarks'] or '-'}"
            )

        context = {
            'supplier_name': supplier.supplier_name,
            'rfq_no': rfq.rfq_no,
            'customer_name': rfq.customer.customer_name,
            'customer_email': rfq.customer.email or '',
            'enquiry_details': rfq.enquiry_details,
            'products': '\n'.join(product_lines),
        }
        subject = render_template(
            subject_template or _get_default_supplier_email_subject(),
            context
        )
        message = render_template(
            body_template or _get_default_supplier_email_body(),
            context
        )

        extra_to_emails = [
            email_address
            for email_address in (to_emails or [])
            if email_address.lower() not in selected_supplier_email_keys
        ]
        recipient_list = normalize_email_list([supplier.email, *extra_to_emails])
        cc_list = normalize_email_list(cc_emails or [])

        try:
            email = EmailMessage(
                subject=subject,
                body=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=recipient_list,
                cc=cc_list,
            )
            if attachment_payload:
                email.attach(*attachment_payload)
            email.send(fail_silently=False)
            sent_count += 1
        except Exception as exc:
            failed_suppliers.append(f'{supplier.supplier_name} ({str(exc)[:180]})')

    return sent_count, failed_suppliers


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
    if status == 'invoice_pending':
        return 'table-info'
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
    if status == 'invoice_pending':
        return 'invoice_pending'
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


@csrf_exempt
def user_login(request):
    if request.method == 'GET' and request.user.is_authenticated:
        logout(request)

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



@login_required
def user_logout(request):
    logout(request)
    return redirect('login')



@role_required('ADMIN')
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
    total_rfq_count = RFQ.objects.count()
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
    customer_invoice_pending_count = CustomerProduct.objects.filter(status='invoice_pending').count()
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

    # RFQ metrics calculations
    all_rfqs = list(RFQ.objects.prefetch_related('products', 'quotations').all())
    confirmed_dprs = list(DPR.objects.exclude(po_attachment='').exclude(po_attachment__isnull=True).values_list('quotation_number', flat=True))
    confirmed_quote_set = set()
    for dpr_quote_str in confirmed_dprs:
        if dpr_quote_str:
            for part in dpr_quote_str.split(','):
                confirmed_quote_set.add(part.strip())

    pending_rfqs = []
    confirmed_rfqs = []
    overdue_rfqs = []

    for rfq in all_rfqs:
        rfq.row_class = _get_rfq_row_alert_class(rfq)
        rfq.is_overdue = (today - rfq.mail_date).days >= 3 if rfq.mail_date else False
        rfq_quote_nos = list(rfq.quotations.values_list('quotation_number', flat=True))
        is_po_confirmed = any(q_no in confirmed_quote_set for q_no in rfq_quote_nos)
        is_quote_submitted = (
            rfq.row_class == 'table-success'
            or rfq.quotation_email_sent
            or rfq.quotations.filter(email_sent=True).exists()
            or is_po_confirmed
        )

        if is_quote_submitted:
            confirmed_rfqs.append(rfq)
        elif rfq.row_class == 'table-danger':
            overdue_rfqs.append(rfq)
        else:
            pending_rfqs.append(rfq)

    rfq_confirmed_count = len(confirmed_rfqs)
    rfq_quotation_sent_count = len(confirmed_rfqs)
    rfq_overdue_count = len(overdue_rfqs)
    rfq_quotation_pending_count = len(pending_rfqs)
    rfq_quotation_not_sent_count = len(pending_rfqs)

    rfq_confirmed_pct = _pct(rfq_confirmed_count, total_rfq_count)
    rfq_quotation_sent_pct = _pct(rfq_quotation_sent_count, total_rfq_count)
    rfq_overdue_pct = _pct(rfq_overdue_count, total_rfq_count)
    rfq_price_pending_count = rfq_overdue_count
    rfq_price_pending_pct = rfq_overdue_pct
    rfq_quotation_pending_pct = _pct(rfq_quotation_pending_count, total_rfq_count)

    # Accounts financial metrics calculations
    cust_products = list(CustomerProduct.objects.select_related('dpr', 'dpr__customer').all())
    supp_products = list(SupplierProduct.objects.filter(
        Q(quantity_received__gt=0) |
        Q(quantity_not_ok__gt=0) |
        Q(status__in=['delivered', 'partially_delivered']) |
        (Q(supplier_invoice_number__isnull=False) & ~Q(supplier_invoice_number='')) |
        Q(supplier_bill_amount__gt=0) |
        (Q(bill_attachment__isnull=False) & ~Q(bill_attachment=''))
    ).select_related('supplier', 'customer_product', 'customer_product__dpr').distinct())

    total_cust_invoice_val = Decimal('0.00')
    total_cust_received_val = Decimal('0.00')
    total_cust_outstanding_val = Decimal('0.00')
    total_cust_overdue_val = Decimal('0.00')

    cust_paid_count = 0
    cust_partially_paid_count = 0
    cust_not_received_count = 0
    cust_overdue_count = 0
    cust_due_soon_count = 0

    for p in cust_products:
        inv_date = p.invoice_date or (p.dpr.po_date if p.dpr else None) or (p.dpr.created_at.date() if p.dpr and p.dpr.created_at else today)
        cust = p.dpr.customer if p.dpr else None
        terms_str = (cust.payment_terms or '').lower() if cust else ''
        days = 30
        match = re.search(r'(\d+)', terms_str)
        if match:
            try:
                days = int(match.group(1))
            except ValueError:
                pass
        terms_date = inv_date + timedelta(days=days) if inv_date else today

        po_val = p.value or Decimal('0.00')
        rec_amt = p.received_amount or Decimal('0.00')
        rem_amt = max(po_val - rec_amt, Decimal('0.00'))

        total_cust_invoice_val += po_val
        total_cust_received_val += rec_amt
        total_cust_outstanding_val += rem_amt

        is_paid = (p.payment_status == 'amount_received' or rec_amt >= po_val)
        is_due_soon = not is_paid and (today <= terms_date <= today + timedelta(days=7))
        is_overdue = not is_paid and (today > terms_date)

        if is_paid:
            cust_paid_count += 1
        else:
            if p.payment_status == 'partially_received':
                cust_partially_paid_count += 1
            else:
                cust_not_received_count += 1

            if is_due_soon:
                cust_due_soon_count += 1
            if is_overdue:
                cust_overdue_count += 1
                total_cust_overdue_val += rem_amt

    total_supp_invoice_val = Decimal('0.00')
    total_supp_received_val = Decimal('0.00')
    total_supp_outstanding_val = Decimal('0.00')
    total_supp_overdue_val = Decimal('0.00')

    supp_paid_count = 0
    supp_partially_paid_count = 0
    supp_not_received_count = 0
    supp_overdue_count = 0
    supp_due_soon_count = 0

    for p in supp_products:
        dpr_created = p.customer_product.dpr.created_at.date() if (p.customer_product and p.customer_product.dpr and p.customer_product.dpr.created_at) else today
        inv_date = p.invoice_date or p.po_date or dpr_created
        supp = p.supplier
        terms_str = (supp.payment_terms or '').strip().lower() if supp else ''
        days = 30
        match = re.search(r'(\d+)', terms_str)
        if match:
            try:
                val = int(match.group(1))
                if 'day' in terms_str:
                    days = val
                elif 'month' in terms_str:
                    days = val * 30
                else:
                    days = val * 7
            except ValueError:
                pass
        terms_date = inv_date + timedelta(days=days) if inv_date else today

        po_val = p.supplier_bill_amount if (p.supplier_bill_amount is not None and p.supplier_bill_amount > Decimal('0.00')) else (p.po_value or Decimal('0.00'))
        rec_amt = p.received_amount or Decimal('0.00')
        rem_amt = max(po_val - rec_amt, Decimal('0.00'))

        total_supp_invoice_val += po_val
        total_supp_received_val += rec_amt
        total_supp_outstanding_val += rem_amt

        is_paid = (p.payment_status == 'amount_received' or rec_amt >= po_val)
        is_due_soon = not is_paid and (today <= terms_date <= today + timedelta(days=7))
        is_overdue = not is_paid and (today > terms_date)

        if is_paid:
            supp_paid_count += 1
        else:
            if p.payment_status == 'partially_received':
                supp_partially_paid_count += 1
            else:
                supp_not_received_count += 1

            if is_due_soon:
                supp_due_soon_count += 1
            if is_overdue:
                supp_overdue_count += 1
                total_supp_overdue_val += rem_amt

    cust_collection_pct = _pct(float(total_cust_received_val), float(total_cust_invoice_val))
    supp_payment_pct = _pct(float(total_supp_received_val), float(total_supp_invoice_val))

    return render(request, 'dashboard.html', {
        'total_dpr_count': total_dpr_count,
        'total_rfq_count': total_rfq_count,
        'total_customer_products': total_customer_products,
        'total_supplier_products': total_supplier_products,
        'pending_mail_confirmation_count': pending_mail_confirmation_count,
        'customer_within_7_days_count': customer_within_7_days_count,
        'customer_expired_count': customer_expired_count,
        'customer_delivered_count': customer_delivered_count,
        'customer_invoice_pending_count': customer_invoice_pending_count,
        'customer_partial_count': customer_partial_count,
        'customer_cancelled_count': customer_cancelled_count,
        'customer_pending_count': customer_pending_count,
        'supplier_within_7_days_count': supplier_within_7_days_count,
        'supplier_expired_count': supplier_expired_count,
        'supplier_delivered_count': supplier_delivered_count,
        'supplier_not_delivered_count': total_supplier_products - supplier_delivered_count,
        'supplier_partial_count': supplier_partial_count,
        'supplier_cancelled_count': supplier_cancelled_count,
        'supplier_pending_count': supplier_pending_count,
        'customer_delivery_pct': customer_delivery_pct,
        'supplier_delivery_pct': supplier_delivery_pct,
        'mail_confirmation_pct': mail_confirmation_pct,
        'overall_delivery_pct': overall_delivery_pct,
        'supplier_order_pending_difference': supplier_order_pending_difference,
        'rfq_quotation_not_sent_count': rfq_quotation_not_sent_count,
        'rfq_confirmed_count': rfq_confirmed_count,
        'rfq_quotation_sent_count': rfq_quotation_sent_count,
        'rfq_price_pending_count': rfq_price_pending_count,
        'rfq_overdue_count': rfq_overdue_count,
        'rfq_quotation_pending_count': rfq_quotation_pending_count,
        'rfq_confirmed_pct': rfq_confirmed_pct,
        'rfq_quotation_sent_pct': rfq_quotation_sent_pct,
        'rfq_price_pending_pct': rfq_price_pending_pct,
        'rfq_overdue_pct': rfq_overdue_pct,
        'rfq_quotation_pending_pct': rfq_quotation_pending_pct,

        # Financial accounts context variables
        'total_cust_invoice_val': float(total_cust_invoice_val),
        'total_cust_received_val': float(total_cust_received_val),
        'total_cust_outstanding_val': float(total_cust_outstanding_val),
        'total_cust_overdue_val': float(total_cust_overdue_val),
        'cust_paid_count': cust_paid_count,
        'cust_partially_paid_count': cust_partially_paid_count,
        'cust_not_received_count': cust_not_received_count,
        'cust_due_soon_count': cust_due_soon_count,
        'cust_overdue_count': cust_overdue_count,
        'cust_collection_pct': cust_collection_pct,
        'total_supp_invoice_val': float(total_supp_invoice_val),
        'total_supp_received_val': float(total_supp_received_val),
        'total_supp_outstanding_val': float(total_supp_outstanding_val),
        'total_supp_overdue_val': float(total_supp_overdue_val),
        'supp_paid_count': supp_paid_count,
        'supp_partially_paid_count': supp_partially_paid_count,
        'supp_not_received_count': supp_not_received_count,
        'supp_due_soon_count': supp_due_soon_count,
        'supp_overdue_count': supp_overdue_count,
        'supp_payment_pct': supp_payment_pct,
    })


@role_required('ADMIN', 'PURCHASE')
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
        is_completed = (
            dpr.total_quantity_ordered > 0
            and dpr.total_quantity_ordered == dpr.customer_qty_delivered
            and dpr.supplier_quantity_ordered == dpr.supplier_qty_received
        )
        is_supplier_order_pending = (
            dpr.po_date is not None
            and dpr.po_date <= today
            and dpr.total_quantity_ordered > dpr.supplier_quantity_ordered
        )
        is_qty_matched = (
            dpr.total_quantity_ordered > 0
            and dpr.supplier_quantity_ordered == dpr.total_quantity_ordered
        )

        dpr.filter_states = []
        if is_completed:
            dpr.filter_states.append('completed')
        if dpr.validity_state == 'expired':
            dpr.filter_states.append('after_due')
        if dpr.validity_state == 'due_soon':
            dpr.filter_states.append('due_soon')
        if is_supplier_order_pending:
            dpr.filter_states.append('supplier_order_pending')
        if dpr.is_alert_row:
            dpr.filter_states.append('mail_alert')
        if is_qty_matched:
            dpr.filter_states.append('qty_matched')
        if not dpr.filter_states:
            dpr.filter_states.append('normal')

        if is_completed:
            dpr.filter_state = 'completed'
        elif dpr.validity_state == 'expired':
            dpr.filter_state = 'after_due'
        elif dpr.validity_state == 'due_soon':
            dpr.filter_state = 'due_soon'
        elif is_supplier_order_pending:
            dpr.filter_state = 'supplier_order_pending'
        elif dpr.is_alert_row:
            dpr.filter_state = 'mail_alert'
        elif is_qty_matched:
            dpr.filter_state = 'qty_matched'
        else:
            dpr.filter_state = 'normal'
        dpr.row_class = _get_dpr_row_class(dpr.filter_state)

    dprs.sort(key=lambda dpr: 1 if dpr.filter_state == 'completed' else 0)

    return render(
        request,
        'dpr_view.html',
        {
            'dprs': dprs,
            'is_mail_filter': is_mail_filter,
            'case_filter': case_filter,
        }
    )


@role_required('ADMIN', 'PURCHASE')
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


@role_required('ADMIN', 'PURCHASE')
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
            dpr.enquiry_attachment,
            f'enquiry/{PurePath(dpr.enquiry_attachment.name).name}'
            if dpr.enquiry_attachment else ''
        )
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


@role_required('ADMIN', 'PURCHASE', 'SALES')
def customer_po_product_details(request):
    validity_filter = request.GET.get('validity')
    case_filter = request.GET.get('case', '').strip()
    today = timezone.localdate()
    within_7_days = today + timedelta(days=5)

    products = CustomerProduct.objects.select_related(
        'dpr',
        'dpr__customer'
    ).prefetch_related('supplierproduct_set', 'invoices').annotate(
        status_rank=Case(
            When(status='delivered', then=Value(2)),
            When(status='partially_delivered', then=Value(1)),
            default=Value(0),
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

    products = list(products.order_by('status_rank', 'dpr__po_validity', '-id'))

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

        sps = list(product.supplierproduct_set.all())
        if not sps:
            product.material_status_code = 'pending'
            product.material_status_label = 'Not Delivered (Pending)'
        else:
            if all(sp.status == 'delivered' for sp in sps):
                product.material_status_code = 'delivered'
                product.material_status_label = 'Delivered'
            elif any(sp.status == 'partially_delivered' or sp.quantity_received > 0 for sp in sps):
                product.material_status_code = 'partially_delivered'
                product.material_status_label = 'Partially Delivered'
            else:
                product.material_status_code = 'pending'
                product.material_status_label = 'Not Delivered (Pending)'

        inv_no_val = (product.invoice_dc_number or '').strip()
        match = re.search(r'(\d+)', inv_no_val) if inv_no_val else None
        if match:
            product.generated_invoice_number = f"MES-F{int(match.group(1)):04d}"
        elif inv_no_val:
            product.generated_invoice_number = inv_no_val
        else:
            product.generated_invoice_number = f"MES-F{product.id:04d}"

        product.generated_invoices = list(product.invoices.all().order_by('id'))

    # Sort Red (Pending/Expired/Not Delivered) at TOP (0), Yellow (Partial/Due Soon) in MIDDLE (1), Green (Delivered/Closed) at BOTTOM (2)
    def _color_rank(p):
        if p.status == 'delivered' or (p.quantity_delivered and p.quantity_delivered >= p.quantity_ordered):
            return 2
        if p.row_class == 'table-success':
            return 2
        if p.status == 'partially_delivered' or p.row_class in ('table-warning', 'table-info'):
            return 1
        return 0

    import datetime
    products.sort(key=lambda p: (_color_rank(p), p.dpr.po_validity or datetime.date.max, -p.id))

    total_products = len(products)
    delivered_count = sum(1 for p in products if p.status == 'delivered')
    not_delivered_count = total_products - delivered_count

    total_ordered_qty = sum(p.quantity_ordered or 0 for p in products)
    total_delivered_qty = sum(p.quantity_delivered or 0 for p in products)
    total_pending_qty = max(total_ordered_qty - total_delivered_qty, 0)
    delivery_pct = _pct(delivered_count, total_products)

    return render(
        request,
        'customer_po_product_details.html',
        {
            'products': products,
            'total_products': total_products,
            'delivered_count': delivered_count,
            'not_delivered_count': not_delivered_count,
            'total_ordered_qty': total_ordered_qty,
            'total_delivered_qty': total_delivered_qty,
            'total_pending_qty': total_pending_qty,
            'delivery_pct': delivery_pct,
            'case_filter': case_filter,
        }
    )


@role_required('ADMIN', 'PURCHASE')
def customer_product_status_update(request, product_id):
    if request.method != 'POST':
        raise Http404
    try:
        customer_product = CustomerProduct.objects.select_related('dpr').get(pk=product_id)
    except CustomerProduct.DoesNotExist:
        raise Http404

    status = request.POST.get('status', '').strip() or None
    if status not in ('delivered', 'invoice_pending', 'partially_delivered', 'cancelled', None):
        return JsonResponse({'status': 'error', 'message': 'Invalid status'}, status=400)

    customer_product.status = status
    if status in ('delivered', 'invoice_pending'):
        delivery_detail_type = request.POST.get('delivery_detail_type', '').strip()
        invoice_dc_number = request.POST.get('invoice_dc_number', '').strip()
        invoice_dc_attachment = request.FILES.get('invoice_dc_attachment')
        if delivery_detail_type not in ('invoice', 'dc'):
            return JsonResponse({'status': 'error', 'message': 'Delivery Detail is required'}, status=400)
        if not invoice_dc_number:
            label = 'Invoice number' if delivery_detail_type == 'invoice' else 'DC Number'
            return JsonResponse({'status': 'error', 'message': f'{label} is required'}, status=400)
        if not invoice_dc_attachment and not customer_product.invoice_dc_attachment:
            return JsonResponse({'status': 'error', 'message': 'Invoice/DC attachment is required'}, status=400)
        customer_product.quantity_delivered = customer_product.quantity_ordered
        customer_product.delivery_detail_type = delivery_detail_type
        customer_product.status = 'delivered' if delivery_detail_type == 'invoice' else 'invoice_pending'
        customer_product.invoice_dc_number = invoice_dc_number
        if invoice_dc_attachment:
            customer_product.invoice_dc_attachment = invoice_dc_attachment
    elif status == 'cancelled':
        customer_product.quantity_delivered = 0
        customer_product.delivery_detail_type = None
        customer_product.invoice_dc_number = None
        customer_product.invoice_dc_attachment = None
    elif status == 'partially_delivered':
        delivered_qty_raw = request.POST.get('quantity_delivered', '').strip()
        delivery_detail_type = request.POST.get('delivery_detail_type', '').strip()
        invoice_dc_number = request.POST.get('invoice_dc_number', '').strip()
        invoice_dc_attachment = request.FILES.get('invoice_dc_attachment')
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
        if delivery_detail_type not in ('invoice', 'dc'):
            return JsonResponse({'status': 'error', 'message': 'Delivery Detail is required'}, status=400)
        if not invoice_dc_number:
            label = 'Invoice number' if delivery_detail_type == 'invoice' else 'DC Number'
            return JsonResponse({'status': 'error', 'message': f'{label} is required'}, status=400)
        if not invoice_dc_attachment and not customer_product.invoice_dc_attachment:
            return JsonResponse({'status': 'error', 'message': 'Invoice/DC attachment is required'}, status=400)

        customer_product.quantity_delivered = delivered_qty
        customer_product.delivery_detail_type = delivery_detail_type
        customer_product.invoice_dc_number = invoice_dc_number
        if invoice_dc_attachment:
            customer_product.invoice_dc_attachment = invoice_dc_attachment
    else:
        customer_product.quantity_delivered = 0
        customer_product.delivery_detail_type = None
        customer_product.invoice_dc_number = None
        customer_product.invoice_dc_attachment = None

    customer_product.save(update_fields=[
        'status',
        'quantity_delivered',
        'delivery_detail_type',
        'invoice_dc_number',
        'invoice_dc_attachment'
    ])
    _sync_dpr_customer_qty_delivered(customer_product.dpr)
    return JsonResponse({
        'status': 'ok',
        'product_status': customer_product.status or '',
        'invoice_dc_number': customer_product.invoice_dc_number or '',
        'delivery_detail_type': customer_product.delivery_detail_type or '',
        'invoice_dc_attachment_url': customer_product.invoice_dc_attachment.url if customer_product.invoice_dc_attachment else ''
    })


@role_required('ADMIN', 'PURCHASE')
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
            'quantity_received': row.quantity_received,
            'expected_date': row.expected_date.strftime('%Y-%m-%d') if row.expected_date else '-',
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


@role_required('ADMIN', 'PURCHASE')
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
        supplier_product.quantity_ok = max(
            supplier_product.quantity_received - supplier_product.quantity_not_ok,
            0
        )

    return render(
        request,
        'supplier_po_product_details.html',
        {'supplier_products': supplier_products}
    )


@role_required('ADMIN', 'PURCHASE')
def material_status(request):
    case_filter = request.GET.get('case', '').strip()

    supplier_products = SupplierProduct.objects.select_related(
        'customer_product',
        'customer_product__dpr',
        'customer_product__dpr__customer',
        'supplier'
    ).annotate(
        status_rank=Case(
            When(status='delivered', then=Value(1)),
            default=Value(0),
            output_field=IntegerField()
        )
    )

    total_products = supplier_products.count()
    delivered_count = supplier_products.filter(status='delivered').count()
    not_delivered_count = total_products - delivered_count

    partially_delivered_count = supplier_products.filter(status='partially_delivered').count()
    pending_count = supplier_products.filter(status__isnull=True).count()
    cancelled_count = supplier_products.filter(status='cancelled').count()

    total_ordered_qty = sum(sp.quantity for sp in supplier_products)
    total_received_qty = sum(sp.quantity_received for sp in supplier_products)
    total_pending_qty = max(total_ordered_qty - total_received_qty, 0)

    delivery_pct = _pct(delivered_count, total_products)

    if case_filter == 'delivered':
        supplier_products = supplier_products.filter(status='delivered')
    elif case_filter == 'not_delivered':
        supplier_products = supplier_products.exclude(status='delivered')
    elif case_filter == 'partially_delivered':
        supplier_products = supplier_products.filter(status='partially_delivered')
    elif case_filter == 'pending':
        supplier_products = supplier_products.filter(status__isnull=True)
    elif case_filter == 'cancelled':
        supplier_products = supplier_products.filter(status='cancelled')

    supplier_products = list(supplier_products.order_by('status_rank', '-id'))

    for sp in supplier_products:
        sp.remaining_qty = max(sp.quantity - sp.quantity_received, 0)
        sp.is_delivered = (sp.status == 'delivered')

    return render(
        request,
        'material_status.html',
        {
            'supplier_products': supplier_products,
            'total_products': total_products,
            'delivered_count': delivered_count,
            'not_delivered_count': not_delivered_count,
            'partially_delivered_count': partially_delivered_count,
            'pending_count': pending_count,
            'cancelled_count': cancelled_count,
            'total_ordered_qty': total_ordered_qty,
            'total_received_qty': total_received_qty,
            'total_pending_qty': total_pending_qty,
            'delivery_pct': delivery_pct,
            'case_filter': case_filter,
        }
    )


@role_required('ADMIN', 'ACCOUNTS')
def accounts_details(request):
    case_filter = request.GET.get('case', '').strip()
    today = timezone.localdate()

    products = CustomerProduct.objects.select_related(
        'dpr',
        'dpr__customer'
    )

    items = []
    total_invoice_value = Decimal('0.00')
    total_received_amount = Decimal('0.00')
    total_outstanding_amount = Decimal('0.00')
    overdue_amount = Decimal('0.00')

    received_count = 0
    partially_received_count = 0
    not_received_count = 0
    due_soon_count = 0
    overdue_count = 0

    for product in products:
        inv_date = product.invoice_date or product.dpr.po_date or product.dpr.created_at.date()
        inv_no = (product.invoice_dc_number or '').strip()
        if not inv_no:
            inv_no = f"MES-F{product.id:04d}"

        cust = product.dpr.customer
        terms_str = (cust.payment_terms or '').lower() if cust else ''
        days = 30
        match = re.search(r'(\d+)', terms_str)
        if match:
            try:
                days = int(match.group(1))
            except ValueError:
                pass
        
        terms_date = inv_date + timedelta(days=days)

        po_val = product.value or Decimal('0.00')
        rec_amt = product.received_amount or Decimal('0.00')
        rem_amt = max(po_val - rec_amt, Decimal('0.00'))

        is_paid = (product.payment_status == 'amount_received' or rec_amt >= po_val)
        is_due_soon = not is_paid and (today <= terms_date <= today + timedelta(days=7))
        is_overdue = not is_paid and (today > terms_date)

        if is_paid:
            color_state = 'green'
            received_count += 1
        elif is_due_soon:
            color_state = 'orange'
            due_soon_count += 1
            if product.payment_status == 'partially_received':
                partially_received_count += 1
            else:
                not_received_count += 1
        elif is_overdue:
            color_state = 'red'
            overdue_count += 1
            overdue_amount += rem_amt
            if product.payment_status == 'partially_received':
                partially_received_count += 1
            else:
                not_received_count += 1
        else:
            color_state = 'orange' if (terms_date - today).days <= 7 else 'red' if today > terms_date else 'normal'
            if product.payment_status == 'partially_received':
                partially_received_count += 1
            else:
                not_received_count += 1

        total_invoice_value += po_val
        total_received_amount += rec_amt
        total_outstanding_amount += rem_amt

        item = {
            'product': product,
            'dpr': product.dpr,
            'customer_id': cust.id if cust else '',
            'customer_name': cust.customer_name if cust else '-',
            'customer_email': cust.email if cust and cust.email else 'ajayasok008@gmail.com',
            'customer_po_number': (product.dpr.po_number or '').strip() if product.dpr else '',
            'product_name': product.product_name or product.product_type or '-',
            'invoice_number': inv_no,
            'invoice_date': inv_date,
            'invoice_date_formatted': inv_date.strftime('%d-%m-%Y') if inv_date else '-',
            'terms_date': terms_date,
            'po_value': po_val,
            'received_amount': rec_amt,
            'remaining_amount': rem_amt,
            'payment_status': product.payment_status or 'not_received',
            'color_state': color_state,
            'is_paid': is_paid,
            'is_due_soon': is_due_soon,
            'is_overdue': is_overdue,
            'expected_payment_date': product.expected_payment_date,
            'payment_received_date': product.payment_received_date,
            'follow_up_remarks': product.follow_up_remarks or '',
            'payment_notes': product.payment_notes or '',
        }
        items.append(item)

    items.sort(key=lambda x: x['invoice_date'], reverse=True)

    total_products = len(products)
    delivery_pct = _pct(received_count, total_products)

    return render(
        request,
        'accounts_details.html',
        {
            'items': items,
            'total_products': total_products,
            'total_invoice_value': total_invoice_value,
            'total_received_amount': total_received_amount,
            'total_outstanding_amount': total_outstanding_amount,
            'overdue_amount': overdue_amount,
            'received_count': received_count,
            'partially_received_count': partially_received_count,
            'not_received_count': not_received_count,
            'due_soon_count': due_soon_count,
            'overdue_count': overdue_count,
            'delivery_pct': delivery_pct,
            'case_filter': case_filter,
        }
    )


@role_required('ADMIN', 'ACCOUNTS')
def customer_product_followup_update(request, product_id):
    if request.method != 'POST':
        raise Http404
    try:
        product = CustomerProduct.objects.get(pk=product_id)
    except CustomerProduct.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Product not found'}, status=404)

    exp_date_raw = request.POST.get('expected_payment_date', '').strip()
    remarks = request.POST.get('follow_up_remarks', '').strip()

    if exp_date_raw:
        try:
            from datetime import datetime
            product.expected_payment_date = datetime.strptime(exp_date_raw, '%Y-%m-%d').date()
        except ValueError:
            product.expected_payment_date = None
    else:
        product.expected_payment_date = None

    product.follow_up_remarks = remarks
    product.save(update_fields=['expected_payment_date', 'follow_up_remarks'])

    return JsonResponse({'status': 'ok'})


@role_required('ADMIN', 'ACCOUNTS')
def customer_product_payment_update(request, product_id):
    if request.method != 'POST':
        raise Http404
    try:
        product = CustomerProduct.objects.select_related('dpr').get(pk=product_id)
    except CustomerProduct.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Product not found'}, status=404)

    payment_status = request.POST.get('payment_status', '').strip()
    if payment_status not in ('amount_received', 'partially_received', 'not_received'):
        return JsonResponse({'status': 'error', 'message': 'Invalid payment status'}, status=400)

    today = timezone.localdate()

    if payment_status in ('amount_received', 'partially_received'):
        po_val = product.value or Decimal('0.00')
        amount_raw = request.POST.get('received_amount', '').strip()
        try:
            amt = Decimal(amount_raw or '0')
        except InvalidOperation:
            amt = po_val

        if payment_status == 'amount_received':
            product.payment_status = 'amount_received'
            product.received_amount = amt if amt > 0 else po_val
        else:
            if amt >= po_val:
                product.payment_status = 'amount_received'
                product.received_amount = po_val
            elif amt > 0:
                product.payment_status = 'partially_received'
                product.received_amount = amt
            else:
                return JsonResponse({'status': 'error', 'message': 'Received amount must be greater than 0'}, status=400)

        rec_date_raw = request.POST.get('payment_received_date', '').strip()
        if rec_date_raw:
            try:
                from datetime import datetime
                product.payment_received_date = datetime.strptime(rec_date_raw, '%Y-%m-%d').date()
            except ValueError:
                product.payment_received_date = today
        else:
            product.payment_received_date = today

        notes = request.POST.get('payment_notes', '').strip()
        if 'payment_notes' in request.POST:
            product.payment_notes = notes

    elif payment_status == 'not_received':
        product.payment_status = 'not_received'
        product.received_amount = Decimal('0.00')
        product.payment_received_date = None
        product.payment_notes = None

    if 'expected_payment_date' in request.POST:
        exp_date_raw = request.POST.get('expected_payment_date', '').strip()
        if exp_date_raw:
            try:
                from datetime import datetime
                product.expected_payment_date = datetime.strptime(exp_date_raw, '%Y-%m-%d').date()
            except ValueError:
                pass
        else:
            product.expected_payment_date = None

    if 'follow_up_remarks' in request.POST:
        product.follow_up_remarks = request.POST.get('follow_up_remarks', '').strip()

    product.save(update_fields=[
        'payment_status',
        'received_amount',
        'payment_received_date',
        'payment_notes',
        'expected_payment_date',
        'follow_up_remarks'
    ])

    return JsonResponse({
        'status': 'ok',
        'payment_status': product.payment_status,
        'received_amount': str(product.received_amount),
        'payment_received_date': product.payment_received_date.strftime('%Y-%m-%d') if product.payment_received_date else '',
    })


@role_required('ADMIN', 'ACCOUNTS')
def supplier_accounts_details(request):
    case_filter = request.GET.get('case', '').strip()
    today = timezone.localdate()

    products = SupplierProduct.objects.filter(
        Q(quantity_received__gt=0) |
        Q(quantity_not_ok__gt=0) |
        Q(status__in=['delivered', 'partially_delivered']) |
        (Q(supplier_invoice_number__isnull=False) & ~Q(supplier_invoice_number='')) |
        Q(supplier_bill_amount__gt=0) |
        (Q(bill_attachment__isnull=False) & ~Q(bill_attachment=''))
    ).select_related(
        'supplier',
        'customer_product',
        'customer_product__dpr'
    ).distinct()

    items = []
    total_invoice_value = Decimal('0.00')
    total_received_amount = Decimal('0.00')
    total_outstanding_amount = Decimal('0.00')
    overdue_amount = Decimal('0.00')

    received_count = 0
    partially_received_count = 0
    not_received_count = 0
    due_soon_count = 0
    overdue_count = 0

    for product in products:
        dpr_created = product.customer_product.dpr.created_at.date() if (product.customer_product and product.customer_product.dpr and product.customer_product.dpr.created_at) else today
        inv_date = product.invoice_date or product.po_date or dpr_created
        inv_no = (product.po_number or product.invoice_dc_number or '').strip()
        if not inv_no:
            inv_no = f"SUP-PO{product.id:04d}"

        supp = product.supplier
        terms_str = (supp.payment_terms or '').strip().lower() if supp else ''
        days = 30
        match = re.search(r'(\d+)', terms_str)
        if match:
            try:
                val = int(match.group(1))
                if 'day' in terms_str:
                    days = val
                elif 'month' in terms_str:
                    days = val * 30
                else:
                    # Supplier payment terms in Master is entered in Weeks (e.g. 2, 4 weeks)
                    days = val * 7
            except ValueError:
                pass

        terms_date = inv_date + timedelta(days=days)

        po_val = product.supplier_bill_amount if (product.supplier_bill_amount is not None and product.supplier_bill_amount > Decimal('0.00')) else (product.po_value or Decimal('0.00'))
        rec_amt = product.received_amount or Decimal('0.00')
        rem_amt = max(po_val - rec_amt, Decimal('0.00'))

        is_paid = (product.payment_status == 'amount_received' or rec_amt >= po_val)
        is_due_soon = not is_paid and (today <= terms_date <= today + timedelta(days=7))
        is_overdue = not is_paid and (today > terms_date)

        if is_paid:
            color_state = 'green'
            received_count += 1
        elif is_due_soon:
            color_state = 'orange'
            due_soon_count += 1
            if product.payment_status == 'partially_received':
                partially_received_count += 1
            else:
                not_received_count += 1
        elif is_overdue:
            color_state = 'red'
            overdue_count += 1
            overdue_amount += rem_amt
            if product.payment_status == 'partially_received':
                partially_received_count += 1
            else:
                not_received_count += 1
        else:
            color_state = 'orange' if (terms_date - today).days <= 7 else 'red' if today > terms_date else 'normal'
            if product.payment_status == 'partially_received':
                partially_received_count += 1
            else:
                not_received_count += 1

        total_invoice_value += po_val
        total_received_amount += rec_amt
        total_outstanding_amount += rem_amt

        item = {
            'product': product,
            'supplier_id': supp.id if supp else '',
            'supplier_name': supp.supplier_name if supp else '-',
            'supplier_email': supp.email if supp and supp.email else 'ajayasok008@gmail.com',
            'supplier_po_number': (product.po_number or '').strip(),
            'product_name': product.customer_product.product_name if product.customer_product else (product.customer_product.product_type if product.customer_product else '-'),
            'invoice_number': inv_no,
            'invoice_date': inv_date,
            'invoice_date_formatted': inv_date.strftime('%d-%m-%Y') if inv_date else '-',
            'terms_date': terms_date,
            'po_value': po_val,
            'received_amount': rec_amt,
            'remaining_amount': rem_amt,
            'payment_status': product.payment_status or 'not_received',
            'color_state': color_state,
            'is_paid': is_paid,
            'is_due_soon': is_due_soon,
            'is_overdue': is_overdue,
            'expected_payment_date': product.expected_payment_date,
            'payment_received_date': product.payment_received_date,
            'follow_up_remarks': product.follow_up_remarks or '',
            'payment_notes': product.payment_notes or '',
            'supplier_invoice_number': product.supplier_invoice_number or '',
            'bill_attachment_url': product.bill_attachment.url if product.bill_attachment else '',
            'bill_attachment_name': re.split(r'[/\\]', product.bill_attachment.name)[-1] if product.bill_attachment else '',
        }
        items.append(item)

    items.sort(key=lambda x: x['invoice_date'] if x['invoice_date'] else today, reverse=True)

    total_products = len(products)
    delivery_pct = _pct(received_count, total_products)

    return render(
        request,
        'supplier_accounts_details.html',
        {
            'items': items,
            'total_products': total_products,
            'total_invoice_value': total_invoice_value,
            'total_received_amount': total_received_amount,
            'total_outstanding_amount': total_outstanding_amount,
            'overdue_amount': overdue_amount,
            'received_count': received_count,
            'partially_received_count': partially_received_count,
            'not_received_count': not_received_count,
            'due_soon_count': due_soon_count,
            'overdue_count': overdue_count,
            'delivery_pct': delivery_pct,
            'case_filter': case_filter,
        }
    )


@role_required('ADMIN', 'ACCOUNTS')
def supplier_product_followup_update(request, supplier_product_id):
    if request.method != 'POST':
        raise Http404
    try:
        product = SupplierProduct.objects.get(pk=supplier_product_id)
    except SupplierProduct.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Supplier product not found'}, status=404)

    exp_date_raw = request.POST.get('expected_payment_date', '').strip()
    remarks = request.POST.get('follow_up_remarks', '').strip()

    if exp_date_raw:
        try:
            from datetime import datetime
            product.expected_payment_date = datetime.strptime(exp_date_raw, '%Y-%m-%d').date()
        except ValueError:
            product.expected_payment_date = None
    else:
        product.expected_payment_date = None

    product.follow_up_remarks = remarks
    product.save(update_fields=['expected_payment_date', 'follow_up_remarks'])

    return JsonResponse({'status': 'ok'})


@role_required('ADMIN', 'ACCOUNTS')
def supplier_product_payment_update(request, supplier_product_id):
    if request.method != 'POST':
        raise Http404
    try:
        product = SupplierProduct.objects.select_related('supplier').get(pk=supplier_product_id)
    except SupplierProduct.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Supplier product not found'}, status=404)

    payment_status = request.POST.get('payment_status', '').strip()
    if payment_status not in ('amount_received', 'partially_received', 'not_received'):
        return JsonResponse({'status': 'error', 'message': 'Invalid payment status'}, status=400)

    today = timezone.localdate()

    if payment_status in ('amount_received', 'partially_received'):
        po_val = product.po_value or Decimal('0.00')
        amount_raw = request.POST.get('received_amount', '').strip()
        try:
            amt = Decimal(amount_raw or '0')
        except InvalidOperation:
            amt = po_val

        if payment_status == 'amount_received':
            product.payment_status = 'amount_received'
            product.received_amount = amt if amt > 0 else po_val
        else:
            if amt >= po_val:
                product.payment_status = 'amount_received'
                product.received_amount = po_val
            elif amt > 0:
                product.payment_status = 'partially_received'
                product.received_amount = amt
            else:
                return JsonResponse({'status': 'error', 'message': 'Received amount must be greater than 0'}, status=400)

        rec_date_raw = request.POST.get('payment_received_date', '').strip()
        if rec_date_raw:
            try:
                from datetime import datetime
                product.payment_received_date = datetime.strptime(rec_date_raw, '%Y-%m-%d').date()
            except ValueError:
                product.payment_received_date = today
        else:
            product.payment_received_date = today

        notes = request.POST.get('payment_notes', '').strip()
        if 'payment_notes' in request.POST:
            product.payment_notes = notes

    elif payment_status == 'not_received':
        product.payment_status = 'not_received'
        product.received_amount = Decimal('0.00')
        product.payment_received_date = None
        product.payment_notes = None

    if 'expected_payment_date' in request.POST:
        exp_date_raw = request.POST.get('expected_payment_date', '').strip()
        if exp_date_raw:
            try:
                from datetime import datetime
                product.expected_payment_date = datetime.strptime(exp_date_raw, '%Y-%m-%d').date()
            except ValueError:
                pass
        else:
            product.expected_payment_date = None

    if 'follow_up_remarks' in request.POST:
        product.follow_up_remarks = request.POST.get('follow_up_remarks', '').strip()

    product.save(update_fields=[
        'payment_status',
        'received_amount',
        'payment_received_date',
        'payment_notes',
        'expected_payment_date',
        'follow_up_remarks'
    ])

    return JsonResponse({
        'status': 'ok',
        'payment_status': product.payment_status,
        'received_amount': str(product.received_amount),
        'payment_received_date': product.payment_received_date.strftime('%Y-%m-%d') if product.payment_received_date else '',
    })


@role_required('ADMIN', 'PURCHASE')
def supplier_product_status_update(request, supplier_product_id):
    if request.method != 'POST':
        raise Http404
    try:
        supplier_product = SupplierProduct.objects.select_related(
            'customer_product__dpr'
        ).get(pk=supplier_product_id)
    except SupplierProduct.DoesNotExist:
        raise Http404

    ok_raw = request.POST.get('ok_quantity')
    not_ok_raw = request.POST.get('not_ok_quantity')
    received_raw = request.POST.get('quantity_received')

    if ok_raw is not None or not_ok_raw is not None:
        try:
            received_quantity = int(received_raw or 0)
            ok_quantity = int(ok_raw or 0)
            not_ok_quantity = int(not_ok_raw or 0)
        except ValueError:
            return JsonResponse({
                'status': 'error',
                'message': 'Received, OK and NOT OK quantities must be numbers.'
            }, status=400)

        if received_quantity < 0 or ok_quantity < 0 or not_ok_quantity < 0:
            return JsonResponse({
                'status': 'error',
                'message': 'Received, OK and NOT OK quantities cannot be negative.'
            }, status=400)

        if received_quantity > supplier_product.quantity:
            return JsonResponse({
                'status': 'error',
                'message': (
                    'Received quantity cannot be greater than '
                    f'ordered quantity ({supplier_product.quantity}).'
                )
            }, status=400)

        if ok_quantity + not_ok_quantity > received_quantity:
            return JsonResponse({
                'status': 'error',
                'message': (
                    'OK quantity plus NOT OK quantity cannot exceed '
                    f'received quantity ({received_quantity}).'
                )
            }, status=400)

        not_ok_reason = request.POST.get('not_ok_reason', '').strip()
        if not_ok_quantity > 0 and not not_ok_reason:
            return JsonResponse({
                'status': 'error',
                'message': 'Reason is required when NOT OK quantity is greater than 0.'
            }, status=400)

        if (
            received_quantity == supplier_product.quantity
            and ok_quantity == supplier_product.quantity
            and not_ok_quantity == 0
        ):
            supplier_product.status = 'delivered'
        elif received_quantity > 0:
            supplier_product.status = 'partially_delivered'
        else:
            supplier_product.status = None

        supplier_invoice_number = request.POST.get('supplier_invoice_number', '').strip() or None
        supplier_bill_amount_raw = request.POST.get('supplier_bill_amount', '').strip()
        bill_attachment = request.FILES.get('bill_attachment')

        if 'supplier_invoice_number' in request.POST:
            if not supplier_invoice_number:
                return JsonResponse({'status': 'error', 'message': 'Supplier Invoice Number is required.'}, status=400)

            try:
                bill_amt = Decimal(supplier_bill_amount_raw or '0')
            except (InvalidOperation, ValueError):
                bill_amt = Decimal('0.00')

            if bill_amt <= Decimal('0.00'):
                return JsonResponse({'status': 'error', 'message': 'Supplier Bill Amount is required and must be greater than 0.'}, status=400)

            if not bill_attachment and not supplier_product.bill_attachment:
                return JsonResponse({'status': 'error', 'message': 'Bill Attachment is required.'}, status=400)

        supplier_product.quantity_received = received_quantity
        supplier_product.quantity_not_ok = not_ok_quantity
        supplier_product.not_ok_reason = not_ok_reason if not_ok_quantity > 0 else None

        update_fields = [
            'status',
            'quantity_received',
            'quantity_not_ok',
            'not_ok_reason'
        ]

        if 'supplier_invoice_number' in request.POST:
            supplier_product.supplier_invoice_number = supplier_invoice_number
            supplier_product.invoice_dc_number = supplier_invoice_number
            update_fields.extend(['supplier_invoice_number', 'invoice_dc_number'])

        if 'supplier_bill_amount' in request.POST:
            try:
                supplier_product.supplier_bill_amount = Decimal(supplier_bill_amount_raw or '0')
            except (InvalidOperation, ValueError):
                supplier_product.supplier_bill_amount = Decimal('0.00')
            update_fields.append('supplier_bill_amount')

        if bill_attachment:
            supplier_product.bill_attachment = bill_attachment
            update_fields.append('bill_attachment')

        if received_quantity > 0 and not supplier_product.invoice_date:
            supplier_product.invoice_date = timezone.localdate()
            update_fields.append('invoice_date')

        supplier_product.save(update_fields=update_fields)
        _sync_dpr_supplier_qty_received(supplier_product.customer_product.dpr)
        return JsonResponse({
            'status': 'ok',
            'inward_status': supplier_product.status or '',
            'quantity_received': supplier_product.quantity_received,
            'quantity_ok': ok_quantity,
            'quantity_not_ok': supplier_product.quantity_not_ok,
            'not_ok_reason': supplier_product.not_ok_reason or '',
            'supplier_invoice_number': supplier_product.supplier_invoice_number or '',
            'supplier_bill_amount': str(supplier_product.supplier_bill_amount or '0.00'),
            'bill_attachment_url': supplier_product.bill_attachment.url if supplier_product.bill_attachment else '',
            'bill_attachment_name': supplier_product.bill_attachment.name.split('/')[-1] if supplier_product.bill_attachment else ''
        })

    status = request.POST.get('status', '').strip() or None
    if status not in ('delivered', 'partially_delivered', 'cancelled', None):
        return JsonResponse({'status': 'error', 'message': 'Invalid status'}, status=400)

    supplier_product.status = status
    supplier_product.quantity_not_ok = 0
    supplier_product.not_ok_reason = None
    if status == 'delivered':
        supplier_product.quantity_received = supplier_product.quantity
    elif status == 'cancelled':
        supplier_product.quantity_received = 0
        supplier_product.quantity_not_ok = supplier_product.quantity
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

    update_fields = [
        'status',
        'quantity_received',
        'quantity_not_ok',
        'not_ok_reason'
    ]
    if supplier_product.quantity_received > 0 and not supplier_product.invoice_date:
        supplier_product.invoice_date = timezone.localdate()
        update_fields.append('invoice_date')

    supplier_product.save(update_fields=update_fields)
    _sync_dpr_supplier_qty_received(supplier_product.customer_product.dpr)
    return JsonResponse({'status': 'ok'})


@role_required('ADMIN', 'PURCHASE')
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


@role_required('ADMIN', 'PURCHASE')
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


@role_required('ADMIN', 'PURCHASE')
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


@role_required('ADMIN', 'PURCHASE')
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
        dpr.enquiry_attachment = (
            request.FILES.get('enquiry_attachment')
            or dpr.enquiry_attachment
        )
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
        quantities = request.POST.getlist('quantity[]')
        rates = request.POST.getlist('mes_rate_per_unit[]')
        _, po_value_error = _validate_po_value_matches_total(
            request.POST.get('po_value'),
            product_names,
            quantities,
            rates
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
        rates = request.POST.getlist('mes_rate_per_unit[]')
        mes_rates = request.POST.getlist('mes_rate_per_unit[]')
        remarks_list = request.POST.getlist('remarks[]')

        for i, product_name in enumerate(product_names):
            if product_name.strip() == '':
                continue
            try:
                quantity, rate_per_unit, value = _calculate_product_line_value(
                    quantities[i] if i < len(quantities) else '0',
                    rates[i] if i < len(rates) else '0',
                    i + 1
                )
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect('customer_order_edit', dpr_id=dpr.id)

            mes_rate_val = mes_rates[i] if i < len(mes_rates) else '0'
            try:
                mes_rate_per_unit = Decimal(mes_rate_val or '0')
                mes_value = (Decimal(quantity) * mes_rate_per_unit).quantize(Decimal('0.01'))
            except Exception:
                mes_rate_per_unit = Decimal('0.00')
                mes_value = Decimal('0.00')

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
                rate_per_unit=rate_per_unit,
                mes_rate_per_unit=mes_rate_per_unit,
                value=value,
                mes_value=mes_value,
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


@role_required('ADMIN', 'PURCHASE')
def dpr_supplier(request, dpr_id):
    try:
        dpr = DPR.objects.get(pk=dpr_id)
    except DPR.DoesNotExist:
        raise Http404

    from collections import defaultdict
    from datetime import timedelta
    from products.models import SupplierProduct

    products = CustomerProduct.objects.filter(dpr=dpr)
    suppliers = Supplier.objects.all()
    total_customer_quantity = products.aggregate(
        total=Coalesce(Sum('quantity_ordered'), 0)
    )['total']

    supplier_orders = SupplierProduct.objects.filter(
        customer_product__dpr=dpr
    ).select_related('customer_product', 'supplier')

    customer_products = list(products)
    product_lookup = {}
    for product in customer_products:
        name_key = (product.product_name or '').strip().lower()
        type_key = (product.product_type or '').strip().lower()
        product_lookup[(name_key, type_key)] = product.id
        if name_key not in product_lookup:
            product_lookup[name_key] = product.id

    rfq_rate_map = {}
    product_default_rate_map = {}
    rfq_supplier_prices = RFQSupplierPrice.objects.filter(
        product__rfq__customer=dpr.customer
    ).select_related('product', 'supplier').order_by('product__rfq__created_at')
    for supplier_price in rfq_supplier_prices:
        name_key = (supplier_price.product.product_name or '').strip().lower()
        type_key = (supplier_price.product.product_type or '').strip().lower()
        customer_product_id = product_lookup.get((name_key, type_key))
        if not customer_product_id:
            customer_product_id = product_lookup.get(name_key)
        if not customer_product_id:
            continue
        rfq_rate_map.setdefault(str(customer_product_id), {})[
            str(supplier_price.supplier_id)
        ] = str(supplier_price.price)
        # Use the first supplier price as the default rate for the product
        if str(customer_product_id) not in product_default_rate_map:
            product_default_rate_map[str(customer_product_id)] = str(supplier_price.price)

    # Also check known-price RFQ products (rate_per_unit > 0) as default rates
    rfq_products_known = RFQProduct.objects.filter(
        rfq__customer=dpr.customer,
        price_known=True,
        rate_per_unit__gt=0
    ).order_by('rfq__created_at')
    for rfq_product in rfq_products_known:
        name_key = (rfq_product.product_name or '').strip().lower()
        type_key = (rfq_product.product_type or '').strip().lower()
        customer_product_id = product_lookup.get((name_key, type_key))
        if not customer_product_id:
            customer_product_id = product_lookup.get(name_key)
        if not customer_product_id:
            continue
        # Known-price products override the supplier-price default
        product_default_rate_map[str(customer_product_id)] = str(rfq_product.rate_per_unit)

    if not supplier_orders.exists() and products.exists() and request.method == 'GET':
        from datetime import timedelta
        po_date = timezone.localdate()
        target_customer_date = dpr.po_validity or dpr.po_date
        validity_date = (target_customer_date - timedelta(days=7)) if target_customer_date else (po_date + timedelta(days=7))

        for p in products:
            p_id_str = str(p.id)
            rates_for_prod = rfq_rate_map.get(p_id_str, {})
            assigned_supplier = None
            assigned_rate = Decimal('0.00')
            if rates_for_prod:
                for s_id_str, s_price in rates_for_prod.items():
                    assigned_supplier = suppliers.filter(id=int(s_id_str)).first()
                    if assigned_supplier:
                        assigned_rate = Decimal(str(s_price))
                        break
            if not assigned_supplier:
                assigned_supplier = suppliers.first()
                def_rate = product_default_rate_map.get(p_id_str)
                assigned_rate = Decimal(str(def_rate)) if def_rate else (p.rate_per_unit or Decimal('0.00'))

            po_val = (p.quantity_ordered or 0) * assigned_rate
            po_num = f'SPO-{po_date:%Y%m%d}-{dpr.id:04d}-{assigned_supplier.id:04d}' if assigned_supplier else ''

            SupplierProduct.objects.create(
                customer_product=p,
                supplier=assigned_supplier,
                rate_per_unit=assigned_rate,
                po_value=po_val,
                po_date=po_date,
                po_validity=validity_date,
                quantity=p.quantity_ordered or 0,
                po_number=po_num,
            )

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

    if request.method == 'GET' and request.GET.get('po_generated') == '1':
        from django.contrib import messages
        messages.success(request, 'Supplier orders updated and PO generated successfully.')

    if request.method == 'POST':
        product_ids = request.POST.getlist('product[]')
        supplier_ids = request.POST.getlist('supplier[]')
        rates = request.POST.getlist('rate_per_unit[]')
        quantities = request.POST.getlist('quantity[]')
        po_validities = request.POST.getlist('po_validity[]')
        supplier_product_ids = request.POST.getlist('supplier_product_id[]')
        quantity_by_product = {}
        total_entered_supplier_qty = 0

        existing_attachments = {
            str(sp.id): sp.po_attachment
            for sp in supplier_orders
        }
        existing_po_numbers = {
            str(sp.id): sp.po_number
            for sp in supplier_orders
        }
        existing_email_states = {
            str(sp.id): (
                sp.po_email_sent,
                sp.customer_product_id,
                sp.supplier_id,
            )
            for sp in supplier_orders
        }
        existing_pdf_generated = {
            str(sp.id): sp.po_pdf_generated
            for sp in supplier_orders
        }

        for i in range(len(product_ids)):
            if not product_ids[i] or not supplier_ids[i]:
                continue

            required_values = [
                product_ids[i],
                supplier_ids[i],
                rates[i] if i < len(rates) else '',
                quantities[i] if i < len(quantities) else '',
                po_validities[i] if i < len(po_validities) else '',
            ]
            existing_id = (
                supplier_product_ids[i]
                if i < len(supplier_product_ids)
                else ''
            )

            if any(v in ('', None) for v in required_values):
                messages.error(request, f"All fields are mandatory in row {i + 1}.")
                return redirect('dpr_supplier', dpr_id=dpr.id)

            customer_product = CustomerProduct.objects.get(pk=product_ids[i])
            try:
                entered_quantity, _, _ = _calculate_product_line_value(
                    quantities[i] if i < len(quantities) else '0',
                    rates[i] if i < len(rates) else '0',
                    i + 1
                )
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect('dpr_supplier', dpr_id=dpr.id)
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

            if po_validities[i]:
                try:
                    from datetime import datetime
                    sp_val_dt = datetime.strptime(po_validities[i], '%Y-%m-%d').date()
                    today = timezone.localdate()
                    if sp_val_dt < today:
                        messages.error(
                            request,
                            f"Supplier PO validity in row {i + 1} ({sp_val_dt.strftime('%d-%m-%Y')}) cannot be in the past (Today is {today.strftime('%d-%m-%Y')})."
                        )
                        return redirect('dpr_supplier', dpr_id=dpr.id)

                    target_customer_date = dpr.po_validity or dpr.po_date
                    if target_customer_date and sp_val_dt >= target_customer_date:
                        messages.error(
                            request,
                            f"Supplier PO validity in row {i + 1} ({sp_val_dt.strftime('%d-%m-%Y')}) must be earlier than Customer PO date ({target_customer_date.strftime('%d-%m-%Y')})."
                        )
                        return redirect('dpr_supplier', dpr_id=dpr.id)
                except ValueError:
                    pass

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
            quantity, rate, po_value = _calculate_product_line_value(
                quantities[i] if i < len(quantities) else '0',
                rates[i] if i < len(rates) else '0',
                i + 1
            )
            existing_id = (
                supplier_product_ids[i]
                if i < len(supplier_product_ids)
                else ''
            )

            po_attachment = request.FILES.get(f'po_attachment_{i}')
            if not po_attachment and existing_id:
                po_attachment = existing_attachments.get(existing_id)

            previous_email_state = existing_email_states.get(existing_id)
            preserve_email_sent = bool(
                previous_email_state
                and previous_email_state[0]
                and previous_email_state[1] == customer_product.id
                and previous_email_state[2] == supplier.id
            )

            po_date = timezone.localdate()
            po_number = existing_po_numbers.get(existing_id)
            if not po_number or not po_number.startswith('SPO-'):
                po_number = f'SPO-{po_date:%Y%m%d}-{dpr.id:04d}-{supplier.id:04d}'

            SupplierProduct.objects.create(
                customer_product=customer_product,
                supplier=supplier,
                rate_per_unit=rate,
                po_value=po_value,
                po_date=po_date,
                po_validity=po_validities[i] or None,
                quantity=quantity,
                po_number=po_number,
                po_attachment=po_attachment,
                po_email_sent=preserve_email_sent,
                po_pdf_generated=existing_pdf_generated.get(existing_id, False),
            )

        _sync_dpr_supplier_qty_ordered(dpr)

        if request.POST.get('action') == 'generate_po':
            return generate_supplier_po(request, dpr.id)

        messages.success(
            request,
            'Supplier orders updated successfully' if is_edit else 'Supplier orders saved successfully'
        )
        return redirect('dpr_view')

    missing_email_suppliers = []
    has_supplier_emails = True
    for sp in supplier_orders:
        supplier = sp.supplier
        if not supplier.email or not supplier.email.strip():
            if supplier.supplier_name not in missing_email_suppliers:
                missing_email_suppliers.append(supplier.supplier_name)
    if missing_email_suppliers:
        has_supplier_emails = False

    groups = defaultdict(list)
    for sp in supplier_orders:
        groups[sp.supplier].append(sp)

    po_data_list = []
    supplier_group_indices = {}
    for group_index, (supplier, items) in enumerate(groups.items()):
        supplier_group_indices[supplier.id] = group_index
        po_number = items[0].po_number or f'SPO-{timezone.localdate():%Y%m%d}-{dpr.id:04d}-{supplier.id:04d}'
        # po_pdf_generated is True only when the Generate PO & Update button was clicked
        supplier_po_generated = any(sp.po_pdf_generated for sp in items)
        po_data_list.append({
            'supplier_id': supplier.id,
            'po_number': po_number,
            'supplier_name': supplier.supplier_name,
            'supplier_email': supplier.email or '',
            'combined_supplier': True,
            'product_count': len(items),
            'po_generated': supplier_po_generated,
            'default_subject': f"Purchase Order - {po_number} from Metrology Engineering Solutions",
            'default_body': (
                f"Dear Sir/Madam,\n\n"
                f"Greetings from Metrology Engineering Solutions.\n\n"
                f"Please find the attached Purchase Order ({po_number}) for your reference.\n\n"
                f"Kindly acknowledge the receipt of this email and confirm the delivery date.\n\n"
                f"Thank you,\n"
                f"Metrology Engineering Solutions"
            )
        })

    seen_suppliers_for_email = set()
    for sp in supplier_orders:
        if sp.supplier_id not in seen_suppliers_for_email:
            sp.show_supplier_email_action = True
            seen_suppliers_for_email.add(sp.supplier_id)
        else:
            sp.show_supplier_email_action = False
        sp.email_group_index = supplier_group_indices.get(sp.supplier_id, 0)
        sp.email_group_product_count = len(groups.get(sp.supplier, []))

    supplier_ids = {sp.supplier_id for sp in supplier_orders}
    all_same_supplier = bool(supplier_orders) and len(supplier_ids) == 1
    # po_prepared is True only when at least one supplier order had its PO PDF generated
    po_prepared = any(sp.po_pdf_generated for sp in supplier_orders)

    target_customer_date = dpr.po_validity or dpr.po_date
    default_supplier_validity = (target_customer_date - timedelta(days=7)) if target_customer_date else (timezone.localdate() + timedelta(days=7))

    today = timezone.localdate()
    context = {
        'dpr': dpr,
        'products': products,
        'suppliers': suppliers,
        'supplier_orders': supplier_orders,
        'total_customer_quantity': total_customer_quantity,
        'total_supplier_quantity': total_supplier_quantity,
        'is_edit': is_edit,
        'is_fully_allocated': is_edit,
        'rfq_rate_map': rfq_rate_map,
        'product_default_rate_map': product_default_rate_map,
        'has_supplier_emails': has_supplier_emails,
        'missing_email_suppliers': missing_email_suppliers,
        'po_data_list': po_data_list,
        'all_same_supplier': all_same_supplier,
        'po_prepared': po_prepared,
        'customer_po_date': dpr.po_date,
        'customer_po_validity': dpr.po_validity,
        'target_customer_date': target_customer_date,
        'default_supplier_validity': default_supplier_validity,
        'today': today,
        'today_str': today.strftime('%Y-%m-%d'),
    }
    return render(request, 'supplier_order.html', context)


@role_required('ADMIN', 'PURCHASE')
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


@role_required('ADMIN', 'SALES')
def customer_details(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        customer_id = request.POST.get('customer_id')
        customer_name = request.POST.get('customer_name', '').strip()
        region = request.POST.get('region', '').strip()
        email = request.POST.get('email', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        address = request.POST.get('address', '').strip()
        gstin = request.POST.get('gstin', '').strip().upper()
        state_code = request.POST.get('state_code', '').strip().upper()
        payment_terms = request.POST.get('payment_terms', '').strip()


        if action in ('add', 'edit'):
            if not customer_name:
                messages.error(request, 'Customer Name is required.')
                return redirect('customer_details')
            region_error = _validate_customer_region(region)
            if region_error:
                messages.error(request, region_error)
                return redirect('customer_details')
            if not state_code:
                messages.error(request, 'State Code is required.')
                return redirect('customer_details')
            if email:
                try:
                    validate_email(email)
                except ValidationError:
                    messages.error(request, 'Enter a valid customer email address.')
                    return redirect('customer_details')
            phone_error = _validate_master_phone(phone_number)
            if phone_error:
                messages.error(request, phone_error)
                return redirect('customer_details')

        if action == 'add':
            Customer.objects.create(
                customer_name=customer_name,
                region=region,
                email=email or None,
                phone_number=phone_number or None,
                address=address or None,
                gstin=gstin or None,
                state_code=state_code or None,
                payment_terms=payment_terms or None
            )
            messages.success(request, 'Customer added successfully.')
        elif action == 'edit':
            try:
                customer = Customer.objects.get(pk=customer_id)
            except Customer.DoesNotExist:
                raise Http404
            customer.customer_name = customer_name
            customer.region = region
            customer.email = email or None
            customer.phone_number = phone_number or None
            customer.address = address or None
            customer.gstin = gstin or None
            customer.state_code = state_code or None
            customer.payment_terms = payment_terms or None
            customer.save(update_fields=['customer_name', 'region', 'email', 'phone_number', 'address', 'gstin', 'state_code', 'payment_terms'])
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

    search_query = request.GET.get('search', '').strip()
    customers = Customer.objects.order_by('customer_name')
    if search_query:
        customers = customers.filter(customer_name__icontains=search_query)
    return render(request, 'customer_details.html', {'customers': customers, 'search_query': search_query})


@role_required('ADMIN', 'SALES')
def rfq_details(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        rfq_id = request.POST.get('rfq_id')
        mail_date = request.POST.get('mail_date', '').strip()
        customer_id = request.POST.get('customer_id', '').strip()
        enquiry_details = request.POST.get('enquiry_details', '').strip()
        remarks = request.POST.get('remarks', '').strip()
        attachment = request.FILES.get('attachment')
        product_names = request.POST.getlist('product_name[]')
        product_ids = request.POST.getlist('product_id[]')
        product_types = request.POST.getlist('product_type[]')
        price_known_values = request.POST.getlist('price_known[]')
        quantities = request.POST.getlist('quantity[]')
        units = request.POST.getlist('unit[]')
        rates = request.POST.getlist('rate_per_unit[]')
        product_remarks = request.POST.getlist('product_remarks[]')
        supplier_email_to = request.POST.get('supplier_email_to', '').strip().lower()
        supplier_email_cc = request.POST.get('supplier_email_cc', '').strip().lower()
        supplier_email_subject = request.POST.get('supplier_email_subject', '').strip()
        supplier_email_body = request.POST.get('supplier_email_body', '').strip()
        supplier_email_attachment = request.FILES.get('supplier_email_attachment')
        has_unknown_price = any(val == 'no' for val in price_known_values)
        send_supplier_email = (
            request.POST.get('send_supplier_email') == '1'
            or (has_unknown_price and bool(supplier_email_to.strip()))
        )
        product_rows = []

        if send_supplier_email:
            supplier_to_emails = [
                email.strip().lower()
                for email in re.split(r'[;,]', supplier_email_to)
                if email.strip()
            ]
            supplier_cc_emails = [
                email.strip().lower()
                for email in re.split(r'[;,]', supplier_email_cc)
                if email.strip()
            ]
            for email_address in supplier_to_emails + supplier_cc_emails:
                try:
                    validate_email(email_address)
                except ValidationError:
                    messages.error(request, f'Enter a valid supplier email address: {email_address}')
                    return redirect('rfq_details')
        else:
            supplier_to_emails = []
            supplier_cc_emails = []

        if action in ('add', 'edit'):
            if not mail_date:
                messages.error(request, 'Mail Date is required.')
                return redirect('rfq_details')
            if not customer_id:
                messages.error(request, 'Customer is required.')
                return redirect('rfq_details')
            if not enquiry_details:
                messages.error(request, 'Enquiry Details is required.')
                return redirect('rfq_details')

            try:
                customer = Customer.objects.get(pk=customer_id)
            except Customer.DoesNotExist:
                messages.error(request, 'Select a valid customer.')
                return redirect('rfq_details')

            for i, product_name in enumerate(product_names):
                if not product_name.strip():
                    continue
                product_id_val = product_ids[i] if i < len(product_ids) else ''
                product_id = int(product_id_val) if product_id_val and product_id_val.isdigit() else None
                product_type = product_types[i] if i < len(product_types) else ''
                price_known = (price_known_values[i] if i < len(price_known_values) else 'yes') == 'yes'
                selected_supplier_ids = [
                    supplier_id
                    for supplier_id in request.POST.getlist(f'supplier_ids_{i}[]')
                    if supplier_id
                ]
                supplier_price_rows = []
                seen_price_supplier_ids = set()
                price_supplier_ids = request.POST.getlist(f'supplier_price_supplier_{i}[]')
                supplier_price_values = request.POST.getlist(f'supplier_price_{i}[]')
                for price_index, supplier_id in enumerate(price_supplier_ids):
                    supplier_id = (supplier_id or '').strip()
                    price_raw = supplier_price_values[price_index] if price_index < len(supplier_price_values) else ''
                    price_raw = (price_raw or '').strip()
                    if not supplier_id and not price_raw:
                        continue
                    if not supplier_id or not price_raw:
                        messages.error(request, f'Supplier and price are required in price detail row {price_index + 1} for product row {i + 1}.')
                        return redirect('rfq_details')
                    if supplier_id in seen_price_supplier_ids:
                        messages.error(request, f'Duplicate supplier price detail in product row {i + 1}.')
                        return redirect('rfq_details')
                    try:
                        supplier_price = Decimal(price_raw)
                    except (InvalidOperation, TypeError):
                        messages.error(request, f'Enter a valid supplier price in product row {i + 1}.')
                        return redirect('rfq_details')
                    if supplier_price < 0:
                        messages.error(request, f'Supplier price cannot be negative in product row {i + 1}.')
                        return redirect('rfq_details')
                    seen_price_supplier_ids.add(supplier_id)
                    supplier_price_rows.append({
                        'supplier_id': supplier_id,
                        'price': supplier_price,
                    })
                selected_supplier_ids = list(dict.fromkeys(selected_supplier_ids + list(seen_price_supplier_ids)))
                if supplier_price_rows:
                    price_known = True
                quantity_raw = quantities[i] if i < len(quantities) else ''
                rate_raw = rates[i] if i < len(rates) else ''
                if supplier_price_rows and rate_raw in ('', None):
                    rate_raw = str(supplier_price_rows[0]['price'])
                remarks_raw = product_remarks[i] if i < len(product_remarks) else ''

                if not product_type or quantity_raw in ('', None):
                    messages.error(request, f'Product Type and Qty are required in product row {i + 1}.')
                    return redirect('rfq_details')
                if price_known and rate_raw in ('', None):
                    messages.error(request, f'Add at least one supplier price detail in product row {i + 1}.')
                    return redirect('rfq_details')
                try:
                    quantity, rate_per_unit, value = _calculate_product_line_value(
                        quantity_raw,
                        rate_raw if price_known else '0',
                        i + 1
                    )
                except ValueError as exc:
                    messages.error(request, str(exc))
                    return redirect('rfq_details')

                if quantity <= 0:
                    messages.error(request, f'Qty must be greater than 0 in product row {i + 1}.')
                    return redirect('rfq_details')
                if rate_per_unit < 0:
                    messages.error(request, f'Rate Per Unit cannot be negative in product row {i + 1}.')
                    return redirect('rfq_details')
                for supplier_price_row in supplier_price_rows:
                    supplier_price_row['value'] = (Decimal(quantity) * supplier_price_row['price']).quantize(Decimal('0.01'))

                suppliers_for_price = []
                if selected_supplier_ids:
                    suppliers_for_price = list(Supplier.objects.filter(pk__in=selected_supplier_ids))
                    if len(suppliers_for_price) != len(set(selected_supplier_ids)):
                        messages.error(request, f'Select a valid supplier in product row {i + 1}.')
                        return redirect('rfq_details')

                unit_val = (units[i].strip() if i < len(units) and units[i].strip() else "No's")
                product_rows.append({
                    'id': product_id,
                    'product_name': product_name.strip(),
                    'product_type': product_type,
                    'price_known': price_known,
                    'supplier': suppliers_for_price[0] if suppliers_for_price else None,
                    'suppliers': suppliers_for_price,
                    'quantity': quantity,
                    'unit': unit_val,
                    'rate_per_unit': rate_per_unit,
                    'value': value,
                    'remarks': remarks_raw.strip() or None,
                    'supplier_prices': supplier_price_rows,
                })

            if not product_rows:
                messages.error(request, 'Add at least one RFQ product.')
                return redirect('rfq_details')

        if action == 'add':
            with transaction.atomic():
                rfq = RFQ.objects.create(
                    mail_date=mail_date,
                    customer=customer,
                    enquiry_details=enquiry_details,
                    remarks=remarks or None,
                    attachment=attachment
                )
                for product_row in product_rows:
                    selected_suppliers = product_row.pop('suppliers', [])
                    supplier_prices = product_row.pop('supplier_prices', [])
                    product_row.pop('id', None)
                    rfq_product = RFQProduct.objects.create(rfq=rfq, **product_row)
                    if selected_suppliers:
                        rfq_product.suppliers.set(selected_suppliers)
                    RFQSupplierPrice.objects.bulk_create([
                        RFQSupplierPrice(
                            product=rfq_product,
                            supplier_id=price_row['supplier_id'],
                            price=price_row['price'],
                            value=price_row['value']
                        )
                        for price_row in supplier_prices
                    ])
                    product_row['suppliers'] = selected_suppliers
                    product_row['supplier_prices'] = supplier_prices
            if send_supplier_email:
                subject_to_send = supplier_email_subject
                if '{rfq_no}' in subject_to_send:
                    subject_to_send = subject_to_send.replace('{rfq_no}', rfq.rfq_no or '')
                elif rfq.rfq_no and rfq.rfq_no not in subject_to_send:
                    subject_to_send = f"{subject_to_send} - {rfq.rfq_no}"
                sent_count, failed_suppliers = _send_rfq_supplier_price_requests(
                    rfq,
                    product_rows,
                    subject_to_send,
                    supplier_email_body,
                    supplier_email_attachment,
                    supplier_to_emails,
                    supplier_cc_emails
                )
                if failed_suppliers:
                    messages.warning(request, f"RFQ added, but price request email failed for: {', '.join(failed_suppliers)}.")
                elif sent_count:
                    messages.success(request, f'RFQ added successfully. Price request email sent to {sent_count} supplier(s).')
                else:
                    messages.warning(request, 'RFQ added, but no supplier email was sent because there are no unknown-price products with suppliers.')
            else:
                messages.success(request, 'RFQ added successfully.')
        elif action == 'edit':
            try:
                rfq = RFQ.objects.get(pk=rfq_id)
            except RFQ.DoesNotExist:
                raise Http404
            rfq.mail_date = mail_date
            rfq.customer = customer
            rfq.enquiry_details = enquiry_details
            rfq.remarks = remarks or None
            update_fields = [
                'mail_date',
                'customer',
                'enquiry_details',
                'remarks',
            ]
            if attachment:
                rfq.attachment = attachment
                update_fields.append('attachment')
            with transaction.atomic():
                rfq.save(update_fields=update_fields)

                # Fetch existing products
                existing_products_map = {p.id: p for p in rfq.products.all()}
                submitted_product_ids = [row['id'] for row in product_rows if row.get('id')]

                # Delete any products that were removed in edit modal
                rfq.products.exclude(id__in=submitted_product_ids).delete()

                for product_row in product_rows:
                    selected_suppliers = product_row.pop('suppliers', [])
                    supplier_prices = product_row.pop('supplier_prices', [])
                    prod_id = product_row.pop('id', None)
                    if prod_id and prod_id in existing_products_map:
                        # Update existing product
                        rfq_product = existing_products_map[prod_id]
                        rfq_product.product_name = product_row['product_name']
                        rfq_product.product_type = product_row['product_type']
                        rfq_product.price_known = product_row['price_known']
                        rfq_product.supplier = product_row['supplier']
                        rfq_product.quantity = product_row['quantity']
                        rfq_product.unit = product_row.get('unit', "No's")
                        rfq_product.rate_per_unit = product_row['rate_per_unit']
                        rfq_product.value = product_row['value']
                        rfq_product.remarks = product_row['remarks']
                        rfq_product.save()
                    else:
                        # Create new product
                        rfq_product = RFQProduct.objects.create(rfq=rfq, **product_row)

                    if selected_suppliers:
                        rfq_product.suppliers.set(selected_suppliers)
                    else:
                        rfq_product.suppliers.clear()
                    rfq_product.supplier_prices.all().delete()
                    RFQSupplierPrice.objects.bulk_create([
                        RFQSupplierPrice(
                            product=rfq_product,
                            supplier_id=price_row['supplier_id'],
                            price=price_row['price'],
                            value=price_row['value']
                        )
                        for price_row in supplier_prices
                    ])

                    product_row['suppliers'] = selected_suppliers
                    product_row['supplier_prices'] = supplier_prices

                # Recalculate RFQ level fields
                all_products = list(rfq.products.all())
                if all_products and all(p.quotation_email_sent for p in all_products):
                    rfq.quotation_email_sent = True
                else:
                    rfq.quotation_email_sent = False
                    rfq.email_sent_date = None
                    rfq.quotation_due_date = None
                rfq.save(update_fields=['quotation_email_sent', 'email_sent_date', 'quotation_due_date'])
            if send_supplier_email:
                subject_to_send = supplier_email_subject
                if '{rfq_no}' in subject_to_send:
                    subject_to_send = subject_to_send.replace('{rfq_no}', rfq.rfq_no or '')
                elif rfq.rfq_no and rfq.rfq_no not in subject_to_send:
                    subject_to_send = f"{subject_to_send} - {rfq.rfq_no}"
                sent_count, failed_suppliers = _send_rfq_supplier_price_requests(
                    rfq,
                    product_rows,
                    subject_to_send,
                    supplier_email_body,
                    supplier_email_attachment,
                    supplier_to_emails,
                    supplier_cc_emails
                )
                if failed_suppliers:
                    messages.warning(request, f"RFQ updated, but price request email failed for: {', '.join(failed_suppliers)}.")
                elif sent_count:
                    messages.success(request, f'RFQ updated successfully. Price request email sent to {sent_count} supplier(s).')
                else:
                    messages.warning(request, 'RFQ updated, but no supplier email was sent because there are no unknown-price products with suppliers.')
            else:
                messages.success(request, 'RFQ updated successfully.')
        elif action == 'send_email':
            quotation_record = None
            try:
                rfq = RFQ.objects.select_related('customer').prefetch_related('products').get(pk=rfq_id)
            except RFQ.DoesNotExist:
                raise Http404

            customer_email = request.POST.get('customer_email', '').strip()
            quotation_email_subject = request.POST.get('quotation_email_subject', '').strip()
            quotation_email_body = request.POST.get('quotation_email_body', '').strip()
            quotation_attachment = request.FILES.get('quotation_email_attachment')
            quotation_product_ids = request.POST.getlist('quotation_product_ids')
            quotation_supplier_price_ids = request.POST.getlist('quotation_supplier_price_ids')

            customer_emails = [
                email.strip()
                for email in re.split(r'[;,]', customer_email)
                if email.strip()
            ]
            if not customer_emails:
                messages.error(request, 'Customer email is required to send quotation email.')
                return redirect('rfq_details')
            for email_address in customer_emails:
                try:
                    validate_email(email_address)
                except ValidationError:
                    messages.error(request, f'Enter a valid customer email address: {email_address}')
                    return redirect('rfq_details')
            if not quotation_email_subject or not quotation_email_body:
                messages.error(request, 'Email subject and body are required.')
                return redirect('rfq_details')

            quotation_products, quotation_product_ids_to_mark = _build_selected_quotation_products(
                rfq,
                quotation_product_ids,
                quotation_supplier_price_ids
            )
            if quotation_product_ids or quotation_supplier_price_ids:
                if not quotation_products:
                    messages.error(request, 'Select valid RFQ products or supplier prices for quotation attachment.')
                    return redirect('rfq_details')

            if not quotation_products and not quotation_attachment:
                messages.error(request, 'Select products to auto-attach quotation or upload an attachment.')
                return redirect('rfq_details')

            # Quotation Email Validation: Check if all selected products have finalized prices
            # A product has a known price if: price_known=True AND value > 0
            # This validation is applied before generating PDF or sending email
            if quotation_products:
                invalid_products = [
                    p for p in quotation_products
                    if not p.price_known or not p.value or p.value == 0
                ]

                if invalid_products:
                    messages.error(
                        request,
                        'Quotation email cannot be sent because one or more selected products do not have finalized prices.'
                    )
                    return redirect('rfq_details')

            try:
                email = EmailMessage(
                    subject=quotation_email_subject,
                    body=quotation_email_body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    to=customer_emails,
                )
                if quotation_products:
                    quotation_record = _find_latest_matching_quotation(
                        rfq,
                        quotation_product_ids_to_mark,
                        email_sent=False
                    )
                    if quotation_record is None:
                        quotation_record = _find_latest_matching_quotation(
                            rfq,
                            quotation_product_ids_to_mark,
                            email_sent=None
                        )
                    if quotation_record is not None:
                        quotation_products = _deserialize_quotation_products(quotation_record.products_snapshot)
                        if quotation_product_ids_to_mark:
                            pid_set = {int(x) for x in quotation_product_ids_to_mark if str(x).isdigit()}
                            quotation_products = [p for p in quotation_products if getattr(p, 'id', None) in pid_set]
                    else:
                        quotation_record = _create_rfq_quotation_record(
                            rfq,
                            quotation_products,
                            quotation_product_ids_to_mark,
                            email_sent=False
                        )
                    quote_no = quotation_record.quotation_number
                    pdf_buffer = _build_rfq_quotation_pdf(rfq, quotation_products, quote_no=quote_no)
                    filename = f"{quote_no.replace('/', '_')}.pdf"
                    email.attach(filename, pdf_buffer.getvalue(), 'application/pdf')
                if quotation_attachment:
                    email.attach(
                        quotation_attachment.name,
                        quotation_attachment.read(),
                        getattr(quotation_attachment, 'content_type', None) or 'application/octet-stream'
                    )
                email.send(fail_silently=False)

                if quotation_products:
                    for qp in quotation_products:
                        if hasattr(qp, 'id') and qp.id:
                            RFQProduct.objects.filter(id=qp.id).update(
                                rate_per_unit=qp.rate_per_unit,
                                value=qp.value,
                                quotation_email_sent=True,
                                quotation_prepared=True
                            )
                    RFQProduct.objects.filter(
                        rfq=rfq,
                        id__in=quotation_product_ids_to_mark
                    ).update(quotation_email_sent=True, quotation_prepared=True)

                    if quotation_record:
                        quotation_record.email_sent = True
                        quotation_record.save(update_fields=['email_sent', 'updated_at'])

                rfq.email_sent_date = timezone.now()
                rfq.quotation_due_date = timezone.localdate() + timedelta(days=3)
                rfq.quotation_prepared = True
                rfq.quotation_email_sent = not RFQProduct.objects.filter(
                    rfq=rfq,
                    quotation_email_sent=False
                ).exists()
                rfq.save(update_fields=['email_sent_date', 'quotation_due_date', 'quotation_prepared', 'quotation_email_sent'])

                messages.success(request, f"Quotation email sent to {', '.join(customer_emails)}.")
            except Exception as exc:
                messages.warning(request, f"Quotation email failed for {', '.join(customer_emails)}: {str(exc)[:180]}")
        elif action == 'delete':
            try:
                rfq = RFQ.objects.get(pk=rfq_id)
            except RFQ.DoesNotExist:
                raise Http404
            rfq.delete()
            messages.success(request, 'RFQ deleted successfully.')

        return redirect('rfq_details')

    status_filter = request.GET.get('status', 'all')
    all_rfqs = list(RFQ.objects.select_related('customer').prefetch_related('products__suppliers', 'products__supplier_prices__supplier', 'quotations').order_by('-created_at', '-id'))
    
    confirmed_dprs = list(DPR.objects.exclude(po_attachment='').exclude(po_attachment__isnull=True).values_list('quotation_number', flat=True))
    confirmed_quote_set = set()
    for dpr_quote_str in confirmed_dprs:
        if dpr_quote_str:
            for part in dpr_quote_str.split(','):
                confirmed_quote_set.add(part.strip())

    today = timezone.localdate()
    pending_rfqs = []
    confirmed_rfqs = []
    overdue_rfqs = []

    for rfq in all_rfqs:
        rfq.row_class = _get_rfq_row_alert_class(rfq)
        rfq.is_overdue = (today - rfq.mail_date).days >= 3 if rfq.mail_date else False
        rfq.quotation_prepared = rfq.quotation_prepared or rfq.quotations.exists() or any(p.quotation_prepared or p.quotation_email_sent for p in rfq.products.all())

        rfq.quotation_records_display = list(rfq.quotations.order_by('created_at', 'id'))
        latest_quotation = rfq.quotations.order_by('-created_at', '-id').first()
        if latest_quotation:
            rfq.quotation_no_display = latest_quotation.quotation_number
        elif any(p.quotation_prepared or p.quotation_email_sent for p in rfq.products.all()):
            rfq.quotation_no_display = _get_mes_quote_no(rfq)
        else:
            rfq.quotation_no_display = '-'

        rfq_quote_nos = list(rfq.quotations.values_list('quotation_number', flat=True))
        is_po_confirmed = any(q_no in confirmed_quote_set for q_no in rfq_quote_nos)
        is_quote_submitted = (
            rfq.row_class == 'table-success'
            or rfq.quotation_email_sent
            or rfq.quotations.filter(email_sent=True).exists()
            or is_po_confirmed
        )

        if is_quote_submitted:
            rfq.order_status = 'confirmed'
            confirmed_rfqs.append(rfq)
        elif rfq.row_class == 'table-danger':
            rfq.order_status = 'overdue'
            overdue_rfqs.append(rfq)
        else:
            rfq.order_status = 'pending'
            pending_rfqs.append(rfq)

    if status_filter == 'pending':
        rfqs_to_display = pending_rfqs
    elif status_filter == 'confirmed':
        rfqs_to_display = confirmed_rfqs
    elif status_filter == 'overdue':
        rfqs_to_display = overdue_rfqs
    else:
        rfqs_to_display = all_rfqs

    tab_counts = {
        'all': len(all_rfqs),
        'pending': len(pending_rfqs),
        'confirmed': len(confirmed_rfqs),
        'overdue': len(overdue_rfqs),
    }

    rfq_payloads = []
    for rfq in rfqs_to_display:
        row_class = rfq.row_class
        rfq_payloads.append({
            'id': rfq.id,
            'rfq_no': rfq.rfq_no,
            'quotation_no': rfq.quotation_no_display,
            'mail_date': rfq.mail_date.strftime('%Y-%m-%d') if rfq.mail_date else '',
            'customer_id': rfq.customer_id,
            'customer_name': rfq.customer.customer_name,
            'customer_region': rfq.customer.region or '',
            'customer_email': rfq.customer.email or '',
            'enquiry_details': rfq.enquiry_details,
            'remarks': rfq.remarks or '',
            'attachment_url': rfq.attachment.url if rfq.attachment else '',
            'attachment_name': rfq.attachment.name.split('/')[-1] if rfq.attachment else '',
            'row_class': row_class,  # Row highlighting class for color-based alerts
            'products': [
                {
                    'id': product.id,
                    'product_name': product.product_name,
                    'product_type': product.product_type or '',
                    'price_known': product.price_known,
                    'quotation_email_sent': product.quotation_email_sent,
                    'quotation_prepared': product.quotation_prepared,
                    'supplier_id': product.supplier_id,
                    'supplier_ids': list(product.suppliers.values_list('id', flat=True)),
                    'supplier_prices': [
                        {
                            'id': supplier_price.id,
                            'supplier_id': supplier_price.supplier_id,
                            'supplier_name': supplier_price.supplier.supplier_name,
                            'price': str(supplier_price.price),
                            'value': str(supplier_price.value),
                        }
                        for supplier_price in product.supplier_prices.all()
                    ],
                    'quantity': product.quantity,
                    'unit': product.unit or "No's",
                    'rate_per_unit': str(product.rate_per_unit),
                    'value': str(product.value),
                    'remarks': product.remarks or '',
                }
                for product in rfq.products.all()
            ],
        })
    customers = Customer.objects.order_by('customer_name')
    suppliers = Supplier.objects.order_by('supplier_name')
    return render(request, 'rfq_details.html', {
        'rfqs': rfqs_to_display,
        'customers': customers,
        'suppliers': suppliers,
        'product_type_choices': CustomerProduct.PRODUCT_TYPE_CHOICES,
        'rfq_payloads': rfq_payloads,
        'default_supplier_email_subject': _get_default_supplier_email_subject(),
        'default_supplier_email_body': _get_default_supplier_email_body(),
        'status_filter': status_filter,
        'tab_counts': tab_counts,
    })


@role_required('ADMIN', 'SALES')
def rfq_quotation_download(request, rfq_id):
    try:
        rfq = RFQ.objects.select_related('customer').get(pk=rfq_id)
    except RFQ.DoesNotExist:
        raise Http404

    if request.method == 'GET':
        req_product_ids = [str(x) for x in request.GET.getlist('product_ids') if str(x).strip()]
        req_supplier_price_ids = [str(x) for x in request.GET.getlist('supplier_price_ids') if str(x).strip()]
        quotation_id = request.GET.get('quotation_id', '').strip()

        latest_quotation = None
        if quotation_id.isdigit():
            latest_quotation = RFQQuotation.objects.filter(rfq=rfq, id=int(quotation_id)).first()
        elif req_product_ids or req_supplier_price_ids:
            int_pids = [int(p) for p in req_product_ids if p.isdigit()]
            int_spids = [int(p) for p in req_supplier_price_ids if p.isdigit()]
            if int_pids or int_spids:
                temp_prods, pids_to_mark = _build_selected_quotation_products(rfq, int_pids, int_spids)
                latest_quotation = _find_latest_matching_quotation(rfq, pids_to_mark, email_sent=None)

        if req_product_ids or req_supplier_price_ids:
            products, _ = _build_selected_quotation_products(rfq, req_product_ids, req_supplier_price_ids)
            quote_no = latest_quotation.quotation_number if latest_quotation else _get_mes_quote_no(rfq)
        elif latest_quotation:
            products = _deserialize_quotation_products(latest_quotation.products_snapshot)
            quote_no = latest_quotation.quotation_number
        else:
            products, _ = _build_selected_quotation_products(rfq, [], [])
            if not products:
                products = list(rfq.products.all())
            quote_no = _get_mes_quote_no(rfq)

        pdf_buffer = _build_rfq_quotation_pdf(rfq, products, quote_no=quote_no)
        filename = f"{quote_no.replace('/', '_')}.pdf"
        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response

    product_ids = request.POST.getlist('product_ids')
    supplier_price_ids = request.POST.getlist('supplier_price_ids')
    mes_rates = request.POST.getlist('mes_rates')
    delivery_weeks = request.POST.get('delivery_weeks')
    installation_charge = request.POST.get('installation_charge')
    products, quotation_product_ids_to_mark = _build_selected_quotation_products(
        rfq, product_ids, supplier_price_ids, mes_rates=mes_rates, delivery_weeks=delivery_weeks, installation_charge=installation_charge
    )
    if not products:
        messages.error(request, 'Select at least one product to prepare quotation.')
        return redirect('rfq_details')

    # Quotation Validation: Check if all selected products have finalized prices
    # A product has a known price if: price_known=True AND value > 0
    invalid_products = [
        p for p in products
        if not p.price_known or not p.value or p.value == 0
    ]

    if invalid_products:
        messages.error(
            request,
            'Cannot prepare quotation because one or more selected products do not have a finalized price.'
        )
        return redirect('rfq_details')

    disposition = 'inline' if request.POST.get('preview') == '1' else 'attachment'
    quotation_record = None
    quote_no = _get_mes_quote_no(rfq)
    if disposition == 'attachment' and quotation_product_ids_to_mark:
        quotation_record = _create_rfq_quotation_record(
            rfq,
            products,
            quotation_product_ids_to_mark,
            email_sent=False
        )
        quote_no = quotation_record.quotation_number

    pdf_buffer = _build_rfq_quotation_pdf(rfq, products, quote_no=quote_no)
    filename = f"{quote_no.replace('/', '_')}.pdf"
    response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
    if quotation_record:
        for qp in products:
            if hasattr(qp, 'id') and qp.id:
                RFQProduct.objects.filter(id=qp.id).update(
                    rate_per_unit=qp.rate_per_unit,
                    value=qp.value,
                    quotation_prepared=True
                )
        RFQProduct.objects.filter(
            rfq=rfq,
            id__in=quotation_product_ids_to_mark
        ).update(quotation_prepared=True)
        rfq.quotation_prepared = True
        rfq.save(update_fields=['quotation_prepared'])
    response['Content-Disposition'] = f'{disposition}; filename="{filename}"'
    response.set_cookie('rfq_quotation_downloaded', 'true', path='/')
    return response


@role_required('ADMIN')
def supplier_details(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        supplier_id = request.POST.get('supplier_id')
        supplier_name = request.POST.get('supplier_name', '').strip()
        email = request.POST.get('email', '').strip()
        phone_number = request.POST.get('phone_number', '').strip()
        address = request.POST.get('address', '').strip()
        gstin = request.POST.get('gstin', '').strip().upper()
        payment_terms = request.POST.get('payment_terms', '').strip()


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
                email=email or None,
                phone_number=phone_number or None,
                address=address or None,
                gstin=gstin or None,
                payment_terms=payment_terms or None
            )
            messages.success(request, 'Supplier added successfully.')
        elif action == 'edit':
            try:
                supplier = Supplier.objects.get(pk=supplier_id)
            except Supplier.DoesNotExist:
                raise Http404
            supplier.supplier_name = supplier_name
            supplier.email = email or None
            supplier.phone_number = phone_number or None
            supplier.address = address or None
            supplier.gstin = gstin or None
            supplier.payment_terms = payment_terms or None
            supplier.save(update_fields=['supplier_name', 'email', 'phone_number', 'address', 'gstin', 'payment_terms'])
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

    search_query = request.GET.get('search', '').strip()
    suppliers = Supplier.objects.order_by('supplier_name')
    if search_query:
        suppliers = suppliers.filter(supplier_name__icontains=search_query)
    return render(request, 'supplier_details.html', {'suppliers': suppliers, 'search_query': search_query})

@role_required('ADMIN', 'PURCHASE')
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

        enquiry_attachment = request.FILES.get(
            'enquiry_attachment'
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
        quantities = request.POST.getlist('quantity[]')
        rates = request.POST.getlist('mes_rate_per_unit[]')
        _, po_value_error = _validate_po_value_matches_total(
            request.POST.get('po_value'),
            product_names,
            quantities,
            rates
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

            enquiry_attachment=enquiry_attachment,

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

        rates = request.POST.getlist(
            'mes_rate_per_unit[]'
        )

        mes_rates = request.POST.getlist(
            'mes_rate_per_unit[]'
        )

        remarks_list = request.POST.getlist(
            'remarks[]'
        )

        for i in range(len(product_names)):

            product_name = product_names[i]

            if product_name.strip() == '':
                continue

            try:
                quantity, rate_per_unit, value = _calculate_product_line_value(
                    quantities[i] if i < len(quantities) else '0',
                    rates[i] if i < len(rates) else '0',
                    i + 1
                )
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect('customer_order')

            mes_rate_val = mes_rates[i] if i < len(mes_rates) else '0'
            try:
                mes_rate_per_unit = Decimal(mes_rate_val or '0')
                mes_value = (Decimal(quantity) * mes_rate_per_unit).quantize(Decimal('0.01'))
            except Exception:
                mes_rate_per_unit = Decimal('0.00')
                mes_value = Decimal('0.00')

            remarks = remarks_list[i]

            attachment = request.FILES.get(f'product_attachment_{i}')

            CustomerProduct.objects.create(

                dpr=dpr,

                product_name=product_name,
                product_type=product_types[i] if i < len(product_types) else None,

                quantity_ordered=quantity,

                rate_per_unit=rate_per_unit,
                mes_rate_per_unit=mes_rate_per_unit,

                value=value,
                mes_value=mes_value,

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

@role_required('ADMIN', 'SALES')
def add_customer(request):

    if request.method == 'POST':

        customer_name = request.POST.get(
            'customer_name'
        , '').strip()

        region = request.POST.get(
            'region'
        , '').strip()

        email = request.POST.get(
            'email'
        , '').strip()

        phone_number = request.POST.get(
            'phone_number'
        , '').strip()

        address = request.POST.get(
            'address'
        , '').strip()

        gstin = request.POST.get(
            'gstin'
        , '').strip().upper()

        state_code = request.POST.get(
            'state_code'
        , '').strip().upper()

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

        if email:
            try:
                validate_email(email)
            except ValidationError:
                return JsonResponse({
                    'status': 'error',
                    'message': 'Enter a valid customer email address.',
                    'field': 'email'
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

            email=email or None,

            phone_number=phone_number or None,

            address=address or None,

            gstin=gstin or None,

            state_code=state_code or None
        )

        return JsonResponse({

            'status': 'success',

            'id': customer.id,

            'name': customer.customer_name,

            'region': customer.region,

            'email': customer.email or ''
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


@role_required('ADMIN')
def add_supplier(request):
    if request.method == 'POST':
        supplier_name = request.POST.get('supplier_name', '').strip()
        email = request.POST.get('email', '').strip()
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


@role_required('ADMIN', 'SALES', 'PURCHASE')
def get_customer_quotations(request):
    customer_id = request.GET.get('customer_id')
    if not customer_id:
        return JsonResponse({'status': 'error', 'message': 'customer_id is required'}, status=400)
    try:
        customer = Customer.objects.get(id=customer_id)
    except Customer.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Customer not found'}, status=404)

    rfqs = RFQ.objects.filter(customer=customer).prefetch_related('products', 'quotations').order_by('-created_at')

    quotations = []
    for rfq in rfqs:
        quotation_records = list(rfq.quotations.all().order_by('revision_number'))
        if quotation_records:
            for quotation in quotation_records:
                prepared_not_emailed = not quotation.email_sent
                products_list = []
                for product in quotation.products_snapshot or []:
                    product_data = dict(product)
                    product_data['quotation_email_sent'] = quotation.email_sent
                    product_data['quotation_prepared'] = True
                    product_data['prepared_not_emailed'] = prepared_not_emailed
                    products_list.append(product_data)

                if products_list:
                    quotations.append({
                        'rfq_id': rfq.id,
                        'quotation_id': quotation.id,
                        'rfq_no': rfq.rfq_no,
                        'quotation_number': quotation.quotation_number,
                        'revision_number': quotation.revision_number,
                        'preview_url': f'/rfq/{rfq.id}/quotation/download/?quotation_id={quotation.id}',
                        'status_label': 'Prepared - Email not sent' if prepared_not_emailed else 'Email sent',
                        'prepared_not_emailed': prepared_not_emailed,
                        'products': products_list,
                    })
            continue

        quote_no = _get_mes_quote_no(rfq)
        products_list = []
        has_prepared_not_emailed = False
        for p in rfq.products.all():
            if p.quotation_email_sent or p.quotation_prepared:
                prepared_not_emailed = p.quotation_prepared and not p.quotation_email_sent
                has_prepared_not_emailed = has_prepared_not_emailed or prepared_not_emailed
                products_list.append({
                    'product_id': p.id,
                    'product_name': p.product_name,
                    'product_type': p.product_type or '',
                    'quantity': p.quantity,
                    'rate_per_unit': str(p.rate_per_unit),
                    'value': str(p.value),
                    'remarks': p.remarks or '',
                    'quotation_email_sent': p.quotation_email_sent,
                    'quotation_prepared': p.quotation_prepared,
                    'prepared_not_emailed': prepared_not_emailed,
                })

        if products_list:
            quotations.append({
                'rfq_id': rfq.id,
                'quotation_id': None,
                'rfq_no': rfq.rfq_no,
                'quotation_number': quote_no,
                'revision_number': 0,
                'preview_url': f'/rfq/{rfq.id}/quotation/download/',
                'status_label': 'Prepared - Email not sent' if has_prepared_not_emailed else 'Email sent',
                'prepared_not_emailed': has_prepared_not_emailed,
                'products': products_list,
            })

    return JsonResponse({'status': 'success', 'quotations': quotations})


def _get_uom(product_name):
    return "NOS"


def _get_hsn(product_name):
    name = (product_name or "").lower()
    if "plug" in name:
        return "90173021"
    elif "snap" in name:
        return "90173029"
    elif "ring" in name:
        return "90173022"
    elif "gauge" in name:
        return "90173029"
    else:
        return "90173000"


def _number_to_words(num):
    if num is None:
        return ""
    under_20 = [
        'Zero', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten',
        'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen'
    ]
    tens = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety']
    
    def convert_below_thousand(n):
        if n < 20:
            return under_20[n]
        elif n < 100:
            return tens[n // 10] + ('' if n % 10 == 0 else ' ' + under_20[n % 10])
        else:
            remainder = n % 100
            return under_20[n // 100] + ' Hundred' + ('' if remainder == 0 else ' ' + convert_below_thousand(remainder))

    def convert(n):
        if n == 0:
            return 'Zero'
        parts = []
        if n >= 10000000:
            parts.append(convert_below_thousand(n // 10000000) + ' Crore')
            n %= 10000000
        if n >= 100000:
            parts.append(convert_below_thousand(n // 100000) + ' Lakh')
            n %= 100000
        if n >= 1000:
            parts.append(convert_below_thousand(n // 1000) + ' Thousand')
            n %= 1000
        if n > 0:
            parts.append(convert_below_thousand(n))
        return ' '.join(parts)

    try:
        val_dec = Decimal(str(num)).quantize(Decimal('0.01'))
        integer_part = int(val_dec)
        decimal_part = int((val_dec - integer_part) * 100)
    except Exception:
        integer_part = int(num)
        decimal_part = 0

    if integer_part == 0:
        words = "Zero Rupees"
    else:
        words = convert(integer_part) + " Rupees"
        
    if decimal_part > 0:
        words += " and " + convert(decimal_part) + " Paise"
        
    words += " Only."
    return words


def _build_single_po_story(dpr, supplier, items, delivery_address='hosur'):
    import os
    from xml.sax.saxutils import escape as xml_escape
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle, Image

    styles = getSampleStyleSheet()
    
    normal_style = ParagraphStyle(
        'MESNormal',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        alignment=TA_LEFT,
    )
    
    bold_style = ParagraphStyle(
        'MESBold',
        parent=normal_style,
        fontName='Helvetica-Bold',
    )
    
    center_style = ParagraphStyle(
        'MESCenter',
        parent=normal_style,
        alignment=TA_CENTER,
    )

    center_bold_style = ParagraphStyle(
        'MESCenterBold',
        parent=normal_style,
        fontName='Helvetica-Bold',
        alignment=TA_CENTER,
    )

    right_style = ParagraphStyle(
        'MESRight',
        parent=normal_style,
        alignment=TA_RIGHT,
    )
    
    right_bold_style = ParagraphStyle(
        'MESRightBold',
        parent=normal_style,
        fontName='Helvetica-Bold',
        alignment=TA_RIGHT,
    )

    def pdf_text(value):
        return xml_escape(str(value or ''))

    story = []

    # 1. Header Table
    logo_path = os.path.join(settings.BASE_DIR, 'static', 'images', 'mes_logo.png')
    if os.path.exists(logo_path):
        logo_cell = Image(logo_path, width=30 * mm, height=21 * mm)
    else:
        logo_cell = Paragraph('<b><font size="24" color="white">MES</font></b>', ParagraphStyle('LogoText', alignment=TA_CENTER, leading=26))

    company_details = Paragraph(
        '<b><font size="11">METROLOGY ENGINEERING SOLUTIONS</font></b><br/>'
        'NO.684/9, Sri Sai Jayalakshmi Complex, Maruthi Nagar ,<br/>'
        '2nd Cross,Dharga, Opposite to Sathya mess,Hosur,Krishnagiri, Tamilnadu-635109.<br/>'
        'Phone : +91-965-577-8807 / +91-965-577-8871<br/>'
        'Email : info@mesinstruments.co.in | Web : www.mesinstruments.co.in',
        center_style
    )

    header_table = Table([[logo_cell, company_details]], colWidths=[32 * mm, 148 * mm])
    header_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ('LINEBEFORE', (1, 0), (1, -1), 1, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (0, 0), 1),
        ('RIGHTPADDING', (0, 0), (-1, -1), 1),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 2 * mm))

    # 2. PURCHASE ORDER bar
    po_title_table = Table([[Paragraph('<b>PURCHASE ORDER</b>', center_bold_style)]], colWidths=[180 * mm])
    po_title_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(po_title_table)
    story.append(Spacer(1, 2 * mm))

    # 3. Buyer & Vendor Details Table
    effective_po_no = items[0].po_number or 'PO'
    po_date_str = items[0].po_date.strftime('%d-%m-%Y') if items[0].po_date else timezone.localdate().strftime('%d-%m-%Y')
    po_validity_str = items[0].po_validity.strftime('%d-%m-%Y') if items[0].po_validity else (dpr.po_validity.strftime('%d-%m-%Y') if dpr.po_validity else '-')

    buyer_text = (
        '<b>NAME : METROLOGY ENGINEERING SOLUTIONS</b><br/>'
        'NO.684/9, Sri Sai Jayalakshmi Complex, Maruthi Nagar ,<br/>'
        '2nd Cross,Dharga, Opposite to Sathya mess,Hosur,Krishnagiri,<br/>'
        'Tamilnadu-635109.     Phone : +91-965-577-8807<br/>'
        'GSTIN : 33BIKPG0091L1ZU'
    )
    buyer_para = Paragraph(buyer_text, normal_style)

    vendor_lines = [
        f'<b>NAME : {pdf_text(supplier.supplier_name.upper())}</b>'
    ]
    if supplier.address:
        vendor_lines.append(pdf_text(supplier.address).replace('\n', '<br/>'))
    if supplier.phone_number:
        vendor_lines.append(f'PH : {pdf_text(supplier.phone_number)}')
    if supplier.email:
        vendor_lines.append(f'Email : {pdf_text(supplier.email)}')

    supplier_gstin = (getattr(supplier, 'gstin', None) or '').strip()
    if not supplier_gstin and supplier.address:
        gst_match = re.search(r'GST(?:IN)?\s*[:\-]?\s*([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}[Z]{1}[0-9A-Z]{1})', supplier.address, re.IGNORECASE)
        if gst_match:
            supplier_gstin = gst_match.group(1)
        else:
            gst_match_alt = re.search(r'\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z]{1}[1-9A-Z]{1}[Z]{1}[0-9A-Z]{1}\b', supplier.address)
            if gst_match_alt:
                supplier_gstin = gst_match_alt.group(0)

    if supplier_gstin:
        vendor_lines.append(f'GSTIN : {pdf_text(supplier_gstin)}')
    vendor_para = Paragraph('<br/>'.join(vendor_lines), normal_style)

    meta_lines = [
        f'<b>PO NO</b> : {effective_po_no}',
        f'<b>DATE</b> : {po_date_str}',
        f'<b>PO Validity</b> : {po_validity_str}',
        f'<b>Quotation No</b> : {pdf_text(dpr.quotation_number or "-")}',
    ]
    meta_para = Paragraph('<br/>'.join(meta_lines), normal_style)

    buyer_vendor_table = Table(
        [
            [Paragraph('<b>BUYER</b>', bold_style), meta_para],
            [buyer_para, Paragraph(f'<b>VENDOR : {pdf_text(supplier.supplier_name.upper())}</b><br/>' + '<br/>'.join(vendor_lines[1:]), normal_style)],
        ],
        colWidths=[90 * mm, 90 * mm]
    )
    buyer_vendor_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ('LINEBEFORE', (1, 0), (1, -1), 1, colors.black),
        ('LINEBELOW', (0, 0), (0, 0), 1, colors.black),
        ('LINEBELOW', (1, 0), (1, 0), 1, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(buyer_vendor_table)
    story.append(Spacer(1, 2 * mm))

    # 4. PURCHASE ORDER PRODUCTS bar
    po_prod_title_table = Table([[Paragraph('<b>PURCHASE ORDER PRODUCTS</b>', center_bold_style)]], colWidths=[180 * mm])
    po_prod_title_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(po_prod_title_table)
    story.append(Spacer(1, 2 * mm))

    # 5. Products Table
    prod_header = [
        Paragraph('<b>SL/NO</b>', center_bold_style),
        Paragraph('<b>ITEM DISCRIPTION</b>', center_bold_style),
        Paragraph('<b>HSN / SAC</b>', center_bold_style),
        Paragraph('<b>QTY</b>', center_bold_style),
        Paragraph('<b>UOM</b>', center_bold_style),
        Paragraph('<b>RATE</b>', center_bold_style),
        Paragraph('<b>DISC %</b>', center_bold_style),
        Paragraph('<b>TOTAL</b>', center_bold_style),
    ]
    prod_data = [prod_header]
    
    basic_total = Decimal('0.00')
    for i, sp in enumerate(items):
        qty = sp.quantity or 0
        rate = sp.rate_per_unit or Decimal('0.00')
        line_total = sp.po_value or (qty * rate)
        basic_total += line_total
        
        desc_lines = [f'<b>{pdf_text(sp.customer_product.product_name.upper())}</b>']
        if sp.customer_product.remarks:
            clean_remarks = pdf_text(sp.customer_product.remarks).replace('\n', '<br/>')
            desc_lines.append(clean_remarks)
            
        desc_para = Paragraph('<br/>'.join(desc_lines), normal_style)
        hsn = _get_hsn(sp.customer_product.product_name)
        uom = _get_uom(sp.customer_product.product_name)
        
        prod_data.append([
            Paragraph(str(i + 1), center_style),
            desc_para,
            Paragraph(hsn, center_style),
            Paragraph(str(qty), center_style),
            Paragraph(uom, center_style),
            Paragraph(f'{rate:,.2f}', right_style),
            Paragraph('NILL', center_style),
            Paragraph(f'{line_total:,.2f}', right_style),
        ])

    prod_table = Table(prod_data, colWidths=[12 * mm, 68 * mm, 22 * mm, 12 * mm, 12 * mm, 20 * mm, 14 * mm, 20 * mm])
    prod_table.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(prod_table)
    story.append(Spacer(1, 2 * mm))

    # 6. Remarks & Totals Table
    is_igst = False
    if supplier_gstin and len(supplier_gstin) >= 2 and supplier_gstin[:2].isdigit():
        is_igst = (supplier_gstin[:2] != '33')
    elif supplier.address:
        addr_lower = supplier.address.lower()
        if 'tamil nadu' not in addr_lower and 'tamilnadu' not in addr_lower and 'hosur' not in addr_lower and 'chennai' not in addr_lower and ('karnataka' in addr_lower or 'bangalore' in addr_lower or 'bengaluru' in addr_lower or 'maharashtra' in addr_lower or 'mumbai' in addr_lower or 'pune' in addr_lower or 'delhi' in addr_lower or 'gujarat' in addr_lower or 'andhra' in addr_lower or 'telangana' in addr_lower or 'kerala' in addr_lower):
            is_igst = True
    gst_rate = Decimal('18.00')
    gst_amount = (basic_total * gst_rate / Decimal('100.00')).quantize(Decimal('0.01'))
    grand_total = basic_total + gst_amount

    remarks_col = Paragraph(
        '<b>REMARKS :</b><br/>'
        '1. Please return the duplicate copy of this order duly signed in as a token of your acceptance.<br/>'
        '2. Mention our Purchase Order number in all your Delivery challans, Invoices & other correspondence documents.<br/>'
        '3. Test Certificate (calibration Certificate) is Mandatory where ever applicable.',
        normal_style
    )

    totals_data = [
        [remarks_col, Paragraph('<b>BASIC</b>', bold_style), Paragraph(f'{basic_total:,.2f}', right_bold_style)],
    ]
    if is_igst:
        totals_data.append(['', Paragraph('<b>IGST 18%</b>', bold_style), Paragraph(f'{gst_amount:,.2f}', right_bold_style)])
    else:
        half_gst = (gst_amount / Decimal('2.00')).quantize(Decimal('0.01'))
        totals_data.append(['', Paragraph('<b>CGST 9%</b>', bold_style), Paragraph(f'{half_gst:,.2f}', right_bold_style)])
        totals_data.append(['', Paragraph('<b>SGST 9%</b>', bold_style), Paragraph(f'{half_gst:,.2f}', right_bold_style)])
        
    totals_data.append(['', Paragraph('<b>GRAND TOTAL</b>', bold_style), Paragraph(f'{grand_total:,.2f}', right_bold_style)])

    remarks_table = Table(totals_data, colWidths=[100 * mm, 40 * mm, 40 * mm])
    remarks_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ('SPAN', (0, 0), (0, -1)),
        ('LINEBEFORE', (1, 0), (1, -1), 1, colors.black),
        ('LINEBEFORE', (2, 0), (2, -1), 1, colors.black),
        ('LINEBELOW', (1, 0), (-1, 0), 1, colors.black),
        ('LINEBELOW', (1, 1), (-1, 1), 1, colors.black),
        ('LINEBELOW', (1, 2), (-1, 2), 1, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(remarks_table)
    story.append(Spacer(1, 2 * mm))

    # 7. Amount in Words Table
    words_text = _number_to_words(grand_total)
    words_table = Table([[Paragraph(f'Amount in words : <b>{words_text}</b>', normal_style)]], colWidths=[180 * mm])
    words_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(words_table)
    story.append(Spacer(1, 2 * mm))

    # 8. Invoice / Delivery Address Table
    invoice_addr = Paragraph(
        '<b>METROLOGY ENGINEERING SOLUTIONS</b><br/>'
        'NO.684/9, Sri Sai Jayalakshmi Complex, Maruthi Nagar ,<br/>'
        '2nd Cross,Dharga, Opposite to Sathya mess,Hosur,Krishnagiri,<br/>'
        'Tamilnadu-635109.     Phone : +91-965-577-8807',
        normal_style
    )
    if str(delivery_address).lower() == 'chennai':
        delivery_addr = Paragraph(
            '<b>METROLOGY ENGINEERING SOLUTIONS</b><br/>'
            '14/65, 6th Street, Kamaraj Nagar, Korratur,<br/>'
            'Chennai, Tamil Nadu, India-600 080<br/>'
            'Phone : +91-965-577-8807',
            normal_style
        )
    else:
        delivery_addr = Paragraph(
            '<b>METROLOGY ENGINEERING SOLUTIONS</b><br/>'
            'NO.684/9, Sri Sai Jayalakshmi Complex, Maruthi Nagar ,<br/>'
            '2nd Cross,Dharga, Opposite to Sathya mess,Hosur,Krishnagiri,<br/>'
            'Tamilnadu-635109.     Phone : +91-965-577-8807',
            normal_style
        )
    
    addr_data = [
        [Paragraph('<b>INVOICE ADDRESS :</b>', bold_style), Paragraph('<b>DELIVERY ADDRESS :</b>', bold_style)],
        [invoice_addr, delivery_addr]
    ]
    addr_table = Table(addr_data, colWidths=[90 * mm, 90 * mm])
    addr_table.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 1, colors.black),
        ('LINEBELOW', (0, 0), (-1, 0), 1, colors.black),
        ('LINEBEFORE', (1, 0), (1, -1), 1, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(addr_table)
    return story


def _build_single_po_pdf_buffer(dpr, supplier, items, delivery_address='hosur'):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=14 * mm,
        leftMargin=14 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    story = _build_single_po_story(dpr, supplier, items, delivery_address=delivery_address)
    doc.build(story)
    buffer.seek(0)
    return buffer


def _build_multi_supplier_po_pdf_buffer(dpr, groups, delivery_address='hosur'):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, PageBreak

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=14 * mm,
        leftMargin=14 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    story = []
    supplier_list = list(groups.items())
    for idx, (supplier, items) in enumerate(supplier_list):
        if idx > 0:
            story.append(PageBreak())
        story.extend(_build_single_po_story(dpr, supplier, items, delivery_address=delivery_address))
    doc.build(story)
    buffer.seek(0)
    return buffer


@role_required('ADMIN', 'PURCHASE')
def generate_supplier_po(request, dpr_id):
    """Generate a PDF Purchase Order for all supplier products linked to a DPR."""
    try:
        dpr = DPR.objects.get(pk=dpr_id)
    except DPR.DoesNotExist:
        raise Http404

    from products.models import SupplierProduct
    supplier_products = SupplierProduct.objects.filter(
        customer_product__dpr=dpr
    ).select_related('customer_product', 'supplier')

    if not supplier_products.exists():
        messages.error(request, 'No supplier orders found for this DPR to generate a PO.')
        return redirect('dpr_supplier', dpr_id=dpr_id)

    delivery_address = request.POST.get('delivery_address') or request.GET.get('delivery_address') or 'hosur'

    target_customer_date = dpr.po_validity or dpr.po_date
    if target_customer_date:
        invalid_supplier_order = supplier_products.filter(
            po_validity__gte=target_customer_date
        ).select_related('supplier').first()
        if invalid_supplier_order:
            messages.warning(
                request,
                f'PO was not generated because supplier PO validity ({invalid_supplier_order.po_validity.strftime("%d-%m-%Y")}) must be '
                f'before Customer PO date ({target_customer_date.strftime("%d-%m-%Y")}).'
            )
            return redirect('dpr_supplier', dpr_id=dpr_id)

    # Check for single PO preview filters
    preview_supplier_id = request.POST.get('supplier_id') or request.GET.get('supplier_id')
    preview_po_number = request.POST.get('po_number') or request.GET.get('po_number')
    preview_supplier_product_id = (
        request.POST.get('supplier_product_id')
        or request.GET.get('supplier_product_id')
    )
    if preview_supplier_id or preview_po_number or preview_supplier_product_id:
        preview_items = supplier_products
        if preview_supplier_id:
            try:
                supplier = Supplier.objects.get(pk=preview_supplier_id)
                preview_items = preview_items.filter(supplier=supplier)
            except Supplier.DoesNotExist:
                raise Http404
        elif preview_po_number:
            preview_items = preview_items.filter(po_number=preview_po_number)
        elif preview_supplier_product_id:
            sp_obj = supplier_products.filter(pk=preview_supplier_product_id).first()
            if sp_obj:
                if sp_obj.supplier_id:
                    preview_items = preview_items.filter(supplier_id=sp_obj.supplier_id)
                elif sp_obj.po_number:
                    preview_items = preview_items.filter(po_number=sp_obj.po_number)
                else:
                    preview_items = preview_items.filter(pk=sp_obj.id)

        items = list(preview_items)
        if items:
            supplier = items[0].supplier
            effective_po_number = items[0].po_number or preview_po_number or 'PO'
            pdf_buffer = _build_single_po_pdf_buffer(dpr, supplier, items, delivery_address=delivery_address)
            safe_po_num = re.sub(r'[^a-zA-Z0-9_\-]', '_', effective_po_number)
            filename = f"{safe_po_num}.pdf"
            response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
            response['Content-Disposition'] = f'inline; filename="{filename}"'
            return response

    # Assign one stable PO number per supplier when the PO is generated.
    # Existing generated numbers are preserved on previews/re-downloads.
    po_date = timezone.localdate()
    supplier_ids = supplier_products.values_list('supplier_id', flat=True).distinct()
    for supplier_id in supplier_ids:
        generated_po_number = (
            supplier_products.filter(
                supplier_id=supplier_id,
                po_number__startswith='SPO-',
            ).values_list('po_number', flat=True).first()
            or f'SPO-{po_date:%Y%m%d}-{dpr.id:04d}-{supplier_id:04d}'
        )
        supplier_products.filter(
            supplier_id=supplier_id,
        ).exclude(po_number=generated_po_number).update(
            po_number=generated_po_number,
            po_date=po_date,
        )
        # Mark that the PO PDF has been generated for all rows of this supplier
        supplier_products.filter(supplier_id=supplier_id).update(po_pdf_generated=True)

    supplier_products = SupplierProduct.objects.filter(
        customer_product__dpr=dpr
    ).select_related('customer_product', 'supplier')

    # Group all products into one PO per supplier.
    from collections import defaultdict
    groups = defaultdict(list)
    for sp in supplier_products:
        groups[sp.supplier].append(sp)

    if len(groups) == 1:
        # Only one supplier PO, return it directly as a PDF.
        supplier, items = list(groups.items())[0]
        po_number = items[0].po_number or 'PO'
        pdf_buffer = _build_single_po_pdf_buffer(dpr, supplier, items, delivery_address=delivery_address)
        
        safe_po_num = re.sub(r'[^a-zA-Z0-9_\-]', '_', po_number)
        filename = f"{safe_po_num}.pdf"
        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response
    else:
        # Multiple POs -> Combined multi-page PDF for all suppliers
        pdf_buffer = _build_multi_supplier_po_pdf_buffer(dpr, groups, delivery_address=delivery_address)
        filename = f"{dpr.serial_number}_Supplier_POs.pdf"
        response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
        response['Content-Disposition'] = f'inline; filename="{filename}"'
        return response


@role_required('ADMIN', 'PURCHASE')
def send_supplier_po_email(request, dpr_id):
    """Generate and send PO PDF as email attachment to each supplier of a DPR."""
    try:
        dpr = DPR.objects.get(pk=dpr_id)
    except DPR.DoesNotExist:
        raise Http404

    from products.models import SupplierProduct
    supplier_products = SupplierProduct.objects.filter(
        customer_product__dpr=dpr
    ).select_related('customer_product', 'supplier')

    if not supplier_products.exists():
        messages.error(request, 'No supplier orders found for this DPR to send a PO email.')
        return redirect('dpr_supplier', dpr_id=dpr_id)

    if request.method == 'POST':
        supplier_id = request.POST.get('supplier_id')
        po_number = request.POST.get('po_number')
        supplier_product_id = request.POST.get('supplier_product_id')
        combined_supplier = request.POST.get('combined_supplier') == '1'
        supplier_email = request.POST.get('supplier_email', '').strip()
        email_subject = request.POST.get('email_subject', '').strip()
        email_body = request.POST.get('email_body', '').strip()
        email_attachment = request.FILES.get('email_attachment')

        supplier_emails = [
            email.strip()
            for email in re.split(r'[;,]', supplier_email)
            if email.strip()
        ]
        if not supplier_emails:
            messages.error(request, 'Supplier email is required.')
            return redirect('dpr_supplier', dpr_id=dpr_id)
        for email_address in supplier_emails:
            try:
                validate_email(email_address)
            except ValidationError:
                messages.error(request, f'Enter a valid supplier email address: {email_address}')
                return redirect('dpr_supplier', dpr_id=dpr_id)
        if not email_subject or not email_body:
            messages.error(request, 'Email subject and body are required.')
            return redirect('dpr_supplier', dpr_id=dpr_id)

        try:
            supplier = Supplier.objects.get(pk=supplier_id)
        except Supplier.DoesNotExist:
            messages.error(request, 'Selected supplier does not exist.')
            return redirect('dpr_supplier', dpr_id=dpr_id)

        email_items = supplier_products.filter(supplier=supplier)
        if not combined_supplier:
            email_items = email_items.filter(po_number=po_number)
        if supplier_product_id and not combined_supplier:
            email_items = email_items.filter(pk=supplier_product_id)
        items = list(email_items)
        if not items:
            messages.error(request, 'No PO products found for the selected supplier and PO number.')
            return redirect('dpr_supplier', dpr_id=dpr_id)

        try:
            pdf_buffer = _build_single_po_pdf_buffer(dpr, supplier, items)
            email = EmailMessage(
                subject=email_subject,
                body=email_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=supplier_emails,
            )
            safe_po_num = re.sub(r'[^a-zA-Z0-9_\-]', '_', po_number)
            item_suffix = (
                f"_item_{supplier_product_id}"
                if supplier_product_id
                else ''
            )
            filename = f"{safe_po_num}{item_suffix}.pdf"
            email.attach(filename, pdf_buffer.getvalue(), 'application/pdf')

            if email_attachment:
                email.attach(
                    email_attachment.name,
                    email_attachment.read(),
                    getattr(email_attachment, 'content_type', None) or 'application/octet-stream'
                )

            email.send(fail_silently=False)
            SupplierProduct.objects.filter(
                pk__in=[item.pk for item in items]
            ).update(po_email_sent=True)
            messages.success(request, f"PO email sent successfully to: {supplier.supplier_name} ({', '.join(supplier_emails)})")
        except Exception as exc:
            messages.error(request, f"Failed to send email for {supplier.supplier_name}: {exc}")

        remaining_unsent = SupplierProduct.objects.filter(
            customer_product__dpr=dpr,
            po_email_sent=False
        ).exists()
        if not remaining_unsent:
            return redirect('dpr_view')
        return redirect('dpr_supplier', dpr_id=dpr_id)

    # Group all products into one email attachment per supplier.
    from collections import defaultdict
    groups = defaultdict(list)
    for sp in supplier_products:
        groups[sp.supplier].append(sp)

    sent_emails = []
    missing_emails = []
    failed_emails = []

    for supplier, items in groups.items():
        po_number = items[0].po_number or 'PO'
        if not supplier.email:
            missing_emails.append(supplier.supplier_name)
            continue

        try:
            pdf_buffer = _build_single_po_pdf_buffer(dpr, supplier, items)
            
            subject = f"Purchase Order {po_number} - Metrology Engineering Solutions"
            body = (
                f"Dear {supplier.supplier_name},\n\n"
                f"Please find attached the Purchase Order ({po_number}) for {dpr.serial_number}.\n\n"
                f"Regards,\n"
                f"Metrology Engineering Solutions"
            )
            
            email = EmailMessage(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[supplier.email],
            )
            
            safe_po_num = re.sub(r'[^a-zA-Z0-9_\-]', '_', po_number)
            filename = f"{safe_po_num}.pdf"
            email.attach(filename, pdf_buffer.getvalue(), 'application/pdf')
            email.send(fail_silently=False)
            SupplierProduct.objects.filter(
                pk__in=[item.pk for item in items]
            ).update(po_email_sent=True)
            
            sent_emails.append(f"{supplier.supplier_name} ({supplier.email})")
        except Exception as exc:
            failed_emails.append(f"{supplier.supplier_name} (Error: {exc})")

    # Status alerts
    if sent_emails:
        messages.success(request, f"PO email sent successfully to: {', '.join(sent_emails)}")
    if missing_emails:
        messages.warning(request, f"Could not send email for: {', '.join(missing_emails)} (Missing Email ID).")
    if failed_emails:
        messages.error(request, f"Failed to send email for: {', '.join(failed_emails)}")

    remaining_unsent = SupplierProduct.objects.filter(
        customer_product__dpr=dpr,
        po_email_sent=False
    ).exists()
    if not remaining_unsent:
        return redirect('dpr_view')
    return redirect('dpr_supplier', dpr_id=dpr_id)


@role_required('ADMIN', 'SALES', 'PURCHASE')
def check_customer_po_number(request):
    """AJAX endpoint: checks if a customer PO number already exists (for duplicate validation)."""
    po_number = request.GET.get('po_number', '').strip()
    dpr_id = request.GET.get('dpr_id', '')
    if not po_number:
        return JsonResponse({'exists': False})
    qs = DPR.objects.filter(customer_po_number__iexact=po_number)
    if dpr_id:
        try:
            qs = qs.exclude(pk=int(dpr_id))
        except (ValueError, TypeError):
            pass
    return JsonResponse({'exists': qs.exists()})


def _build_customer_invoice_pdf(product_id, invoice_id=None, selected_product_ids=None, custom_qtys=None):
    from products.models import CustomerProduct, CustomerInvoice
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, HRFlowable, Image
    )
    from reportlab.pdfgen import canvas
    from django.conf import settings
    import os

    try:
        target_product = CustomerProduct.objects.select_related('dpr', 'dpr__customer').get(pk=product_id)
    except CustomerProduct.DoesNotExist:
        return None

    target_invoice = None
    if invoice_id:
        try:
            target_invoice = CustomerInvoice.objects.get(pk=invoice_id, customer_product=target_product)
        except CustomerInvoice.DoesNotExist:
            target_invoice = None

    dpr = target_product.dpr
    customer = dpr.customer
    if selected_product_ids:
        products = list(CustomerProduct.objects.filter(dpr=dpr, id__in=selected_product_ids))
    else:
        products = [target_product]

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=8 * mm,
        bottomMargin=10 * mm,
    )

    styles = getSampleStyleSheet()

    normal = ParagraphStyle(
        'InvNormal',
        parent=styles['Normal'],
        fontName='Times-Roman',
        fontSize=9,
        leading=11.5,
        alignment=TA_LEFT,
    )
    bold = ParagraphStyle(
        'InvBold',
        parent=normal,
        fontName='Times-Bold',
    )
    title_style = ParagraphStyle(
        'InvTitle',
        parent=normal,
        fontName='Times-Bold',
        fontSize=13,
        leading=15,
        alignment=TA_CENTER,
    )
    right_align = ParagraphStyle(
        'InvRight',
        parent=normal,
        fontName='Times-Roman',
        fontSize=9,
        leading=11.5,
        alignment=TA_RIGHT,
    )
    right_bold = ParagraphStyle(
        'InvRightBold',
        parent=normal,
        fontName='Times-Bold',
        fontSize=9,
        leading=11.5,
        alignment=TA_RIGHT,
    )
    center_align = ParagraphStyle(
        'InvCenter',
        parent=normal,
        fontName='Times-Roman',
        fontSize=9,
        leading=11.5,
        alignment=TA_CENTER,
    )
    center_bold = ParagraphStyle(
        'InvCenterBold',
        parent=bold,
        alignment=TA_CENTER,
    )

    class NumberedCanvas(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.pages = []

        def showPage(self):
            self.pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            page_count = len(self.pages)
            for page in self.pages:
                self.__dict__.update(page)
                self.saveState()
                self.setFont("Times-Bold", 9)
                self.drawRightString(
                    self._pagesize[0] - 10 * mm,
                    5 * mm,
                    f"Page {self._pageNumber} of {page_count}"
                )
                self.restoreState()
                super().showPage()
            super().save()

    story = []

    story.append(Paragraph("<b>Original</b>", right_bold))
    story.append(Spacer(1, 1 * mm))
    story.append(Paragraph("TAX INVOICE", title_style))
    story.append(Spacer(1, 1.5 * mm))

    comp_header = (
        "<b>METROLOGY ENGINEERING SOLUTIONS</b><br/>"
        "NO.684/9, Sri Sai Jayalakshmi Complex, Maruthi Nagar,<br/>"
        "2nd Cross, Dharga, Opposite to Sathya mess, Hosur, Krishnagiri,<br/>"
        "Tamilnadu - 635109,<br/>"
        "Contact: 9655778871, 9655778807<br/>"
        "Email : info@mesinstruments.co.in<br/>"
        "GSTIN : 33ABKFM1033E1ZS"
    )

    cust_atten = customer.email.split('@')[0] if (customer.email and '@' in customer.email) else (customer.customer_name or "Mr.Nizamuddeen S")
    cust_info = (
        f"Kindly Atten : {cust_atten}<br/>"
        f"<b>M/s. {customer.customer_name}</b><br/>"
        f"{customer.address or ''}<br/>"
        f"{customer.region or ''}<br/>"
        f"Contact Number : {customer.phone_number or ''}<br/>"
        f"Mail Id : {customer.email or ''}<br/>"
        f"GSTIN : {customer.gstin or '-'}"
    )

    left_cell_content = [
        Paragraph(comp_header, normal),
        HRFlowable(width="100%", thickness=0.5, color=colors.black, spaceBefore=2, spaceAfter=2),
        Paragraph(cust_info, normal),
    ]

    if target_invoice:
        inv_no = target_invoice.invoice_number
        inv_date = target_invoice.invoice_date.strftime('%d/%m/%Y') if target_invoice.invoice_date else timezone.localdate().strftime('%d/%m/%Y')
    else:
        inv_no_val = (target_product.invoice_dc_number or '').strip()
        match = re.search(r'(\d+)', inv_no_val) if inv_no_val else None
        if match:
            inv_no = f"MES-F{int(match.group(1)):04d}"
        elif inv_no_val:
            inv_no = inv_no_val
        else:
            inv_no = f"MES-F{target_product.id:04d}"
        inv_date = timezone.localdate().strftime('%d/%m/%Y')

    po_no = dpr.po_number or dpr.serial_number or '-'
    po_date_str = dpr.po_date.strftime('%d/%m/%Y') if dpr.po_date else '-'
    raw_terms = str(customer.payment_terms).strip() if customer.payment_terms else ''
    if raw_terms.isdigit():
        terms = f"{raw_terms} Week{'s' if raw_terms != '1' else ''}"
    elif raw_terms:
        terms = raw_terms
    else:
        terms = '30 Days Against Invoice'

    right_table_data = [
        [Paragraph("Invoice No :", normal), Paragraph(f"<b>{inv_no}</b>", normal), Paragraph("Dated :", normal), Paragraph(f"<b>{inv_date}</b>", normal)],
        [Paragraph("Vendor Code :", normal), Paragraph("", normal), Paragraph("Terms of Payment :", normal), Paragraph(f"<b>{terms}</b>", normal)],
        [Paragraph("Supplier Ref :", normal), Paragraph("", normal), Paragraph("Other Ref :", normal), Paragraph("", normal)],
        [Paragraph("Buyer's PO No :", normal), Paragraph(f"<b>{po_no}</b>", normal), Paragraph("Dated :", normal), Paragraph(f"<b>{po_date_str}</b>", normal)],
        [Paragraph("Despatch Through :", normal), Paragraph("By Hand", normal), Paragraph("Destination :", normal), Paragraph("", normal)],
        [Paragraph("Shipping Address :", normal), Paragraph("", normal), Paragraph("", normal), Paragraph("", normal)],
    ]

    right_table = Table(
        right_table_data,
        colWidths=[24 * mm, 30 * mm, 28 * mm, 28 * mm]
    )
    right_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 1),
        ('TOPPADDING', (0,0), (-1,-1), 1),
        ('LEFTPADDING', (0,0), (-1,-1), 1),
        ('RIGHTPADDING', (0,0), (-1,-1), 1),
        ('LINEBELOW', (0,0), (-1,-1), 0.3, colors.lightgrey),
    ]))

    header_grid = Table([[left_cell_content, right_table]], colWidths=[80 * mm, 110 * mm])
    header_grid.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('BOX', (0,0), (-1,-1), 1, colors.black),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.black),
        ('TOPPADDING', (0,0), (-1,-1), 2),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ('LEFTPADDING', (0,0), (-1,-1), 3),
        ('RIGHTPADDING', (0,0), (-1,-1), 3),
    ]))

    story.append(header_grid)
    story.append(Spacer(1, 1.5 * mm))

    headers = [
        Paragraph("<b>S.No</b>", center_bold),
        Paragraph("<b>Part No</b>", center_bold),
        Paragraph("<b>Description</b>", center_bold),
        Paragraph("<b>HSN/SAC</b>", center_bold),
        Paragraph("<b>Qty</b>", center_bold),
        Paragraph("<b>Unit</b>", center_bold),
        Paragraph("<b>Rate</b>", center_bold),
        Paragraph("<b>Total</b>", center_bold),
    ]

    prod_table_data = [headers]

    total_qty = 0
    subtotal = Decimal('0.00')

    for idx, p in enumerate(products, 1):
        if target_invoice and p.id == target_invoice.customer_product_id:
            qty = target_invoice.quantity
        elif custom_qtys and p.id in custom_qtys:
            qty = custom_qtys[p.id]
        else:
            qty = p.quantity_delivered if p.quantity_delivered > 0 else p.quantity_ordered
        rate = p.mes_rate_per_unit if (p.mes_rate_per_unit and p.mes_rate_per_unit > 0) else (p.rate_per_unit or Decimal('0.00'))
        line_total = Decimal(qty) * rate

        total_qty += qty
        subtotal += line_total

        hsn = _get_hsn_code(p)
        uom = _get_uom(p.product_name).upper()

        desc_text = f"<b>{p.product_name}</b>"
        if p.remarks:
            desc_text += f"<br/>{p.remarks}"

        row = [
            Paragraph(str(idx), center_align),
            Paragraph(dpr.serial_number or "", center_align),
            Paragraph(desc_text, normal),
            Paragraph(hsn, center_align),
            Paragraph(str(qty), center_align),
            Paragraph(uom, center_align),
            Paragraph(f"Rs. {rate:,.2f}", right_align),
            Paragraph(f"<b>Rs. {line_total:,.2f}</b>", right_align),
        ]
        prod_table_data.append(row)

    # State code based GST calculation:
    # Tamil Nadu (State Code 33 / TN): CGST 9%, SGST 9%, IGST 0%
    # All other states (e.g. 03, 27, 29, etc.): IGST 18%, CGST 0%, SGST 0%
    state_code_clean = (customer.state_code or '').strip().upper() if customer else ''
    gstin_clean = (customer.gstin or '').strip().upper() if customer else ''
    region_clean = (customer.region or '').strip().lower() if customer else ''

    if state_code_clean:
        is_tamilnadu = (state_code_clean == '33' or state_code_clean in ('TN', 'TAMIL NADU', 'TAMILNADU'))
    elif gstin_clean and len(gstin_clean) >= 2 and gstin_clean[:2].isdigit():
        is_tamilnadu = (gstin_clean[:2] == '33')
    else:
        is_tamilnadu = (region_clean in ('chennai', 'hosur', 'tamil nadu', 'tamilnadu') or 'tamil' in region_clean)

    if is_tamilnadu:
        sgst_rate = Decimal('9.00')
        cgst_rate = Decimal('9.00')
        sgst_amt = (subtotal * sgst_rate / Decimal('100')).quantize(Decimal('0.01'))
        cgst_amt = (subtotal * cgst_rate / Decimal('100')).quantize(Decimal('0.01'))
        igst_amt = Decimal('0.00')
        tax_total = sgst_amt + cgst_amt

        tax_rows = [
            ["", "", Paragraph("Sub Total", right_bold), "", "", "", "", Paragraph(f"<b>Rs. {subtotal:,.2f}</b>", right_bold)],
            ["", "", Paragraph("SGST", right_align), Paragraph("9%", center_align), "", "", "", Paragraph(f"Rs. {sgst_amt:,.2f}", right_align)],
            ["", "", Paragraph("CGST", right_align), Paragraph("9%", center_align), "", "", "", Paragraph(f"Rs. {cgst_amt:,.2f}", right_align)],
            ["", "", Paragraph("IGST", right_align), Paragraph("0%", center_align), "", "", "", Paragraph("Rs. 0.00", right_align)],
            ["", "", Paragraph("P & F", right_align), Paragraph("0%", center_align), "", "", "", Paragraph("Rs. 0.00", right_align)],
            ["", "", Paragraph("R.OF", right_align), "", "", "", "", Paragraph("Rs. 0.00", right_align)],
        ]
    else:
        igst_rate = Decimal('18.00')
        igst_amt = (subtotal * igst_rate / Decimal('100')).quantize(Decimal('0.01'))
        sgst_amt = Decimal('0.00')
        cgst_amt = Decimal('0.00')
        tax_total = igst_amt

        tax_rows = [
            ["", "", Paragraph("Sub Total", right_bold), "", "", "", "", Paragraph(f"<b>Rs. {subtotal:,.2f}</b>", right_bold)],
            ["", "", Paragraph("SGST", right_align), Paragraph("0%", center_align), "", "", "", Paragraph("Rs. 0.00", right_align)],
            ["", "", Paragraph("CGST", right_align), Paragraph("0%", center_align), "", "", "", Paragraph("Rs. 0.00", right_align)],
            ["", "", Paragraph("IGST", right_align), Paragraph("18%", center_align), "", "", "", Paragraph(f"Rs. {igst_amt:,.2f}", right_align)],
            ["", "", Paragraph("P & F", right_align), Paragraph("0%", center_align), "", "", "", Paragraph("Rs. 0.00", right_align)],
            ["", "", Paragraph("R.OF", right_align), "", "", "", "", Paragraph("Rs. 0.00", right_align)],
        ]

    grand_total = subtotal + tax_total
    prod_table_data.extend(tax_rows)

    prod_table_data.append([
        "", "", Paragraph("<b>Total</b>", right_bold), "",
        Paragraph(f"<b>{total_qty}</b>", center_bold), "", "",
        Paragraph(f"<b>Rs. {grand_total:,.2f}</b>", right_bold)
    ])

    col_widths = [10 * mm, 26 * mm, 68 * mm, 18 * mm, 10 * mm, 12 * mm, 22 * mm, 24 * mm]

    prod_table = Table(prod_table_data, colWidths=col_widths)
    t_style = [
        ('BOX', (0,0), (-1,-1), 1, colors.black),
        ('INNERGRID', (0,0), (-1,-1), 0.5, colors.black),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 2),
        ('RIGHTPADDING', (0,0), (-1,-1), 2),
    ]
    prod_table.setStyle(TableStyle(t_style))
    story.append(prod_table)

    # Amount Chargable Box
    raw_words = _number_to_words(grand_total).upper().strip()
    raw_words = re.sub(r'\bONLY\.?$', '', raw_words, flags=re.IGNORECASE).strip()
    if not raw_words.endswith('RUPEES') and not raw_words.endswith('RUPPES'):
        raw_words += ' RUPEES'
    amount_words_str = f"{raw_words} ONLY."

    amount_words_p = Paragraph(
        f"<b>Amount Chargable (IN Words) : {amount_words_str}</b>",
        bold
    )

    amount_words_table = Table([[amount_words_p]], colWidths=[190 * mm])
    amount_words_table.setStyle(TableStyle([
        ('BOX', (0,0), (-1,-1), 1, colors.black),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(amount_words_table)

    # Bank Details Table (Left side)
    bank_table_data = [
        [Paragraph("Our Bank", normal), Paragraph(": Indian Bank", bold)],
        [Paragraph("Branch", normal), Paragraph(": Bangalore Road", bold)],
        [Paragraph("Account Number", normal), Paragraph(": 6706325980", bold)],
        [Paragraph("IFSC Code", normal), Paragraph(": IDIB000B142", bold)],
        [Paragraph("<br/><b>Declaration :</b><br/>We declare that this invoice shows the actual price of the goods described and all particulars are goods and correct.", normal), ""],
    ]

    bank_table = Table(bank_table_data, colWidths=[28 * mm, 67 * mm])
    bank_table.setStyle(TableStyle([
        ('SPAN', (0, 4), (1, 4)),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 1),
        ('RIGHTPADDING', (0, 0), (-1, -1), 1),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
    ]))

    # Right side: Signatory box
    sign_cell_content = [
        Paragraph("For Metrology Enginnering Solutions", right_align),
        Spacer(1, 18 * mm),
        Paragraph("Authorized Signatory", right_bold),
    ]

    sign_box = Table([[sign_cell_content]], colWidths=[95 * mm])
    sign_box.setStyle(TableStyle([
        ('BOX', (0, 0), (-1, -1), 0.8, colors.black),
        ('ALIGN', (0, 0), (-1, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('LEFTPADDING', (0, 0), (-1, -1), 3),
        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
    ]))

    footer_master_data = [
        [bank_table, sign_box]
    ]

    footer_master = Table(footer_master_data, colWidths=[95 * mm, 95 * mm])
    footer_master.setStyle(TableStyle([
        ('VALIGN', (0, 0), (0, 0), 'TOP'),
        ('VALIGN', (1, 0), (1, 0), 'BOTTOM'),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('BOX', (0, 0), (-1, -1), 1, colors.black),
    ]))

    story.append(KeepTogether([footer_master]))

    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer


@role_required('ADMIN', 'PURCHASE', 'SALES', 'ACCOUNTS')
def customer_invoice_modal_data(request, product_id):
    """Return JSON data for the Generate Invoice modal popup showing only the selected product."""
    from products.models import CustomerProduct
    try:
        target_product = CustomerProduct.objects.select_related('dpr', 'dpr__customer').get(pk=product_id)
    except CustomerProduct.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Product not found'}, status=404)

    dpr = target_product.dpr
    all_dpr_products = list(CustomerProduct.objects.filter(dpr=dpr))
    all_dpr_products_count = len(all_dpr_products)

    remaining_qty = max(target_product.quantity_ordered - (target_product.quantity_delivered or 0), 0)
    data = [{
        'id': target_product.id,
        'product_name': target_product.product_name,
        'quantity_ordered': target_product.quantity_ordered,
        'quantity_delivered': target_product.quantity_delivered or 0,
        'remaining_qty': remaining_qty,
        'invoice_qty': remaining_qty,
        'status': target_product.status or '',
    }]

    return JsonResponse({
        'status': 'success',
        'dpr_serial': dpr.serial_number,
        'customer_name': dpr.customer.customer_name if dpr.customer else '',
        'total_dpr_products_count': all_dpr_products_count,
        'products': data,
    })


@role_required('ADMIN', 'PURCHASE', 'SALES', 'ACCOUNTS')
def generate_customer_invoice(request, product_id, invoice_id=None):
    """Generate a Tax Invoice PDF for a customer product / DPR order."""
    from products.models import CustomerProduct, CustomerInvoice
    try:
        target_product = CustomerProduct.objects.select_related('dpr').get(pk=product_id)
    except CustomerProduct.DoesNotExist:
        raise Http404("Product not found")

    dpr = target_product.dpr
    all_products = list(CustomerProduct.objects.filter(dpr=dpr))

    selected_product_ids = None
    custom_qtys = {}
    new_invoice_id = invoice_id

    if request.method == 'POST':
        raw_selected = request.POST.getlist('selected_products')
        if raw_selected:
            selected_product_ids = [int(pid) for pid in raw_selected if str(pid).isdigit()]

            for pid in selected_product_ids:
                raw_q = request.POST.get(f'qty_{pid}', '').strip()
                if raw_q and raw_q.isdigit():
                    custom_qtys[pid] = int(raw_q)

            # Update delivered quantity and status for selected items
            for p in all_products:
                if p.id in selected_product_ids:
                    rem_qty = max(p.quantity_ordered - (p.quantity_delivered or 0), 0)
                    raw_invoiced_qty = custom_qtys.get(p.id, p.quantity_ordered)
                    invoiced_qty = min(raw_invoiced_qty, rem_qty) if rem_qty > 0 else raw_invoiced_qty
                    p.quantity_delivered = min((p.quantity_delivered or 0) + invoiced_qty, p.quantity_ordered)

                    if p.quantity_delivered >= p.quantity_ordered and p.quantity_delivered > 0:
                        p.status = 'delivered'
                    elif p.quantity_delivered > 0:
                        p.status = 'partially_delivered'
                    p.save()

                    existing_invoices = list(p.invoices.all().order_by('id'))
                    count = len(existing_invoices) + 1

                    inv_no_val = (p.invoice_dc_number or '').strip()
                    match = re.search(r'(\d+)', inv_no_val) if inv_no_val else None
                    if match:
                        base_no = f"MES-F{int(match.group(1)):04d}"
                    elif inv_no_val:
                        base_no = inv_no_val
                    else:
                        base_no = f"MES-F{p.id:04d}"

                    if count == 1 and p.quantity_delivered >= p.quantity_ordered:
                        inv_no = base_no
                    else:
                        inv_no = f"{base_no}-{count}"

                    if count == 2 and len(existing_invoices) == 1 and existing_invoices[0].invoice_number == base_no:
                        existing_invoices[0].invoice_number = f"{base_no}-1"
                        existing_invoices[0].save(update_fields=['invoice_number'])

                    created_inv = CustomerInvoice.objects.create(
                        customer_product=p,
                        invoice_number=inv_no,
                        quantity=invoiced_qty,
                        invoice_date=timezone.localdate()
                    )
                    if p.id == target_product.id:
                        new_invoice_id = created_inv.id

    pdf_buffer = _build_customer_invoice_pdf(product_id, invoice_id=new_invoice_id, selected_product_ids=selected_product_ids, custom_qtys=custom_qtys)
    if not pdf_buffer:
        raise Http404("Product not found")

    inv_filename = f"Tax_Invoice_{product_id}.pdf"
    if new_invoice_id:
        try:
            inv_obj = CustomerInvoice.objects.get(pk=new_invoice_id)
            safe_inv_no = re.sub(r'[^a-zA-Z0-9_\-]', '_', inv_obj.invoice_number)
            inv_filename = f"Tax_Invoice_{safe_inv_no}.pdf"
        except CustomerInvoice.DoesNotExist:
            pass
    else:
        inv_no_val = (target_product.invoice_dc_number or '').strip()
        match = re.search(r'(\d+)', inv_no_val) if inv_no_val else None
        if match:
            inv_no = f"MES-F{int(match.group(1)):04d}"
        elif inv_no_val:
            inv_no = inv_no_val
        else:
            inv_no = f"MES-F{target_product.id:04d}"
        safe_inv_no = re.sub(r'[^a-zA-Z0-9_\-]', '_', inv_no)
        inv_filename = f"Tax_Invoice_{safe_inv_no}.pdf"

    response = HttpResponse(pdf_buffer.getvalue(), content_type='application/pdf')
    response['Content-Disposition'] = f'inline; filename="{inv_filename}"'
    return response


def _build_customer_outstanding_email_html(customer, items, custom_body=None):
    """Build formatted HTML email body with outstanding payment reminder text and items table."""
    table_rows = []
    total_received = Decimal('0.00')
    total_outstanding = Decimal('0.00')

    for item in items:
        inv_date = item.invoice_date or item.dpr.po_date or (item.dpr.created_at.date() if item.dpr.created_at else None)
        if inv_date:
            date_str = f"{inv_date.day:02d}-{inv_date.month:02d}-{inv_date.year}"
        else:
            date_str = "-"

        inv_no_val = (item.invoice_dc_number or '').strip()
        match = re.search(r'(\d+)', inv_no_val) if inv_no_val else None
        if match:
            inv_no = f"MES-F{int(match.group(1)):04d}"
        elif inv_no_val:
            inv_no = inv_no_val
        else:
            inv_no = f"MES-F{item.id:04d}"

        cust_name = customer.customer_name if customer else "-"
        cust_po = (item.dpr.po_number or '').strip() if item.dpr else ''
        if not cust_po:
            cust_po = "DIRECT INVOICE"

        desc = (item.product_type or item.product_name or "-").strip()

        po_val = item.value or Decimal('0.00')
        rec_val = item.received_amount or Decimal('0.00')
        out_val = max(po_val - rec_val, Decimal('0.00'))

        total_received += rec_val
        total_outstanding += out_val

        rec_display = f"{rec_val:.2f}" if rec_val > 0 else ""
        out_display = f"{out_val:.2f}"

        table_rows.append(f"""
        <tr>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: center; vertical-align: middle;">{date_str}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: center; font-weight: bold; vertical-align: middle;">{inv_no}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: center; vertical-align: middle;">{cust_name}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: center; vertical-align: middle;">{cust_po}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: center; vertical-align: middle;">{desc}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: right; vertical-align: middle;">{rec_display}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: right; font-weight: bold; vertical-align: middle;">{out_display}</td>
        </tr>
        """)

    rows_html = "\n".join(table_rows)

    if custom_body and custom_body.strip():
        paragraphs = [p.strip().replace('\n', '<br>') for p in custom_body.strip().split('\n\n') if p.strip()]
        body_html = "".join(f'<p style="margin-bottom: 15px;">{p}</p>' for p in paragraphs)
    else:
        body_html = """
  <p style="margin-bottom: 15px;">Dear Sir/Madam,</p>
  <p style="margin-bottom: 15px;">We would like to bring to your kind attention that the payments for the invoices listed below are currently outstanding.</p>
  <p style="margin-bottom: 20px;">We kindly request you to review the pending invoices and arrange for the payment at your earliest convenience. Timely settlement of the outstanding amount will help us continue providing uninterrupted support and services.</p>
"""

    html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
</head>
<body style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #111111; margin: 0; padding: 15px;">
  {body_html}
  
  <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; border: 1px solid #000000; font-family: Arial, sans-serif; font-size: 13px; margin: 20px 0;">
    <thead>
      <tr style="background-color: #000000; color: #ffffff;">
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">DATE</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">INVOICE NO</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">CUSTOMER</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">CUSTOMER PO</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">DESCRIPTION</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">PAY RECEIVED</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">OUTSTANDING</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
  
  <p style="margin-top: 25px; margin-bottom: 5px;">Thanks & Regards,</p>
  <p style="margin-top: 0; font-weight: bold; color: #111111;">Metrology Engineering Solutions<br>
  <span style="font-weight: normal; color: #555555; font-size: 13px;">Hosur, Tamil Nadu</span></p>
</body>
</html>"""
    return html_content, total_outstanding


@role_required('ADMIN', 'PURCHASE', 'SALES', 'ACCOUNTS')
def send_invoice_email(request):
    """Send outstanding payment reminder email with formatted table in email body separately to each selected customer."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request method.'}, status=400)

    raw_product_ids = request.POST.getlist('product_ids[]') or request.POST.getlist('product_ids')
    if not raw_product_ids:
        raw_single = request.POST.get('product_ids', '').strip()
        if raw_single:
            raw_product_ids = [pid.strip() for pid in raw_single.split(',') if pid.strip()]

    product_ids = [int(pid) for pid in raw_product_ids if str(pid).isdigit()]
    if not product_ids:
        return JsonResponse({'status': 'error', 'message': 'No invoices selected.'}, status=400)

    recipient_email = request.POST.get('recipient_email', '').strip()
    subject = request.POST.get('email_subject', '').strip()
    body = request.POST.get('email_body', '').strip()
    extra_attachment = request.FILES.get('extra_attachment')

    from products.models import CustomerProduct
    products = list(CustomerProduct.objects.select_related('dpr', 'dpr__customer').filter(id__in=product_ids))
    if not products:
        return JsonResponse({'status': 'error', 'message': 'Selected invoice records not found.'}, status=404)

    # Group products by customer
    from collections import defaultdict
    import threading
    import logging

    customer_groups = defaultdict(list)
    for p in products:
        cust = p.dpr.customer
        if cust:
            customer_groups[cust].append(p)

    sent_details = []
    missing_email_customers = []
    messages_to_send = []

    attachment_data = None
    if extra_attachment:
        try:
            attachment_data = (
                extra_attachment.name,
                extra_attachment.read(),
                getattr(extra_attachment, 'content_type', None) or 'application/octet-stream'
            )
        except Exception:
            attachment_data = None

    for cust, items in customer_groups.items():
        cust_email = (cust.email or '').strip()
        override_email = request.POST.get(f'email_{cust.id}', '').strip()
        if override_email:
            cust_email = override_email

        if not cust_email or len(customer_groups) == 1:
            if recipient_email:
                cust_email = recipient_email

        if not cust_email:
            missing_email_customers.append(cust.customer_name)
            continue

        to_emails = [email.strip() for email in re.split(r'[;,]', cust_email) if email.strip()]
        valid_to_emails = []
        for em in to_emails:
            try:
                validate_email(em)
                valid_to_emails.append(em)
            except ValidationError:
                pass

        if not valid_to_emails:
            missing_email_customers.append(f"{cust.customer_name} (Invalid email: {cust_email})")
            continue

        html_body, total_out = _build_customer_outstanding_email_html(cust, items, custom_body=body)
        email_subject = subject or f"Outstanding Payment Reminder - {cust.customer_name} - Metrology Engineering Solutions"

        email = EmailMessage(
            subject=email_subject,
            body=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=valid_to_emails,
        )
        email.content_subtype = "html"

        if attachment_data:
            email.attach(*attachment_data)

        messages_to_send.append(email)
        sent_details.append(f"{cust.customer_name} ({', '.join(valid_to_emails)})")

    if not messages_to_send and missing_email_customers:
        return JsonResponse({
            'status': 'error',
            'message': f"Could not send email. Missing or invalid email address for: {', '.join(missing_email_customers)}. Please enter a valid recipient email."
        }, status=400)

    def _send_emails_worker(msg_list):
        try:
            from django.core.mail import get_connection
            conn = get_connection(timeout=20)
            conn.open()
            conn.send_messages(msg_list)
            conn.close()
        except Exception as exc:
            logging.getLogger(__name__).exception("Error in background email worker: %s", exc)

    if messages_to_send:
        bg_thread = threading.Thread(target=_send_emails_worker, args=(messages_to_send,))
        bg_thread.daemon = True
        bg_thread.start()

    msg_parts = []
    if sent_details:
        msg_parts.append(f"Outstanding payment reminder email sent successfully to: {', '.join(sent_details)}.")
    if missing_email_customers:
        msg_parts.append(f"Skipped customers without email: {', '.join(missing_email_customers)}.")

    return JsonResponse({
        'status': 'ok' if sent_details else 'error',
        'message': " ".join(msg_parts),
        'sent_count': len(sent_details),
    })


def _build_supplier_outstanding_email_html(supplier, items, custom_body=None):
    total_received = Decimal('0.00')
    total_outstanding = Decimal('0.00')

    table_rows = []
    for item in items:
        inv_date = item.invoice_date or item.po_date
        if inv_date:
            date_str = inv_date.strftime('%d-%m-%Y')
        else:
            date_str = "-"

        inv_no = (item.po_number or item.invoice_dc_number or f"SUP-PO{item.id:04d}").strip()
        supp_name = supplier.supplier_name if supplier else "-"
        supp_po = (item.po_number or '').strip()
        if not supp_po:
            supp_po = "DIRECT PO"

        desc = "-"
        if item.customer_product:
            desc = (item.customer_product.product_name or item.customer_product.product_type or "-").strip()

        po_val = item.po_value or Decimal('0.00')
        rec_val = item.received_amount or Decimal('0.00')
        out_val = max(po_val - rec_val, Decimal('0.00'))

        total_received += rec_val
        total_outstanding += out_val

        rec_display = f"{rec_val:.2f}" if rec_val > 0 else ""
        out_display = f"{out_val:.2f}"

        table_rows.append(f"""
        <tr>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: center; vertical-align: middle;">{date_str}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: center; font-weight: bold; vertical-align: middle;">{inv_no}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: center; vertical-align: middle;">{supp_name}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: center; vertical-align: middle;">{supp_po}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: center; vertical-align: middle;">{desc}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: right; vertical-align: middle;">{rec_display}</td>
          <td style="border: 1px solid #000000; padding: 8px 10px; text-align: right; font-weight: bold; vertical-align: middle;">{out_display}</td>
        </tr>
        """)

    rows_html = "\n".join(table_rows)

    if custom_body and custom_body.strip():
        paragraphs = [p.strip().replace('\n', '<br>') for p in custom_body.strip().split('\n\n') if p.strip()]
        body_html = "".join(f'<p style="margin-bottom: 15px;">{p}</p>' for p in paragraphs)
    else:
        body_html = """
  <p style="margin-bottom: 15px;">Dear Sir/Madam,</p>
  <p style="margin-bottom: 15px;">Please find below the payment details and status for purchase orders with your company.</p>
  <p style="margin-bottom: 20px;">Please review the payment details and feel free to reach out if you have any questions.</p>
"""

    html_content = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
</head>
<body style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #111111; margin: 0; padding: 15px;">
  {body_html}
  
  <table border="1" cellpadding="8" cellspacing="0" style="border-collapse: collapse; width: 100%; border: 1px solid #000000; font-family: Arial, sans-serif; font-size: 13px; margin: 20px 0;">
    <thead>
      <tr style="background-color: #000000; color: #ffffff;">
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">DATE</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">PO / INVOICE NO</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">SUPPLIER</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">SUPPLIER PO</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">DESCRIPTION</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">AMOUNT PAID</th>
        <th style="border: 1px solid #000000; padding: 10px 8px; text-align: center; font-weight: bold; background-color: #000000; color: #ffffff;">OUTSTANDING</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
  
  <p style="margin-top: 25px; margin-bottom: 5px;">Thanks & Regards,</p>
  <p style="margin-top: 0; font-weight: bold; color: #111111;">Metrology Engineering Solutions<br>
  <span style="font-weight: normal; color: #555555; font-size: 13px;">Hosur, Tamil Nadu</span></p>
</body>
</html>"""
    return html_content, total_outstanding


@role_required('ADMIN', 'PURCHASE', 'ACCOUNTS')
def send_supplier_invoice_email(request):
    """Send payment status statement email with formatted table in email body separately to each selected supplier."""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request method.'}, status=400)

    raw_product_ids = request.POST.getlist('product_ids[]') or request.POST.getlist('product_ids')
    if not raw_product_ids:
        raw_single = request.POST.get('product_ids', '').strip()
        if raw_single:
            raw_product_ids = [pid.strip() for pid in raw_single.split(',') if pid.strip()]

    product_ids = [int(pid) for pid in raw_product_ids if str(pid).isdigit()]
    if not product_ids:
        return JsonResponse({'status': 'error', 'message': 'No records selected.'}, status=400)

    recipient_email = request.POST.get('recipient_email', '').strip()
    subject = request.POST.get('email_subject', '').strip()
    body = request.POST.get('email_body', '').strip()
    extra_attachment = request.FILES.get('extra_attachment')

    products = list(SupplierProduct.objects.select_related('supplier', 'customer_product', 'customer_product__dpr').filter(id__in=product_ids))
    if not products:
        return JsonResponse({'status': 'error', 'message': 'Selected supplier records not found.'}, status=404)

    from collections import defaultdict
    import threading
    import logging

    supplier_groups = defaultdict(list)
    for p in products:
        supp = p.supplier
        if supp:
            supplier_groups[supp].append(p)

    sent_details = []
    missing_email_suppliers = []
    messages_to_send = []

    attachment_data = None
    if extra_attachment:
        try:
            attachment_data = (
                extra_attachment.name,
                extra_attachment.read(),
                getattr(extra_attachment, 'content_type', None) or 'application/octet-stream'
            )
        except Exception:
            attachment_data = None

    for supp, items in supplier_groups.items():
        supp_email = (supp.email or '').strip()
        override_email = request.POST.get(f'email_{supp.id}', '').strip()
        if override_email:
            supp_email = override_email

        if not supp_email or len(supplier_groups) == 1:
            if recipient_email:
                supp_email = recipient_email

        if not supp_email:
            missing_email_suppliers.append(supp.supplier_name)
            continue

        to_emails = [email.strip() for email in re.split(r'[;,]', supp_email) if email.strip()]
        valid_to_emails = []
        for em in to_emails:
            try:
                validate_email(em)
                valid_to_emails.append(em)
            except ValidationError:
                pass

        if not valid_to_emails:
            missing_email_suppliers.append(f"{supp.supplier_name} (Invalid email: {supp_email})")
            continue

        html_body, total_out = _build_supplier_outstanding_email_html(supp, items, custom_body=body)
        email_subject = subject or f"Supplier Payment Statement - {supp.supplier_name} - Metrology Engineering Solutions"

        email = EmailMessage(
            subject=email_subject,
            body=html_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=valid_to_emails,
        )
        email.content_subtype = "html"

        if attachment_data:
            email.attach(*attachment_data)

        messages_to_send.append(email)
        sent_details.append(f"{supp.supplier_name} ({', '.join(valid_to_emails)})")

    if not messages_to_send and missing_email_suppliers:
        return JsonResponse({
            'status': 'error',
            'message': f"Could not send email. Missing or invalid email address for: {', '.join(missing_email_suppliers)}. Please enter a valid recipient email."
        }, status=400)

    def _send_emails_worker(msg_list):
        try:
            from django.core.mail import get_connection
            conn = get_connection(timeout=20)
            conn.open()
            conn.send_messages(msg_list)
            conn.close()
        except Exception as exc:
            logging.getLogger(__name__).exception("Error in background supplier email worker: %s", exc)

    if messages_to_send:
        bg_thread = threading.Thread(target=_send_emails_worker, args=(messages_to_send,))
        bg_thread.daemon = True
        bg_thread.start()

    msg_parts = []
    if sent_details:
        msg_parts.append(f"Supplier payment statement email sent successfully to: {', '.join(sent_details)}.")
    if missing_email_suppliers:
        msg_parts.append(f"Skipped suppliers without email: {', '.join(missing_email_suppliers)}.")

    return JsonResponse({
        'status': 'ok' if sent_details else 'error',
        'message': " ".join(msg_parts),
        'sent_count': len(sent_details),
    })


