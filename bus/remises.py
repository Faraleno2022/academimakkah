"""Remises sur l'abonnement bus : saisie, enregistrement et liste des bénéficiaires."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from .models import ORDRE_TRANCHES, AbonnementBus, RemiseBus

# Ce que couvre le plafond d'un reçu selon sa première tranche (report de l'excédent).
PORTEE_PLAFOND = {
    'T1': "de l'année",
    'T2': "des 2ème et 3ème tranches",
    'T3': "de la 3ème tranche",
}


def construire_remise(eleve, grille, type_remise, valeur, motif='', tranches=ORDRE_TRANCHES):
    """Remise non enregistrée (aperçu ou contrôle avant enregistrement) ; None sans remise."""
    try:
        valeur = Decimal(str(valeur or 0).replace(',', '.'))
    except (InvalidOperation, ValueError):
        valeur = Decimal('0')
    if type_remise not in RemiseBus.TypeRemise.values or valeur <= 0:
        return None
    return RemiseBus(
        eleve=eleve, grille=grille, type_remise=type_remise, valeur=valeur,
        motif=motif or RemiseBus.MotifRemise.AUTRE,
        sur_tranche_1='T1' in tranches, sur_tranche_2='T2' in tranches, sur_tranche_3='T3' in tranches,
    )


def enregistrer_remise(eleve, grille, remise, user=None):
    """Crée, met à jour ou retire (remise None) la remise de l'élève sur cette grille."""
    existante = grille.remise_pour(eleve)
    if remise is None:
        if existante:
            existante.delete()
        return None
    cible = existante or RemiseBus(eleve=eleve, grille=grille, cree_par=user)
    for champ in ('type_remise', 'valeur', 'motif', 'sur_tranche_1', 'sur_tranche_2', 'sur_tranche_3'):
        setattr(cible, champ, getattr(remise, champ))
    if remise.observations:
        cible.observations = remise.observations
    cible.save()
    return cible


def remise_json(remise, grille):
    if remise is None:
        return None
    return {
        'type': remise.type_remise,
        'valeur': f"{Decimal(remise.valeur).normalize():f}",
        'motif': remise.motif,
        'tranches': remise.tranches,
        'libelle': remise.libelle,
        'montant_annuel': int(remise.montant_total(grille)),
    }


def lignes_remises(remises):
    """Une ligne par remise : tarif, remise, net dû, payé et reste de l'année."""
    remises = list(remises.select_related('eleve', 'eleve__classe', 'grille'))
    totaux = defaultdict(lambda: defaultdict(Decimal))
    paires = {(r.eleve_id, r.grille_id) for r in remises}
    versements = AbonnementBus.objects.filter(
        eleve_id__in={e for e, _g in paires}, grille_id__in={g for _e, g in paires},
    ).values_list('eleve_id', 'grille_id', 'periodicite', 'montant')
    for eleve_id, grille_id, periodicite, montant in versements:
        totaux[(eleve_id, grille_id)][periodicite] += Decimal(montant or 0)
    lignes = []
    for remise in remises:
        situation = remise.grille.situation_depuis_totaux(totaux[(remise.eleve_id, remise.grille_id)], remise)
        annuel = situation['ANNUEL']
        lignes.append({
            'remise': remise,
            'eleve': remise.eleve,
            'classe': getattr(remise.eleve.classe, 'nom', '') or '',
            'grille': remise.grille,
            'tarif': annuel['brut'],
            'montant_remise': annuel['remise'],
            'net': annuel['du'],
            'paye': annuel['paye'],
            'reste': annuel['reste'],
        })
    lignes.sort(key=lambda l: (l['classe'].casefold(), (l['eleve'].nom or '').casefold(), (l['eleve'].prenom or '').casefold()))
    return lignes


def totaux_remises(lignes):
    return {
        cle: sum((ligne[cle] for ligne in lignes), Decimal('0'))
        for cle in ('tarif', 'montant_remise', 'net', 'paye', 'reste')
    }
