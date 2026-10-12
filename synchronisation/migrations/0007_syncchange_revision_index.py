from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('synchronisation', '0006_syncchange_source_event_id')]
    operations = [
        migrations.AddIndex(
            model_name='syncchange',
            index=models.Index(fields=['ecole', 'statut', 'date_application'], name='sync_school_applied_date'),
        ),
    ]
