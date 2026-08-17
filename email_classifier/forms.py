from django import forms
from .models import EmailRecord


class EmailPasteForm(forms.Form):
    sender = forms.EmailField(required=False)
    subject = forms.CharField(max_length=255)
    body = forms.CharField(widget=forms.Textarea(attrs={'rows': 10}))


class ReviewForm(forms.ModelForm):
    class Meta:
        model = EmailRecord
        fields = ['final_category', 'reviewed']
