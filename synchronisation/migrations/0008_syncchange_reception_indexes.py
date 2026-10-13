from django.db import migrations, models


def reprendre_identifiants_recus(apps, schema_editor):
    """Copie l'historique existant par petits lots, sans charger le journal entier."""
    Change = apps.get_model('synchronisation', 'SyncChange')
    alias = schema_editor.connection.alias
    dernier = 0
    while True:
        lignes = list(Change.objects.using(alias).filter(pk__gt=dernier)
                      .only('pk', 'payload').order_by('pk')[:1000])
        if not lignes:
            break
        a_modifier = []
        for ligne in lignes:
            identifiant = ligne.payload.get('server_change_id') if isinstance(ligne.payload, dict) else None
            if type(identifiant) is int and 0 < identifiant <= 9223372036854775807:
                ligne.server_change_id = identifiant
                a_modifier.append(ligne)
        if a_modifier:
            Change.objects.using(alias).bulk_update(a_modifier, ['server_change_id'], batch_size=500)
        dernier = lignes[-1].pk


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('synchronisation', '0007_syncchange_revision_index')]
    operations = [
        migrations.AddField('syncchange', 'server_change_id', models.PositiveBigIntegerField(null=True, blank=True)),
        migrations.RunPython(reprendre_identifiants_recus, migrations.RunPython.noop),
        migrations.AddIndex('syncchange', models.Index(
            fields=['ecole', 'server_change_id', 'statut'], name='sync_school_server_status')),
        migrations.AddIndex('syncchange', models.Index(fields=['ecole', 'sequence'], name='sync_school_sequence')),
        migrations.AddIndex('syncchange', models.Index(fields=['ecole', 'statut', 'id'], name='sync_school_status_id')),
    ]
