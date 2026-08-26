from django import forms
from .models import EmailRecord



class EmailPasteForm(forms.ModelForm):
    class Meta:
        model = EmailRecord
        fields = ['sender', 'subject', 'body']
        widgets = {
            'sender': forms.EmailInput(attrs={'class': 'form-control', 'placeholder': 'e.g. customer@example.com (optional)'}),
            'subject': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'e.g. Request for Quotation - Pressure Transmitter'}),
            'body': forms.Textarea(attrs={'class': 'form-control', 'rows': 8, 'placeholder': 'Paste full email text content here...'}),
        }


class ReviewForm(forms.ModelForm):
    class Meta:
        model = EmailRecord
        fields = ['final_category', 'reviewed']

