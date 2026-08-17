# Generated for the standalone Email Classifier test project.
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name='EmailRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sender', models.EmailField(blank=True, max_length=254)),
                ('subject', models.CharField(max_length=255)),
                ('body', models.TextField()),
                ('ai_category', models.CharField(choices=[('ENQUIRY', 'Enquiry'), ('CUSTOMER_ORDER', 'Customer Order'), ('QUOTATION_REQUEST', 'Quotation Request'), ('PAYMENT_INVOICE', 'Payment / Invoice'), ('SUPPORT_COMPLAINT', 'Support / Complaint'), ('MISCELLANEOUS', 'Miscellaneous')], max_length=30)),
                ('final_category', models.CharField(blank=True, choices=[('ENQUIRY', 'Enquiry'), ('CUSTOMER_ORDER', 'Customer Order'), ('QUOTATION_REQUEST', 'Quotation Request'), ('PAYMENT_INVOICE', 'Payment / Invoice'), ('SUPPORT_COMPLAINT', 'Support / Complaint'), ('MISCELLANEOUS', 'Miscellaneous')], max_length=30)),
                ('confidence', models.FloatField(default=0)),
                ('reason', models.TextField(blank=True)),
                ('important_details', models.TextField(blank=True)),
                ('source', models.CharField(default='manual', max_length=20)),
                ('received_at', models.DateTimeField(auto_now_add=True)),
                ('reviewed', models.BooleanField(default=False)),
            ],
        ),
    ]
