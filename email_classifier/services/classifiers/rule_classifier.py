from .base import BaseEmailClassifier


class RuleBasedEmailClassifier(BaseEmailClassifier):
    """Free local fallback classifier based on business-email keywords."""

    rules = (
        ('CUSTOMER_ORDER', ('purchase order', 'po#', 'po ', 'order confirmation', 'kindly dispatch', 'supply order'),
         'Contains purchase-order or dispatch language.'),
        ('PAYMENT_INVOICE', ('invoice', 'payment advice', 'payment due', 'remittance', 'receipt', 'unpaid'),
         'Contains invoice or payment language.'),
        ('SUPPORT_COMPLAINT', ('complaint', 'not working', 'not work', 'defective', 'breakdown', 'service issue'),
         'Contains support or complaint language.'),
        ('QUOTATION_REQUEST', ('request for quotation', 'request for quote', 'rfq', 'please quote', 'send quotation'),
         'Contains a quotation request.'),
        ('ENQUIRY', ('enquiry', 'inquiry', 'price for', 'price details', 'product details', 'need price'),
         'Contains an enquiry or price request.'),
    )

    def classify(self, subject, body, sender=''):
        text = f'{subject} {body}'.lower()
        for category, keywords, reason in self.rules:
            matches = [keyword for keyword in keywords if keyword in text]
            if matches:
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
