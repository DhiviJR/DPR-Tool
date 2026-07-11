from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0010_supplierproduct_rate_per_unit'),
    ]

    operations = [
        migrations.AddField(
            model_name='supplierproduct',
            name='quantity_not_ok',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='supplierproduct',
            name='not_ok_reason',
            field=models.TextField(blank=True, null=True),
        ),
    ]
