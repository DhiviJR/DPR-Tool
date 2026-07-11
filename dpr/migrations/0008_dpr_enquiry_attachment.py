from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dpr', '0007_dpr_po_confirmation_date'),
    ]

    operations = [
        migrations.AddField(
            model_name='dpr',
            name='enquiry_attachment',
            field=models.FileField(
                blank=True,
                null=True,
                upload_to='enquiry_files/'
            ),
        ),
    ]
