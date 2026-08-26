from .base import BaseEmailClassifier


class RuleBasedEmailClassifier(BaseEmailClassifier):
    """Free local fallback classifier based on business-email keywords."""

    NON_CUSTOMER_SENDERS = (
        '@mesinstruments.co.in', '@mesinstruments.com',
        '@mail.instagram.com', '@facebookmail.com', '@alerts.actcorp.in',
        '@egsindia.com', '@mail.sap.com', '@meshmixmedia.com'
    )

    rules = (
        ('CUSTOMER_ORDER', ('purchase order', 'po#', 'po number', 'po copy', 'supply order', 'open order', 'order confirmation', 'kindly dispatch'),
         'Contains purchase-order or dispatch language.'),
        ('PAYMENT_INVOICE', ('invoice', 'payment advice', 'payment due', 'remittance', 'receipt', 'unpaid'),
         'Contains invoice or payment language.'),
        ('SUPPORT_COMPLAINT', ('complaint', 'not working', 'not work', 'defective', 'breakdown', 'service issue'),
         'Contains support or complaint language.'),
        ('QUOTATION_REQUEST', ('request for quotation', 'request for quote', 'rfq', 'please quote', 'send quotation', 'enquiry', 'inquiry', 'price for', 'price details', 'product details', 'need price', 'air plug', 'air ring', 'thread plug', 'thread ring', 'plain plug', 'plain ring', 'pin gauge', 'multi-gauge', 'multi gauge', 'spc', 'lvdt', 'air unit', 'comparator', 'gauge requirement'),
         'Contains a quotation or enquiry request.'),
    )

    def classify(self, subject, body, sender=''):
        text = f'{subject} {body}'.lower()
        sender_lower = (sender or '').lower()
        is_non_customer = any(domain in sender_lower for domain in self.NON_CUSTOMER_SENDERS)

        for category, keywords, reason in self.rules:
            matches = [keyword for keyword in keywords if keyword in text]
            if matches:
                if category in ('QUOTATION_REQUEST', 'CUSTOMER_ORDER') and is_non_customer:
                    continue
                return {
                    'category': category,
                    'confidence': min(0.65 + (0.1 * len(matches)), 0.95),
                    'reason': reason,
                    'important_details': f'Matched: {", ".join(matches)}',
                }
        return {
            'category': 'OTHERS',
            'confidence': 0.4,
            'reason': 'No configured business-email keywords matched.',
            'important_details': '',
        }
