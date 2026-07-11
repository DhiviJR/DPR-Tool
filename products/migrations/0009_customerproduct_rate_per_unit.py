from decimal import Decimal

from django.db import migrations, models


def populate_rate_per_unit(apps, schema_editor):
    CustomerProduct = apps.get_model('products', 'CustomerProduct')

    for product in CustomerProduct.objects.all():
        if product.quantity_ordered:
            product.rate_per_unit = (
                product.value / Decimal(product.quantity_ordered)
            ).quantize(Decimal('0.01'))
            product.save(update_fields=['rate_per_unit'])


class Migration(migrations.Migration):

    dependencies = [
        ('products', '0008_customerproduct_product_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='customerproduct',
            name='rate_per_unit',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.RunPython(populate_rate_per_unit, migrations.RunPython.noop),
    ]
