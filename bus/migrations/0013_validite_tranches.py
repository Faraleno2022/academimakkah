"""Régularise les périodes sans toucher aux paiements ni aux tarifs."""
from calendar import monthrange
from datetime import timedelta
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from django.db import migrations, models


TRANCHES = {
    'T1': {'T1'}, 'T2': {'T2'}, 'T3': {'T3'},
    'T1_T2M': {'T1', 'T2'}, 'T1_T2': {'T1', 'T2'}, 'T2_T3': {'T2', 'T3'},
    'T1_T2_T3M': {'T1', 'T2', 'T3'}, 'ANNUEL': {'T1', 'T2', 'T3'},
}


def fin_mois(debut, mois):
    index = debut.year * 12 + debut.month - 1 + mois
    annee, mois_index = divmod(index, 12)
    return debut.replace(year=annee, month=mois_index + 1,
        day=min(debut.day, monthrange(annee, mois_index + 1)[1])) - timedelta(days=1)


def tranches_nettes(grille, remise):
    ordre = ('T1', 'T2', 'T3')
    bruts = {code: Decimal(getattr(grille, f'tranche_{n}') or 0)
             for n, code in enumerate(ordre, 1)}
    if not remise:
        return bruts
    portee = [code for n, code in enumerate(ordre, 1)
              if getattr(remise, f'sur_tranche_{n}') and bruts[code] > 0]
    valeur = Decimal(remise.valeur or 0)
    remises = {code: Decimal('0') for code in ordre}
    if valeur <= 0 or not portee:
        return bruts
    if remise.type_remise == 'POURCENTAGE':
        for code in portee:
            remises[code] = (bruts[code] * min(valeur, 100) / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    else:
        total = sum(bruts[code] for code in portee)
        montant = min(valeur.quantize(Decimal('1'), rounding=ROUND_HALF_UP), total)
        exacts = {code: montant * bruts[code] / total for code in portee}
        remises.update({code: exacts[code].to_integral_value(rounding=ROUND_FLOOR) for code in portee})
        manque = int(montant - sum(remises.values()))
        for code in sorted(portee, key=lambda c: exacts[c] - remises[c], reverse=True)[:manque]:
            remises[code] += 1
    return {code: bruts[code] - remises[code] for code in ordre}


def nombre_tranches(totaux, dus):
    ordre = ('T1', 'T2', 'T3')
    payes = {code: Decimal('0') for code in ordre}
    departs = {code: next(t for t in ordre if t in couverts) for code, couverts in TRANCHES.items()}
    for depart in reversed(ordre):
        restant = sum((valeur for code, valeur in totaux.items()
                       if departs.get(code, 'T1') == depart), Decimal('0'))
        for code in ordre[ordre.index(depart):]:
            part = min(max(dus[code] - payes[code], Decimal('0')), restant)
            payes[code] += part
            restant -= part
    return max(sum(payes[code] >= dus[code] for code in ordre), int(sum(totaux.values()) > 0))


def regulariser_dates(apps, schema_editor):
    alias = schema_editor.connection.alias
    bus = apps.get_model('bus', 'AbonnementBus')
    qs = bus.objects.using(alias).filter(sync_deleted_at__isnull=True)
    grilles = apps.get_model('bus', 'GrilleTarifaireBus').objects.using(alias).in_bulk()
    modele_remise = apps.get_model('bus', 'RemiseBus')
    eleves = list(qs.order_by().values_list('eleve_id', flat=True).distinct())
    for debut_lot in range(0, len(eleves), 250):
        abonnements = list(qs.filter(eleve_id__in=eleves[debut_lot:debut_lot + 250]).order_by(
            'date_debut', 'created_at', 'pk'))
        abonnements.sort(key=lambda a: (a.date_debut, a.numero_recu or '\uffff', str(a.sync_uuid)))
        etats = {}
        remises = {(r.eleve_id, r.grille_id): r for r in modele_remise.objects.using(alias).filter(
            eleve_id__in=eleves[debut_lot:debut_lot + 250])}
        for abo in abonnements:
            abo.date_debut_abonnement = abo.date_debut
            couvertes = TRANCHES.get(abo.periodicite)
            if not couvertes:
                continue
            cle = (abo.eleve_id, abo.grille_id, abo.annee_scolaire,
                   abo.zone if not abo.grille_id else '')
            etat = etats.setdefault(cle, {'debut': abo.date_debut, 'tranches': set(), 'totaux': {}})
            etat['tranches'].update(couvertes)
            etat['totaux'][abo.periodicite] = etat['totaux'].get(abo.periodicite, Decimal('0')) + abo.montant
            abo.date_debut_abonnement = etat['debut']
            nombre = len(etat['tranches'])
            if abo.grille_id in grilles:
                dus = tranches_nettes(grilles[abo.grille_id], remises.get((abo.eleve_id, abo.grille_id)))
                nombre = nombre_tranches(etat['totaux'], dus)
            abo.date_expiration = fin_mois(etat['debut'], 3 * nombre)
        bus.objects.using(alias).bulk_update(abonnements,
            ['date_debut_abonnement', 'date_expiration'], batch_size=250)

    cantine = apps.get_model('bus', 'AbonnementCantine')
    qs = cantine.objects.using(alias).filter(periodicite__in=(
        'TRIMESTRIEL', 'DEUX_TRIMESTRES', 'TROIS_TRIMESTRES', 'ANNUEL'))
    dernier_pk = 0
    while True:
        lot = list(qs.filter(pk__gt=dernier_pk).order_by('pk')[:500])
        if not lot:
            break
        dernier_pk = lot[-1].pk
        for abo in lot:
            mois = {'TRIMESTRIEL': 3, 'DEUX_TRIMESTRES': 6,
                    'TROIS_TRIMESTRES': 9, 'ANNUEL': 9}[abo.periodicite]
            abo.date_expiration = fin_mois(abo.date_debut, mois)
        cantine.objects.using(alias).bulk_update(lot, ['date_expiration'], batch_size=250)


class Migration(migrations.Migration):
    dependencies = [('bus', '0012_abonnements_annee_scolaire')]
    operations = [
        migrations.AddField(model_name='abonnementbus', name='date_debut_abonnement',
            field=models.DateField(null=True, blank=True, verbose_name='Début de validité')),
        migrations.RunPython(regulariser_dates, migrations.RunPython.noop),
    ]
