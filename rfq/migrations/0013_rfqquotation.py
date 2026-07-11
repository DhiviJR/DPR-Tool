# Generated manually for RFQ quotation revision tracking.

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('rfq', '0012_rfq_quotation_prepared_rfqproduct_quotation_prepared'),
    ]

    operations = [
        migrations.CreateModel(
            name='RFQQuotation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quotation_number', models.CharField(max_length=120)),
                ('revision_number', models.PositiveIntegerField(default=0)),
                ('products_snapshot', models.JSONField(blank=True, default=list)),
                ('email_sent', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('rfq', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='quotations', to='rfq.rfq')),
            ],
            options={
                'ordering': ['-created_at'],
                'unique_together': {('rfq', 'revision_number')},
            },
        ),
    ]
