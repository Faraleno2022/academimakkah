"""Totaux des abonnements cantine regroupés par périodicité."""

from decimal import Decimal

from .models import AbonnementCantine


def resumes_periodicites_cantine(abonnements):
    """Un abonnement compte une fois, avec sa remise et son encaissement réels."""
    libelles = dict(AbonnementCantine.Periodicite.choices)
    rang = {code: index for index, code in enumerate(libelles)}
    resumes = {}
    for abo in abonnements:
        code = abo.periodicite
        resume = resumes.setdefault(code, {
            'code': code,
            'label': libelles.get(code, code or 'Non renseignée'),
            'nb': 0,
            'brut': Decimal('0'),
            'remise': Decimal('0'),
            'du': Decimal('0'),
            'paye': Decimal('0'),
            'reste': Decimal('0'),
        })
        resume['nb'] += 1
        resume['brut'] += abo.montant_brut
        resume['remise'] += abo.montant_remise
        resume['du'] += abo.montant_du
        resume['paye'] += abo.deja_paye
        resume['reste'] += abo.reste_a_payer
    return sorted(resumes.values(), key=lambda row: (rang.get(row['code'], len(rang)), row['label']))
