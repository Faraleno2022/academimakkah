from django.db import migrations, models
from django.db.models import F, Max


def numeroter_historique(apps, schema_editor):
    """Les changements existants gardent leur id comme numéro de diffusion.

    Les curseurs déjà enregistrés sur les postes (des id) restent ainsi valides,
    et le compteur repart après le plus grand id.
    """
    SyncChange = apps.get_model('synchronisation', 'SyncChange')
    SyncSequence = apps.get_model('synchronisation', 'SyncSequence')
    SyncChange.objects.update(sequence=F('id'))
    maximum = SyncChange.objects.aggregate(maximum=Max('id'))['maximum'] or 0
    SyncSequence.objects.update_or_create(pk=1, defaults={'valeur': maximum})


class Migration(migrations.Migration):

    dependencies = [
        ('synchronisation', '0002_synccheckpoint_syncownership'),
    ]

    operations = [
        migrations.AddField(
            model_name='syncchange',
            name='sequence',
            field=models.PositiveBigIntegerField(blank=True, null=True, unique=True),
        ),
        migrations.CreateModel(
            name='SyncSequence',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('valeur', models.PositiveBigIntegerField(default=0)),
            ],
        ),
        migrations.RunPython(numeroter_historique, migrations.RunPython.noop),
    ]
