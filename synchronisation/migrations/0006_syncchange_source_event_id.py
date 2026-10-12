from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('synchronisation', '0005_code_liaison_poste')]

    operations = [
        migrations.AddField(
            model_name='syncchange', name='source_event_id',
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddConstraint(
            model_name='syncchange',
            constraint=models.UniqueConstraint(
                fields=('device', 'source_event_id'), name='sync_device_source_event_unique',
            ),
        ),
    ]
