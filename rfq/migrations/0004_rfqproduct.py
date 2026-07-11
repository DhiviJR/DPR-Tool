import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rfq', '0003_alter_rfq_rfq_no'),
    ]

    operations = [
        migrations.CreateModel(
            name='RFQProduct',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('product_name', models.CharField(max_length=255)),
                ('product_type', models.CharField(blank=True, choices=[('APG steel', 'APG steel'), ('ARG steel', 'ARG steel'), ('APG carbide', 'APG carbide'), ('ARG carbide', 'ARG carbide'), ('SAPG', 'SAPG'), ('SARG', 'SARG'), ('Multi-Gauge', 'Multi-Gauge'), ('unit Std Air', 'unit Std Air'), ('unit SPC Air', 'unit SPC Air'), ('unit Std lvdt', 'unit Std lvdt'), ('unit SPC lvdt', 'unit SPC lvdt'), ('AMC', 'AMC'), ('Service', 'Service'), ('Spares', 'Spares'), ('TPG', 'TPG'), ('TRG', 'TRG'), ('STPG', 'STPG'), ('STRG', 'STRG'), ('PPG', 'PPG'), ('PRG', 'PRG'), ('SPPG', 'SPPG'), ('SPRG', 'SPRG')], max_length=50, null=True)),
                ('quantity', models.IntegerField(default=0)),
                ('rate_per_unit', models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ('value', models.DecimalField(decimal_places=2, max_digits=12)),
                ('remarks', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('rfq', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='products', to='rfq.rfq')),
            ],
        ),
    ]
