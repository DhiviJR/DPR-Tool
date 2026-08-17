from django import forms
from .models import EmailRecord



class ReviewForm(forms.ModelForm):
    class Meta:
        model = EmailRecord
        fields = ['final_category', 'reviewed']
