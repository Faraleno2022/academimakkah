from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [('synchronisation', '0008_syncchange_reception_indexes')]
    operations = [migrations.CreateModel(
        name='SyncMediaTransfer',
        fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('model_label', models.CharField(max_length=120)),
            ('object_uuid', models.UUIDField()),
            ('field_name', models.CharField(max_length=64)),
            ('direction', models.CharField(choices=[('PUSH', 'Envoi'), ('PULL', 'Réception')], max_length=4)),
            ('name', models.CharField(max_length=500)),
            ('sha256', models.CharField(blank=True, max_length=64)),
            ('complete', models.BooleanField(default=False)),
            ('attempts', models.PositiveIntegerField(default=0)),
            ('retry_at', models.DateTimeField(default=django.utils.timezone.now)),
            ('error', models.TextField(blank=True)),
            ('ecole', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='eleves.ecole')),
        ],
        options={
            'constraints': [models.UniqueConstraint(
                fields=('ecole', 'model_label', 'object_uuid', 'field_name', 'direction'),
                name='sync_media_object_direction')],
            'indexes': [models.Index(fields=['ecole', 'complete', 'retry_at'], name='sync_media_pending')],
        },
    )]
