from django.core.management.base import BaseCommand
from suppliers.models import Supplier
from rfq.models import SupplierDatasheet, DatasheetPriceRange, DatasheetModifier, DatasheetAccessoryRate, DatasheetExtensionRate, DatasheetDepthCollarRate, DatasheetAddonRate
from decimal import Decimal


class Command(BaseCommand):
    help = 'Seed initial rate cards for APG, ARG, Carbide Gauges, and Setting Rings.'

    def handle(self, *args, **options):
        supplier = Supplier.objects.filter(supplier_name__icontains='SKILL').first()
        if not supplier:
            supplier = Supplier.objects.first()
        if not supplier:
            supplier = Supplier.objects.create(
                supplier_name='SKILL Make Gauges',
                email='info@skillgauges.com',
                phone_number='9876543210',
                address='Standard Supplier Address'
            )

        self.stdout.write(f"Seeding datasheets for supplier: {supplier.supplier_name}")

        # 1. APG & Setting Rings Rate Card
        SupplierDatasheet.objects.filter(supplier=supplier, product_type='APG').delete()
        apg_ds = SupplierDatasheet.objects.create(
            supplier=supplier,
            product_type='APG',
            title='Air Plug Gauges & Setting Rings Rate Card',
            description='2 jet standard Air Plug Gauges & setting Rings suitable for SKILL make air gauge unit.',
            is_active=True,
        )

        apg_ranges = [
            (4.00, 6.00, 1950.00, 1050.00),
            (6.00, 18.00, 1060.00, 790.00),
            (18.00, 30.00, 1284.00, 870.00),
            (30.00, 50.00, 1358.00, 992.00),
            (50.00, 60.00, 1493.00, 1100.00),
            (60.00, 70.00, 2112.00, 1420.00),
            (70.00, 80.00, 2216.00, 1550.00),
            (80.00, 90.00, 2310.00, 1848.00),
            (90.00, 100.00, 2403.00, 1980.00),
            (100.00, 110.00, 2524.00, 2667.00),
            (110.00, 120.00, 2893.00, 2865.00),
            (120.00, 130.00, 3885.00, 3454.00),
            (140.00, 150.00, 5527.00, 4108.00),
            (150.00, 160.00, 6737.00, 4752.00),
            (160.00, 170.00, 7865.00, 5835.00),
            (170.00, 180.00, 8900.00, 6260.00),
            (180.00, 190.00, 10122.00, 7139.00),
            (190.00, 200.00, 11236.00, 8178.00),
        ]

        for min_d, max_d, apg_p, sr_p in apg_ranges:
            DatasheetPriceRange.objects.create(
                datasheet=apg_ds,
                category_name='Air Plug Gauge & Setting Ring',
                min_diameter=Decimal(str(min_d)),
                max_diameter=Decimal(str(max_d)),
                price=Decimal(str(apg_p)),
                setting_ring_price=Decimal(str(sr_p)),
            )

        # Extensions to check deeper bores
        apg_ds.extensions.all().delete()
        DatasheetExtensionRate.objects.create(datasheet=apg_ds, from_size=Decimal('50.00'), to_size=Decimal('100.00'), price=Decimal('250.00'))
        DatasheetExtensionRate.objects.create(datasheet=apg_ds, from_size=Decimal('100.00'), to_size=Decimal('150.00'), price=Decimal('400.00'))
        DatasheetExtensionRate.objects.create(datasheet=apg_ds, from_size=Decimal('150.00'), to_size=None, price=Decimal('600.00'))

        # Depth Collar for air plug gauges
        apg_ds.depth_collars.all().delete()
        DatasheetDepthCollarRate.objects.create(datasheet=apg_ds, from_size=Decimal('4.00'), to_size=Decimal('50.00'), price=Decimal('180.00'))
        DatasheetDepthCollarRate.objects.create(datasheet=apg_ds, from_size=Decimal('50.00'), to_size=Decimal('100.00'), price=Decimal('320.00'))
        DatasheetDepthCollarRate.objects.create(datasheet=apg_ds, from_size=Decimal('100.00'), to_size=None, price=Decimal('500.00'))

        # Add-ons for APG
        apg_ds.addons.all().delete()
        DatasheetAddonRate.objects.create(datasheet=apg_ds, addon_name='Right angle attachment', spec_value='', adjustment_type='FIXED', adjustment_value=Decimal('450.00'))
        DatasheetAddonRate.objects.create(datasheet=apg_ds, addon_name='Bracket or Bench mounted', spec_value='', adjustment_type='FIXED', adjustment_value=Decimal('800.00'))
        DatasheetAddonRate.objects.create(datasheet=apg_ds, addon_name='Super Blind Bore', spec_value='', adjustment_type='PERCENTAGE', adjustment_value=Decimal('20.00'))
        DatasheetAddonRate.objects.create(datasheet=apg_ds, addon_name='Dull chrome plated setting ring', spec_value='', adjustment_type='PERCENTAGE', adjustment_value=Decimal('25.00'))
        DatasheetAddonRate.objects.create(datasheet=apg_ds, addon_name='Air jet', spec_value='3', adjustment_type='PERCENTAGE', adjustment_value=Decimal('20.00'))

        # 2. Air Ring Gauges (ARG) Rate Card
        SupplierDatasheet.objects.filter(supplier=supplier, product_type='ARG').delete()
        arg_ds = SupplierDatasheet.objects.create(
            supplier=supplier,
            product_type='ARG',
            title='Air Ring Gauges Rate Card',
            description='2 jet standard Air Ring Gauges & setting Plugs.',
            is_active=True,
        )

        arg_ranges = [
            (6.00, 10.00, 3700.00, 4450.00, 750.00),
            (10.00, 38.00, 3110.00, 3580.00, 870.00),
            (38.00, 50.00, 3640.00, 4495.00, 890.00),
            (50.00, 65.00, 4928.00, 5900.00, 925.00),
            (65.00, 75.00, 5970.00, 7180.00, 1110.00),
            (75.00, 90.00, 6710.00, 8085.00, 1204.00),
            (90.00, 100.00, 7656.00, 9185.00, 1386.00),
            (100.00, 110.00, 11760.00, 14200.00, 2350.00),
            (110.00, 120.00, 14135.00, 16967.00, 2810.00),
        ]

        for min_d, max_d, cj_p, oj_p, sp_p in arg_ranges:
            DatasheetPriceRange.objects.create(
                datasheet=arg_ds,
                category_name='Central Jet Air Ring Gauge',
                min_diameter=Decimal(str(min_d)),
                max_diameter=Decimal(str(max_d)),
                price=Decimal(str(cj_p)),
            )

        DatasheetModifier.objects.create(
            datasheet=arg_ds,
            modifier_name='3 Jet Air Ring Gauge (+20%)',
            spec_key='No. of jets',
            spec_value_match='3',
            adjustment_type='PERCENTAGE',
            adjustment_value=Decimal('20.00')
        )
        DatasheetModifier.objects.create(
            datasheet=arg_ds,
            modifier_name='Bench Mounted Air Ring Gauge (+₹800)',
            spec_key='Gauge Type',
            spec_value_match='Bench mount',
            adjustment_type='FIXED',
            adjustment_value=Decimal('800.00')
        )

        self.stdout.write(self.style.SUCCESS("Successfully seeded supplier datasheet rate cards!"))
