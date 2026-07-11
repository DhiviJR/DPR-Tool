import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rfq', '0004_rfqproduct'),
        ('suppliers', '0002_supplier_email'),
    ]

    operations = [
        migrations.AddField(
            model_name='rfqproduct',
            name='price_known',
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name='rfqproduct',
            name='supplier',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='rfq_price_requests', to='suppliers.supplier'),
        ),
    ]
