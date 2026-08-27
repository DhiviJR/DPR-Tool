import json
import ollama

from .base import BaseEmailClassifier


class OllamaEmailClassifier(BaseEmailClassifier):
    allowed_categories = {
        'ENQUIRY', 'CUSTOMER_ORDER', 'QUOTATION_REQUEST', 'PAYMENT_INVOICE',
        'DISPATCH', 'SUPPORT_COMPLAINT', 'OTHERS',
    }

    def __init__(self, model, host):
        self.model = model
        self.client = ollama.Client(host=host)

    def classify(self, subject, body, sender=''):
        prompt = f'''Classify this business email. Return JSON only.
Allowed categories: {', '.join(sorted(self.allowed_categories))}
Schema: {{"category":"one allowed category","confidence":0.0,"reason":"short reason","important_details":"short facts"}}
Sender: {sender}\nSubject: {subject}\nBody:\n{body}'''
        response = self.client.generate(
            model=self.model, prompt=prompt, format='json', stream=False,
            options={'temperature': 0},
        )
        data = json.loads(response['response'])
        category = str(data.get('category', '')).upper()
        if category not in self.allowed_categories:
            category = 'OTHERS'
        try:
            confidence = min(max(float(data.get('confidence', 0)), 0), 1)
        except (TypeError, ValueError):
            confidence = 0
        return {
            'category': category, 'confidence': confidence,
            'reason': str(data.get('reason', '')),
            'important_details': str(data.get('important_details', '')),
        }
