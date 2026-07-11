from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0011_supplierproduct_not_ok_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='customerproduct',
            name='delivery_detail_type',
            field=models.CharField(blank=True, max_length=20, null=True),
        ),
    ]
