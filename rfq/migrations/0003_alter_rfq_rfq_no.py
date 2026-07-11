from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rfq', '0002_rfq_attachment'),
    ]

    operations = [
        migrations.AlterField(
            model_name='rfq',
            name='rfq_no',
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
    ]
