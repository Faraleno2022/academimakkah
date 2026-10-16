"""Régularise la validité cantine selon le tarif net et l'encaissement réel."""
from calendar import monthrange
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations


def regulariser_cantine(apps, schema_editor):
    modele = apps.get_model('bus', 'AbonnementCantine')
    qs = modele.objects.using(schema_editor.connection.alias).filter(sync_deleted_at__isnull=True,
        periodicite__in=('TRIMESTRIEL', 'DEUX_TRIMESTRES', 'TROIS_TRIMESTRES', 'ANNUEL'))
    dernier = 0
    while True:
        lot = list(qs.filter(pk__gt=dernier).order_by('pk')[:500])
        if not lot:
            break
        dernier = lot[-1].pk
        for abo in lot:
            nombre = {'TRIMESTRIEL': 1, 'DEUX_TRIMESTRES': 2, 'TROIS_TRIMESTRES': 3, 'ANNUEL': 3}[abo.periodicite]
            brut = Decimal(abo.montant_tarif if abo.montant_tarif is not None else abo.montant or 0)
            remise = max(Decimal(abo.valeur_remise or 0), Decimal('0'))
            if abo.type_remise == 'POURCENTAGE':
                remise = brut * min(remise, 100) / 100
            elif abo.type_remise != 'MONTANT':
                remise = Decimal('0')
            du = brut - min(brut, remise.quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            paye = max(Decimal(abo.montant or 0), Decimal('0'))
            if du > 0:
                tranche = (du / nombre).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
                seuils = [tranche * n for n in range(1, nombre)] + [du]
                nombre = max(1, sum(paye >= seuil for seuil in seuils)) if paye > 0 else 0
            index = abo.date_debut.year * 12 + abo.date_debut.month - 1 + 3 * nombre
            annee, mois = divmod(index, 12)
            abo.date_expiration = abo.date_debut.replace(year=annee, month=mois + 1,
                day=min(abo.date_debut.day, monthrange(annee, mois + 1)[1])) - timedelta(days=1)
        modele.objects.using(schema_editor.connection.alias).bulk_update(lot, ['date_expiration'], batch_size=250)


class Migration(migrations.Migration):
    dependencies = [('bus', '0013_validite_tranches')]
    operations = [migrations.RunPython(regulariser_cantine, migrations.RunPython.noop)]
