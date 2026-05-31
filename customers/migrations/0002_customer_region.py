from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('customers', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='customer',
            name='region',
            field=models.CharField(
                blank=True,
                choices=[('Chennai', 'Chennai'), ('Hosur', 'Hosur')],
                max_length=20,
                null=True,
            ),
        ),
    ]
