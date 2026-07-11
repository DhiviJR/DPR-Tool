from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rfq', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='rfq',
            name='attachment',
            field=models.FileField(blank=True, null=True, upload_to='rfq_attachments/'),
        ),
    ]
