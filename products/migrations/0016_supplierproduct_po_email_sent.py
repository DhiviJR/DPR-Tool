from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0015_customerproduct_mes_rate_per_unit_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='supplierproduct',
            name='po_email_sent',
            field=models.BooleanField(default=False),
        ),
    ]