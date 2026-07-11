from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rfq', '0005_rfqproduct_price_known_supplier'),
    ]

    operations = [
        migrations.AddField(
            model_name='rfqproduct',
            name='suppliers',
            field=models.ManyToManyField(blank=True, related_name='rfq_multi_price_requests', to='suppliers.supplier'),
        ),
    ]
