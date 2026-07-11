from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib import messages
from .models import CustomUser
from django.contrib.auth.decorators import login_required
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
from django.db.models import Sum, Case, When, Value, IntegerField
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



def _format_mes_quote_no(rfq, revision_number=0):
    base_quote_no = _get_mes_quote_no(rfq)
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
            'selected_supplier_name': getattr(product, 'selected_supplier_name', ''),
        })
    return serialized


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


def _create_rfq_quotation_record(rfq, products, product_ids, email_sent=False):
    latest = RFQQuotation.objects.filter(rfq=rfq).order_by('-revision_number').first()
    revision_number = 0 if latest is None else latest.revision_number + 1
    quotation = RFQQuotation.objects.create(
        rfq=rfq,
        quotation_number=_format_mes_quote_no(rfq, revision_number),
        revision_number=revision_number,
        products_snapshot=_serialize_quotation_products(products),
        email_sent=email_sent,
    )
    return quotation

def _build_selected_quotation_products(rfq, product_ids, supplier_price_ids):
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
        selected_prices = supplier_prices_by_product.get(product.id, [])
        if selected_prices:
            for supplier_price in selected_prices:
                quotation_products.append(SimpleNamespace(
                    id=product.id,
                    product_name=product.product_name,
                    product_type=product.product_type,
                    price_known=True,
                    quotation_email_sent=product.quotation_email_sent,
                    quantity=product.quantity,
                    rate_per_unit=supplier_price.price,
                    value=supplier_price.value,
                    remarks=product.remarks,
                    selected_supplier_name=supplier_price.supplier.supplier_name,
                ))
        else:
            product.selected_supplier_name = ''
            quotation_products.append(product)

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
                # Draw Logo Box
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
                self.drawCentredString(self._pagesize[0] / 2.0 + 10 * mm, self._pagesize[1] - 19 * mm, "L-732/1156, 1st Floor, Rayakottai Hudco,42nd Cross,")
                self.drawCentredString(self._pagesize[0] / 2.0 + 10 * mm, self._pagesize[1] - 22 * mm, "Phase 10, Hosur, Krishnagiri, Tamil Nadu, India-635109")
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
            
    # Check for GSTIN in address lines
    gstin_found = False
    for line in customer_address.split('\n'):
        if 'gst' in line.lower():
            gstin_found = True
            break
    if not gstin_found:
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
        Paragraph('<b>Part No</b>', centered),
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
        selected_supplier_name = getattr(product, 'selected_supplier_name', '')
        if selected_supplier_name:
            description_lines.append(f'Supplier: {pdf_text(selected_supplier_name)}')
        if product.remarks:
            description_lines.append(pdf_text(product.remarks).replace('\n', '<br/>'))
            
        hsn_code = _get_hsn_code(product)
        
        table_data.append([
            Paragraph(str(index), centered),
            Paragraph(pdf_text(product.product_type or 'P0011'), centered),
            Paragraph('<br/>'.join(description_lines), small),
            Paragraph(hsn_code, centered),
            Paragraph(str(product.quantity), centered),
            Paragraph('Set', centered),
            Paragraph(_format_money(product.rate_per_unit), right),
            Paragraph(_format_money(line_total), right),
        ])

    product_table = Table(
        table_data,
        colWidths=[11 * mm, 18 * mm, 72 * mm, 20 * mm, 14 * mm, 12 * mm, 17 * mm, 18 * mm],
        repeatRows=1,
        style=TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#a7d3ef')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('ALIGN', (6, 1), (7, -1), 'RIGHT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ])
    )
    story.append(product_table)
    story.append(Spacer(1, 8))

    packing = (subtotal * Decimal('0.02')).quantize(Decimal('0.01'))
    taxable = subtotal + packing
    gst = (taxable * Decimal('0.18')).quantize(Decimal('0.01'))
    grand_total = taxable + gst
    summary = Table(
        [
            [Paragraph('Basic Total', normal), Paragraph(_format_money(subtotal), right)],
            [Paragraph('Packing & Forwarding @ 2%', normal), Paragraph(_format_money(packing), right)],
            [Paragraph('GST @ 18%', normal), Paragraph(_format_money(gst), right)],
            [Paragraph('<b>Grand Total</b>', normal), Paragraph(f'<b>{_format_money(grand_total)}</b>', right)],
        ],
        colWidths=[55 * mm, 35 * mm],
        hAlign='RIGHT',
        style=TableStyle([
            ('GRID', (0, 0), (-1, -1), 0.5, colors.black),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#a7d3ef')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ])
    )
    story.append(summary)
    story.append(Spacer(1, 10))

    story.append(Paragraph('<b><u>Our Terms & Conditions</u></b>', terms_title_style))
    story.append(Spacer(1, 4))
    
    # Calculate quotation validity date (60 days from today)
    valid_till = (timezone.localdate() + timedelta(days=60)).strftime('%d/%m/%Y')
    
    terms = [
        'Delivery : 3 Weeks',
        'Payment : 30 Days Against Invoice',
        'Goods & Service Tax(GST) : 18% Extra as Applicable',
        'Dispatch Mode : By Courier',
        'Packing & Forwarding : 2%',
        'Installation Charge : -',
        'Discount : -',
        f'Quotation Validity : This offer is Valid till {valid_till}',
        'Purchase Order : Purchase Order must be send to info@mesinstruments.co.in',
        'Bank Details :<br/>Our Bank : Indian Bank, &nbsp;&nbsp;&nbsp;&nbsp; Branch : Bangalore Road,<br/>Acount Number: 6706325980 &nbsp;&nbsp;&nbsp;&nbsp; IFSC Code: IDIB000B142',
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
        'Dear {supplier_name},\n\n'
        'Please share your price for the following RFQ products.\n'
        'RFQ No: {rfq_no}\n'
        'Customer: {customer_name}\n'
        'Enquiry Details: {enquiry_details}\n\n'
        'Products:\n{products}\n\n'
        'Regards,\n'
        'Metrology Engineering Solutions'
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

    rfq_quotation_not_sent_count = RFQ.objects.filter(quotation_email_sent=False).count()

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
        'supplier_partial_count': supplier_partial_count,
        'supplier_cancelled_count': supplier_cancelled_count,
        'supplier_pending_count': supplier_pending_count,
        'customer_delivery_pct': customer_delivery_pct,
        'supplier_delivery_pct': supplier_delivery_pct,
        'mail_confirmation_pct': mail_confirmation_pct,
        'overall_delivery_pct': overall_delivery_pct,
        'supplier_order_pending_difference': supplier_order_pending_difference,
        'rfq_quotation_not_sent_count': rfq_quotation_not_sent_count,
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
        customer_product.delivery_detail_type = None
        customer_product.invoice_dc_number = None
        customer_product.invoice_dc_attachment = None
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
        'product_status': customer_product.status or ''
    })


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
        supplier_product.quantity_ok = max(
            supplier_product.quantity_received - supplier_product.quantity_not_ok,
            0
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

        supplier_product.quantity_received = received_quantity
        supplier_product.quantity_not_ok = not_ok_quantity
        supplier_product.not_ok_reason = not_ok_reason if not_ok_quantity > 0 else None
        supplier_product.save(update_fields=[
            'status',
            'quantity_received',
            'quantity_not_ok',
            'not_ok_reason'
        ])
        _sync_dpr_supplier_qty_received(supplier_product.customer_product.dpr)
        return JsonResponse({
            'status': 'ok',
            'inward_status': supplier_product.status or '',
            'quantity_received': supplier_product.quantity_received,
            'quantity_ok': ok_quantity,
            'quantity_not_ok': supplier_product.quantity_not_ok,
            'not_ok_reason': supplier_product.not_ok_reason or ''
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

    supplier_product.save(update_fields=[
        'status',
        'quantity_received',
        'quantity_not_ok',
        'not_ok_reason'
    ])
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
        rates = request.POST.getlist('rate_per_unit[]')
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
        rates = request.POST.getlist('rate_per_unit[]')
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

    customer_products = list(products)
    product_lookup = {
        (
            (product.product_name or '').strip().lower(),
            product.product_type or ''
        ): product.id
        for product in customer_products
    }
    rfq_rate_map = {}
    product_default_rate_map = {}
    rfq_supplier_prices = RFQSupplierPrice.objects.filter(
        product__rfq__customer=dpr.customer
    ).select_related('product', 'supplier').order_by('product__rfq__created_at')
    for supplier_price in rfq_supplier_prices:
        product_key = (
            (supplier_price.product.product_name or '').strip().lower(),
            supplier_price.product.product_type or ''
        )
        customer_product_id = product_lookup.get(product_key)
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
        product_key = (
            (rfq_product.product_name or '').strip().lower(),
            rfq_product.product_type or ''
        )
        customer_product_id = product_lookup.get(product_key)
        if not customer_product_id:
            continue
        # Known-price products override the supplier-price default
        product_default_rate_map[str(customer_product_id)] = str(rfq_product.rate_per_unit)
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
        rates = request.POST.getlist('rate_per_unit[]')
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
                rates[i] if i < len(rates) else '',
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

            SupplierProduct.objects.create(
                customer_product=customer_product,
                supplier=supplier,
                rate_per_unit=rate,
                po_value=po_value,
                po_date=po_dates[i] or None,
                po_validity=po_validities[i] or None,
                quantity=quantity,
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
        'rfq_rate_map': rfq_rate_map,
        'product_default_rate_map': product_default_rate_map,
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
        email = request.POST.get('email', '').strip()
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
            customer.email = email or None
            customer.phone_number = phone_number or None
            customer.address = address or None
            customer.save(update_fields=['customer_name', 'region', 'email', 'phone_number', 'address'])
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
        rates = request.POST.getlist('rate_per_unit[]')
        product_remarks = request.POST.getlist('product_remarks[]')
        supplier_email_to = request.POST.get('supplier_email_to', '').strip()
        supplier_email_cc = request.POST.get('supplier_email_cc', '').strip()
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
                email.strip()
                for email in re.split(r'[;,]', supplier_email_to)
                if email.strip()
            ]
            supplier_cc_emails = [
                email.strip()
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

                product_rows.append({
                    'id': product_id,
                    'product_name': product_name.strip(),
                    'product_type': product_type,
                    'price_known': price_known,
                    'supplier': suppliers_for_price[0] if suppliers_for_price else None,
                    'suppliers': suppliers_for_price,
                    'quantity': quantity,
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
                sent_count, failed_suppliers = _send_rfq_supplier_price_requests(
                    rfq,
                    product_rows,
                    supplier_email_subject,
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
                sent_count, failed_suppliers = _send_rfq_supplier_price_requests(
                    rfq,
                    product_rows,
                    supplier_email_subject,
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
                    if quotation_record is None:
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

    status_filter = request.GET.get('status')
    if status_filter == 'pending':
        rfqs = list(RFQ.objects.filter(quotation_email_sent=False).select_related('customer').prefetch_related('products__suppliers', 'products__supplier_prices__supplier').all())
    else:
        rfqs = list(RFQ.objects.select_related('customer').prefetch_related('products__suppliers', 'products__supplier_prices__supplier').all())
    today = timezone.localdate()
    for rfq in rfqs:
        rfq.row_class = _get_rfq_row_alert_class(rfq)
        rfq.is_overdue = (today - rfq.mail_date).days >= 3 if rfq.mail_date else False

    # Sort RFQs: red (table-danger) -> white ('') -> green (table-success)
    class_order = {
        'table-danger': 0,
        '': 1,
        'table-success': 2
    }
    rfqs.sort(key=lambda r: class_order.get(r.row_class, 1))

    rfq_payloads = []
    for rfq in rfqs:
        row_class = rfq.row_class
        rfq_payloads.append({
            'id': rfq.id,
            'rfq_no': rfq.rfq_no,
            'mail_date': rfq.mail_date.strftime('%Y-%m-%d') if rfq.mail_date else '',
            'customer_id': rfq.customer_id,
            'customer_name': rfq.customer.customer_name,
            'customer_email': rfq.customer.email or '',
            'enquiry_details': rfq.enquiry_details,
            'remarks': rfq.remarks or '',
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
        'rfqs': rfqs,
        'customers': customers,
        'suppliers': suppliers,
        'product_type_choices': CustomerProduct.PRODUCT_TYPE_CHOICES,
        'rfq_payloads': rfq_payloads,
        'default_supplier_email_subject': _get_default_supplier_email_subject(),
        'default_supplier_email_body': _get_default_supplier_email_body(),
        'status_filter': status_filter,
    })


@login_required
def rfq_quotation_download(request, rfq_id):
    if request.method != 'POST':
        raise Http404
    try:
        rfq = RFQ.objects.select_related('customer').get(pk=rfq_id)
    except RFQ.DoesNotExist:
        raise Http404

    product_ids = request.POST.getlist('product_ids')
    supplier_price_ids = request.POST.getlist('supplier_price_ids')
    products, quotation_product_ids_to_mark = _build_selected_quotation_products(rfq, product_ids, supplier_price_ids)
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
        RFQProduct.objects.filter(
            rfq=rfq,
            id__in=quotation_product_ids_to_mark
        ).update(quotation_prepared=True)
        rfq.quotation_prepared = True
        rfq.save(update_fields=['quotation_prepared'])
    response['Content-Disposition'] = f'{disposition}; filename="{filename}"'
    response.set_cookie('rfq_quotation_downloaded', 'true', path='/')
    return response


@login_required
def supplier_details(request):
    if request.method == 'POST':
        action = request.POST.get('action')
        supplier_id = request.POST.get('supplier_id')
        supplier_name = request.POST.get('supplier_name', '').strip()
        email = request.POST.get('email', '').strip()
        email = request.POST.get('email', '').strip()
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
                email=email or None,
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
            supplier.email = email or None
            supplier.phone_number = phone_number or None
            supplier.address = address or None
            supplier.save(update_fields=['supplier_name', 'email', 'phone_number', 'address'])
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
        rates = request.POST.getlist('rate_per_unit[]')
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
            'rate_per_unit[]'
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

@login_required
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

            address=address or None
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


@login_required
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


@login_required
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
                        'rfq_no': rfq.rfq_no,
                        'quotation_number': quotation.quotation_number,
                        'revision_number': quotation.revision_number,
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
                'rfq_no': rfq.rfq_no,
                'quotation_number': quote_no,
                'revision_number': 0,
                'status_label': 'Prepared - Email not sent' if has_prepared_not_emailed else 'Email sent',
                'prepared_not_emailed': has_prepared_not_emailed,
                'products': products_list,
            })

    return JsonResponse({'status': 'success', 'quotations': quotations})



