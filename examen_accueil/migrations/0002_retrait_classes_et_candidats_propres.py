"""Abandon des classes et candidats propres au test d'accueil.

Le module travaille désormais sur les classes et les élèves de l'école
(migration 0003). Les anciennes tables sont supprimées avec leur historique de
synchronisation : ces lignes ne correspondent plus à aucun modèle diffusé.
"""
from django.db import migrations

ANCIENS_MODELES = [
    'examen_accueil.ClasseAccueil',
    'examen_accueil.MatiereAccueil',
    'examen_accueil.CandidatAccueil',
    'examen_accueil.NoteAccueil',
]


def purger_synchronisation(apps, schema_editor):
    alias = schema_editor.connection.alias
    for modele in ('SyncChange', 'SyncOwnership', 'SyncMediaTransfer'):
        apps.get_model('synchronisation', modele).objects.using(alias).filter(
            model_label__in=ANCIENS_MODELES).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('examen_accueil', '0001_initial'),
        ('synchronisation', '0009_syncmediatransfer'),
    ]

    operations = [
        migrations.RunPython(purger_synchronisation, migrations.RunPython.noop),
        migrations.DeleteModel(name='NoteAccueil'),
        migrations.DeleteModel(name='CandidatAccueil'),
        migrations.DeleteModel(name='MatiereAccueil'),
        migrations.DeleteModel(name='ClasseAccueil'),
    ]
