import re
from .base import BaseEmailClassifier


class RuleBasedEmailClassifier(BaseEmailClassifier):
    """Business email classifier with comprehensive manufacturing keyword analysis."""

    NON_CUSTOMER_SENDERS = (
        '@facebookmail.com', '@mail.instagram.com', '@alerts.actcorp.in',
        '@mail.sap.com', '@meshmixmedia.com', '@linkedin.com', '@twitter.com'
    )

    def classify(self, subject, body, sender=''):
        subject_str = subject or ''
        body_str = body or ''
        sender_str = sender or ''

        text = f'{subject_str} {body_str}'.lower()
        subject_lower = subject_str.lower()
        sender_lower = sender_str.lower()

        # Check non-customer / promotional senders
        if any(domain in sender_lower for domain in self.NON_CUSTOMER_SENDERS):
            return {
                'category': 'OTHERS',
                'confidence': 0.90,
                'reason': 'Automated or promotional sender.',
                'important_details': '',
            }

        # 1. Purchase Order / Customer Order
        po_keywords = [
            'purchase order', 'po#', 'po number', 'po no', 'po copy', 'po attached', 'attached po',
            'supply order', 'open order', 'order confirmation', 'purchase requisition',
            'our po', 'customer po', 'order no', 'order number', 'placed order',
            'new order', 'firm order', 'official order', 'work order', 'release order', 'sales order',
            'por-', 'po -', 'po-'
        ]
        po_matches = [k for k in po_keywords if k in text]
        if re.search(r'(?:^|[\s_\-\(])po(?:[\s_\-\:\#\d\)]|$)', subject_lower) or re.search(r'\bpo\s*[-#:]?\s*\d+', text) or re.search(r'\bp\.o\.\b', text):
            if 'po' not in po_matches:
                po_matches.append('po')

        # 2. Dispatch
        dispatch_keywords = [
            'dispatch', 'dispatched', 'dispatch details', 'dispatch pending', 'dispatch status',
            'delivery status', 'consignment', 'tracking number', 'lr copy', 'lr number',
            'eway bill', 'e-way bill', 'docket', 'shipping details', 'shipment status', 'shipment',
            'pickup details', 'courier', 'courier details', 'goods receipt', 'not yet received',
            'kindly dispatch', 'material dispatch', 'goods dispatch', 'awb', 'transporter',
            'delivery challan', 'dispatch schedule', 'ready for dispatch', 'despatch', 'despatched'
        ]
        dispatch_matches = [k for k in dispatch_keywords if k in text]

        # 3. Payment / Invoice
        payment_keywords = [
            'payment', 'invoice', 'payment advice', 'payment due', 'payment details',
            'payment confirmation', 'payment status', 'payment received', 'remittance',
            'receipt', 'unpaid', 'utr', 'neft', 'rtgs', 'bank transfer', 'wire transfer',
            'tax invoice', 'proforma invoice', 'commercial invoice', 'bill copy', 'payment proof',
            'transaction details', 'amount transferred', 'amount paid', 'cheque', 'chq',
            'credit note', 'debit note', 'payment receipt', 'outstanding payment', 'payment follow up'
        ]
        payment_matches = [k for k in payment_keywords if k in text]

        # 4. Support / Complaint
        support_keywords = [
            'complaint', 'not working', 'not work', 'defective', 'breakdown', 'service issue',
            'calibration issue', 'damaged', 'faulty', 'rework required', 'error in gauge',
            'out of tolerance', 'rejection', 'rejected material', 'quality issue', 'customer complaint'
        ]
        support_matches = [k for k in support_keywords if k in text]

        # 5. Quotation Request / Enquiry
        quote_keywords = [
            'request for quotation', 'request for quote', 'rfq', 'please quote', 'send quotation',
            'quotation', 'enquiry', 'inquiry', 'price for', 'price details', 'product details',
            'need price', 'cost details', 'air plug', 'air ring', 'thread plug', 'thread ring',
            'plain plug', 'plain ring', 'pin gauge', 'multi-gauge', 'multi gauge', 'spc', 'lvdt',
            'air unit', 'comparator', 'gauge requirement', 'gauge quotation', 'quote requirement',
            'give your best quote', 'provide your quote', 'quote request', 'wearcheck'
        ]
        quote_matches = [k for k in quote_keywords if k in text]

        # Subject-based High Priority Matches
        if any(k in subject_lower for k in ('payment', 'invoice', 'utr', 'neft', 'rtgs', 'remittance', 'receipt')):
            return {
                'category': 'PAYMENT_INVOICE',
                'confidence': min(0.75 + 0.05 * len(payment_matches), 0.95),
                'reason': 'Subject contains payment/invoice terms.',
                'important_details': f"Matched: {', '.join(payment_matches)}" if payment_matches else 'Subject matched payment',
            }

        if any(k in subject_lower for k in ('dispatch', 'delivery status', 'consignment', 'eway bill', 'lr copy', 'tracking', 'despatch', 'shipment', 'courier', 'pickup details')):
            return {
                'category': 'DISPATCH',
                'confidence': min(0.75 + 0.05 * len(dispatch_matches), 0.95),
                'reason': 'Subject contains dispatch/shipment tracking terms.',
                'important_details': f"Matched: {', '.join(dispatch_matches)}" if dispatch_matches else 'Subject matched dispatch',
            }

        if any(k in subject_lower for k in ('purchase order', 'po#', 'po number', 'po no', 'supply order', 'order confirmation', 'work order', 'purchase requisition')) or re.search(r'(?:^|[\s_\-\(])po(?:[\s_\-\:\#\d\)]|$)', subject_lower):
            return {
                'category': 'CUSTOMER_ORDER',
                'confidence': min(0.75 + 0.05 * len(po_matches), 0.95),
                'reason': 'Subject contains purchase order terms.',
                'important_details': f"Matched: {', '.join(po_matches)}" if po_matches else 'Subject matched purchase order',
            }

        if any(k in subject_lower for k in ('rfq', 'quotation', 'enquiry', 'inquiry', 'quote')):
            return {
                'category': 'QUOTATION_REQUEST',
                'confidence': min(0.75 + 0.05 * len(quote_matches), 0.95),
                'reason': 'Subject contains quotation/enquiry request terms.',
                'important_details': f"Matched: {', '.join(quote_matches)}" if quote_matches else 'Subject matched quotation request',
            }

        # Content/Body Matches
        if po_matches:
            return {
                'category': 'CUSTOMER_ORDER',
                'confidence': min(0.70 + 0.05 * len(po_matches), 0.90),
                'reason': 'Contains purchase-order language.',
                'important_details': f"Matched: {', '.join(po_matches)}",
            }

        if dispatch_matches:
            return {
                'category': 'DISPATCH',
                'confidence': min(0.70 + 0.05 * len(dispatch_matches), 0.90),
                'reason': 'Contains dispatch or delivery tracking language.',
                'important_details': f"Matched: {', '.join(dispatch_matches)}",
            }

        if payment_matches:
            return {
                'category': 'PAYMENT_INVOICE',
                'confidence': min(0.70 + 0.05 * len(payment_matches), 0.90),
                'reason': 'Contains payment or invoice language.',
                'important_details': f"Matched: {', '.join(payment_matches)}",
            }

        if quote_matches:
            return {
                'category': 'QUOTATION_REQUEST',
                'confidence': min(0.70 + 0.05 * len(quote_matches), 0.90),
                'reason': 'Contains a quotation or enquiry request.',
                'important_details': f"Matched: {', '.join(quote_matches)}",
            }

        if support_matches:
            return {
                'category': 'SUPPORT_COMPLAINT',
                'confidence': min(0.70 + 0.05 * len(support_matches), 0.90),
                'reason': 'Contains support or complaint language.',
                'important_details': f"Matched: {', '.join(support_matches)}",
            }

        return {
            'category': 'OTHERS',
            'confidence': 0.40,
            'reason': 'No configured business-email keywords matched.',
            'important_details': '',
        }
