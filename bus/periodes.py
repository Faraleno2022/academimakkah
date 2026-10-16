"""Dates de fin calculées à partir du premier jour de l'abonnement."""

from calendar import monthrange
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP


def ajouter_mois(jour, mois):
    index = jour.year * 12 + jour.month - 1 + mois
    annee, mois_index = divmod(index, 12)
    return jour.replace(year=annee, month=mois_index + 1,
                        day=min(jour.day, monthrange(annee, mois_index + 1)[1]))


def fin_abonnement(debut, periodicite, service):
    if service == 'bus':
        if periodicite == 'MENSUEL':
            mois = 1
        else:
            from .models import PORTEES_PAIEMENT_BUS
            mois = 3 * len(PORTEES_PAIEMENT_BUS.get(periodicite, {'T1': 1}))
        return ajouter_mois(debut, mois) - timedelta(days=1)

    if periodicite == 'JOURNALIER':
        return debut
    if periodicite == 'HEBDOMADAIRE':
        return debut + timedelta(days=6)
    mois = {
        'MENSUEL': 1, 'TRIMESTRIEL': 3,
        'DEUX_TRIMESTRES': 6, 'TROIS_TRIMESTRES': 9,
        'ANNUEL': 9,
    }.get(periodicite, 1)
    return ajouter_mois(debut, mois) - timedelta(days=1)


TRANCHES_VALIDITE_BUS = {
    'T1': {'T1'}, 'T2': {'T2'}, 'T3': {'T3'},
    'T1_T2M': {'T1', 'T2'}, 'T1_T2': {'T1', 'T2'},
    'T2_T3': {'T2', 'T3'},
    'T1_T2_T3M': {'T1', 'T2', 'T3'}, 'ANNUEL': {'T1', 'T2', 'T3'},
}

TRIMESTRES_CANTINE = {'TRIMESTRIEL': 1, 'DEUX_TRIMESTRES': 2, 'TROIS_TRIMESTRES': 3, 'ANNUEL': 3}


def trimestres_cantine_payes(periodicite, du, paye):
    """Répartit le tarif net de la période en tranches, au franc près."""
    nombre = TRIMESTRES_CANTINE.get(periodicite, 0)
    if not nombre:
        return 0
    du, paye = Decimal(du), max(Decimal(paye), Decimal('0'))
    if du <= 0:
        return nombre
    if paye <= 0:
        return 0
    tranche = (du / nombre).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    seuils = [tranche * n for n in range(1, nombre)] + [du]
    # Comme pour le bus : le premier acompte ouvre le premier trimestre.
    return max(1, sum(paye >= seuil for seuil in seuils))


def fin_validite_cantine(abonnement):
    if abonnement.periodicite not in TRIMESTRES_CANTINE:
        return fin_abonnement(abonnement.date_debut, abonnement.periodicite, 'cantine')
    nombre = trimestres_cantine_payes(abonnement.periodicite, abonnement.montant_du, abonnement.deja_paye)
    return ajouter_mois(abonnement.date_debut, 3 * nombre) - timedelta(days=1)


def libelle_periode_couverte(debut, fin):
    if not debut or not fin or fin < debut:
        return '0 mois sur 9 mois'
    lendemain = fin + timedelta(days=1)
    mois = (lendemain.year - debut.year) * 12 + lendemain.month - debut.month
    return f'{max(mois, 0)} mois sur 9 mois'


def groupe_validite_bus(ligne):
    return (ligne['eleve_id'], ligne['grille_id'], ligne['annee_scolaire'],
            ligne['zone'] if not ligne['grille_id'] else '')


def nombre_tranches_payees(totaux, dus):
    """Même répartition du cash que la grille, remises déjà déduites des dus."""
    ordre = ('T1', 'T2', 'T3')
    payes = {code: Decimal('0') for code in ordre}
    departs = {code: next(t for t in ordre if t in couvertes)
               for code, couvertes in TRANCHES_VALIDITE_BUS.items()}
    for depart in reversed(ordre):
        restant = sum((Decimal(valeur or 0) for code, valeur in totaux.items()
                       if departs.get(code, 'T1') == depart), Decimal('0'))
        for code in ordre[ordre.index(depart):]:
            part = min(max(Decimal(dus[code]) - payes[code], Decimal('0')), restant)
            payes[code] += part
            restant -= part
    complets = sum(payes[code] >= dus[code] for code in ordre)
    # Le premier acompte ouvre le premier trimestre. Les suivants doivent
    # solder les tranches pour prolonger l'accès au deuxième puis au troisième.
    return max(complets, int(sum(totaux.values(), Decimal('0')) > 0))


def periodes_validite_bus(lignes, tarifs=None):
    """Validité acquise à chaque reçu, sans déplacer la date du paiement.

    Un complément sur une même tranche ne prolonge pas la durée. Une nouvelle
    tranche porte la validité à 6 puis 9 mois depuis le premier paiement.
    """
    etats, periodes = {}, {}
    for ligne in sorted(lignes, key=lambda r: (
        r['date_debut'], r.get('numero_recu') or '\uffff', str(r.get('sync_uuid') or ''),
    )):
        debut = ligne['date_debut']
        couvertes = TRANCHES_VALIDITE_BUS.get(ligne['periodicite'])
        if not couvertes:
            periodes[ligne['id']] = (debut, ligne.get('date_expiration') or
                                    fin_abonnement(debut, ligne['periodicite'], 'bus'))
            continue
        etat = etats.setdefault(groupe_validite_bus(ligne), {
            'debut': debut, 'tranches': set(), 'totaux': {},
        })
        etat['tranches'].update(couvertes)
        code = ligne['periodicite']
        etat['totaux'][code] = etat['totaux'].get(code, Decimal('0')) + Decimal(ligne.get('montant') or 0)
        dus = (tarifs or {}).get((ligne['eleve_id'], ligne['grille_id']))
        nombre = nombre_tranches_payees(etat['totaux'], dus) if dus is not None else len(etat['tranches'])
        periodes[ligne['id']] = (
            etat['debut'], ajouter_mois(etat['debut'], 3 * nombre) - timedelta(days=1),
        )
    return periodes


CHAMPS_VALIDITE_BUS = (
    'id', 'eleve_id', 'grille_id', 'annee_scolaire', 'zone', 'periodicite',
    'date_debut', 'date_expiration', 'created_at', 'montant', 'numero_recu', 'sync_uuid',
)


def abonnements_du_cycle(eleve_id, grille_id, annee_scolaire, zone):
    from .models import AbonnementBus
    qs = AbonnementBus.objects.filter(eleve_id=eleve_id, sync_deleted_at__isnull=True)
    if grille_id:
        return qs.filter(grille_id=grille_id)
    return qs.filter(grille__isnull=True, annee_scolaire=annee_scolaire, zone=zone)


REMISE_PAR_DEFAUT = object()


def dates_validite_bus(abonnement, remise=REMISE_PAR_DEFAUT):
    if abonnement.periodicite not in TRANCHES_VALIDITE_BUS or not abonnement.eleve_id:
        return abonnement.date_debut, abonnement.date_expiration or fin_abonnement(
            abonnement.date_debut, abonnement.periodicite, 'bus')
    qs = abonnements_du_cycle(abonnement.eleve_id, abonnement.grille_id,
                              abonnement.annee_scolaire, abonnement.zone)
    lignes = list(qs.exclude(pk=abonnement.pk).values(*CHAMPS_VALIDITE_BUS))
    lignes.append({champ: getattr(abonnement, champ) for champ in CHAMPS_VALIDITE_BUS})
    tarifs = {}
    if abonnement.grille_id:
        if remise is REMISE_PAR_DEFAUT:
            remise = abonnement.grille.remise_pour(abonnement.eleve_id)
        tarifs[(abonnement.eleve_id, abonnement.grille_id)] = abonnement.grille._tranches(remise)
    return periodes_validite_bus(lignes, tarifs)[abonnement.pk]


def recalculer_dates_bus(eleve_id, grille_id, annee_scolaire, zone):
    """Répercute une correction ou suppression d'un ancien reçu sur la suite."""
    from synchronisation.mixins import SyncTrackedModel
    qs = abonnements_du_cycle(eleve_id, grille_id, annee_scolaire, zone)
    abonnements = list(qs.select_related('grille'))
    tarifs = {}
    if abonnements and grille_id:
        grille = abonnements[0].grille
        tarifs[(eleve_id, grille_id)] = grille._tranches(grille.remise_pour(eleve_id))
    periodes = periodes_validite_bus([
        {champ: getattr(abo, champ) for champ in CHAMPS_VALIDITE_BUS} for abo in abonnements
    ], tarifs)
    for abo in abonnements:
        debut, fin = periodes[abo.pk]
        if (abo.date_debut_abonnement, abo.date_expiration) != (debut, fin):
            abo.date_debut_abonnement, abo.date_expiration = debut, fin
            # Contourner seulement la récursion ; conserver les signaux de sync.
            SyncTrackedModel.save(abo, update_fields=['date_debut_abonnement', 'date_expiration'])
    return periodes
