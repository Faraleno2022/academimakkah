from django.conf import settings
from django.core.validators import RegexValidator
from django.db import migrations, models


def renseigner_annees(apps, schema_editor):
    """Rattachement historique par grille/date, sans modifier les montants."""
    alias = schema_editor.connection.alias
    mois = getattr(settings, 'ANNEE_SCOLAIRE_DEBUT_MOIS', 9)
    for nom in ('AbonnementBus', 'AbonnementCantine'):
        modele = apps.get_model('bus', nom)
        qs = modele.objects.using(alias).filter(annee_scolaire='')
        if nom == 'AbonnementBus':
            qs = qs.select_related('grille')
        # Pas d'écriture dans un curseur SQLite encore ouvert sur ces lignes.
        dernier_pk = 0
        while True:
            lot = list(qs.filter(pk__gt=dernier_pk).order_by('pk')[:500])
            if not lot:
                break
            dernier_pk = lot[-1].pk
            for abonnement in lot:
                grille = getattr(abonnement, 'grille', None)
                if grille and grille.annee_scolaire:
                    abonnement.annee_scolaire = grille.annee_scolaire
                elif abonnement.date_debut:
                    annee = abonnement.date_debut.year - (abonnement.date_debut.month < mois)
                    abonnement.annee_scolaire = f'{annee}-{annee + 1}'
            modele.objects.using(alias).bulk_update(lot, ['annee_scolaire'])


class Migration(migrations.Migration):
    dependencies = [('bus', '0011_abonnementcantine_montant_tarif_and_more')]
    operations = [
        migrations.AddField(
            model_name='abonnementcantine', name='annee_scolaire',
            field=models.CharField(blank=True, db_index=True, max_length=9,
                validators=[RegexValidator(regex=r'^\d{4}-\d{4}$',
                    message="L'année scolaire doit être au format AAAA-AAAA.")],
                verbose_name='Année scolaire'),
        ),
        migrations.RunPython(renseigner_annees, migrations.RunPython.noop),
    ]
