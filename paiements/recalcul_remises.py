"""Rejoue les bases de remise enregistrées sans deviner les règles historiques."""
from decimal import Decimal, ROUND_HALF_UP

from .allocation import (
    ALLOCATION_COMPONENTS, allocate_amount_sequentially, allocate_discounts, repartir_remise_par_poste,
)
from .models import Paiement


def memoriser_regle_remise(remise, base_calcul, tranches, *, montant_avant_remise=None,
                          montant_net_enregistre=False, inclure_inscription=False):
    regle = {
        'base': base_calcul,
        'tranches': sorted(int(n) for n in tranches),
        'type': remise.type_remise,
        'valeur': str(remise.valeur),
    }
    if montant_net_enregistre:
        regle['montant_net_enregistre'] = True
    if inclure_inscription:
        regle['inclure_inscription'] = True
    if montant_avant_remise is not None:
        regle['montant_avant_remise'] = str(montant_avant_remise)
    return regle


def montant_brut_pour_remise(paiement):
    """Retrouve le tarif saisi pour ne pas déduire deux fois une remise."""
    for ligne in paiement.remises.all():
        brut = (ligne.regle_calcul or {}).get('montant_avant_remise')
        if brut is not None:
            return Decimal(str(brut))
    return Decimal(str(paiement.montant))


def montant_affiche_sur_recu(paiement):
    """Les remises déduites avant encaissement sont déjà dans le montant net."""
    remises_non_deduites = sum((
        ligne.montant_remise for ligne in paiement.remises.all()
        if 'montant_avant_remise' not in (ligne.regle_calcul or {})
        and not (ligne.regle_calcul or {}).get('montant_net_enregistre')
    ), Decimal('0'))
    return max(Decimal('0'), Decimal(str(paiement.montant)) - remises_non_deduites)


def recalculer_remises_echeancier(echeancier):
    """Recalcule chaque remise depuis les versements validés qui la précèdent.

    Les paiements en attente sont recalculés mais ne consomment aucun solde.
    Le taux/montant accordé reste celui enregistré, même si le catalogue change.
    """
    paiements = (
        Paiement.objects.filter(eleve_id=echeancier.eleve_id,
                               annee_scolaire=echeancier.annee_scolaire,
                               statut__in=['VALIDE', 'EN_ATTENTE'])
        .prefetch_related('remises')
        .order_by('date_paiement', 'date_creation', 'pk')
    )
    payes = {key: Decimal('0') for key, _, _ in ALLOCATION_COMPONENTS}
    for paiement in paiements:
        allocation, nouveaux_payes, _ = allocate_amount_sequentially(
            echeancier, paiement.montant, initial_paid=payes,
        )
        for ligne in paiement.remises.all():
            regle = ligne.regle_calcul
            if not regle:
                continue
            allocation_base = allocation
            if regle.get('montant_avant_remise') is not None:
                allocation_base, _, _ = allocate_amount_sequentially(
                    echeancier, Decimal(regle['montant_avant_remise']), initial_paid=payes,
                )
            postes = [f'tranche_{n}' for n in regle['tranches'] if n in (1, 2, 3)]
            if regle.get('inclure_inscription'):
                postes.insert(0, 'inscription')
            dus = {key: Decimal(str(getattr(echeancier, champ) or 0))
                   for key, champ, _ in ALLOCATION_COMPONENTS}
            bases = {key: max(Decimal('0'), dus[key] if regle['base'] == 'tranches_dues'
                              else allocation_base[key]) for key in postes}
            base = sum(bases.values(), Decimal('0'))
            valeur = Decimal(regle['valeur'])
            montant = base * valeur / 100 if regle['type'] == 'POURCENTAGE' else valeur
            montant = max(Decimal('0'), min(base, montant)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            champs = []
            if regle.get('inclure_inscription'):
                repartition = {key: str(valeur) for key, valeur in repartir_remise_par_poste(montant, bases).items()}
                if regle.get('repartition') != repartition:
                    ligne.regle_calcul = {**regle, 'repartition': repartition}
                    champs.append('regle_calcul')
            if montant != ligne.montant_remise:
                ligne.montant_remise = montant
                champs.append('montant_remise')
            if champs:
                ligne.save(update_fields=champs)
        if paiement.statut == 'VALIDE':
            remises_admission = [ligne for ligne in paiement.remises.all()
                                if (ligne.regle_calcul or {}).get('inclure_inscription')]
            if not remises_admission:
                payes = nouveaux_payes
            else:
                soldes = {key: max(Decimal('0'), Decimal(str(getattr(echeancier, champ) or 0)) - payes[key])
                          for key, champ, _ in ALLOCATION_COMPONENTS}
                reserve, _ = allocate_discounts(echeancier, remises_admission, balances=soldes)
                _, payes, _ = allocate_amount_sequentially(
                    echeancier, paiement.montant,
                    initial_paid={key: payes[key] + reserve[key] for key in payes},
                )
