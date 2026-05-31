from django.db import migrations


def copy_region_to_customer(apps, schema_editor):
    DPR = apps.get_model('dpr', 'DPR')
    Customer = apps.get_model('customers', 'Customer')

    for dpr in DPR.objects.exclude(region='').exclude(region__isnull=True):
        customer = Customer.objects.get(pk=dpr.customer_id)
        if not customer.region:
            customer.region = dpr.region
            customer.save(update_fields=['region'])


class Migration(migrations.Migration):

    dependencies = [
        ('dpr', '0005_rename_and_add_qty_fields'),
        ('customers', '0002_customer_region'),
    ]

    operations = [
        migrations.RunPython(copy_region_to_customer, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='dpr',
            name='region',
        ),
    ]
