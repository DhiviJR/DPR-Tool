from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('email_classifier', '0001_initial')]

    operations = [
        migrations.AddField(
            model_name='emailrecord',
            name='imap_uid',
            field=models.CharField(blank=True, max_length=100, null=True, unique=True),
        ),
    ]
