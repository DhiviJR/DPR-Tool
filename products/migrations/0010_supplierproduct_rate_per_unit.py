from decimal import Decimal

from django.db import migrations, models


def populate_supplier_rate_per_unit(apps, schema_editor):
    SupplierProduct = apps.get_model('products', 'SupplierProduct')
    for product in SupplierProduct.objects.all():
        if product.quantity:
            product.rate_per_unit = (
                product.po_value / Decimal(product.quantity)
            ).quantize(Decimal('0.01'))
            product.save(update_fields=['rate_per_unit'])


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0009_customerproduct_rate_per_unit'),
    ]

    operations = [
        migrations.AddField(
            model_name='supplierproduct',
            name='rate_per_unit',
            field=models.DecimalField(
                decimal_places=2,
                default=0,
                max_digits=12
            ),
        ),
        migrations.RunPython(
            populate_supplier_rate_per_unit,
            migrations.RunPython.noop
        ),
    ]
