"""Totaux des abonnements cantine regroupés par périodicité."""

from decimal import Decimal
from django.db.models import Case, Count, DecimalField, ExpressionWrapper, F, FloatField, Sum, Value, When
from django.db.models.functions import Cast, Coalesce, Greatest, Least, Round

from .models import AbonnementCantine


def montants_cantine_sql(qs):
    """Même remise au franc que le modèle, calculée par la base."""
    argent = DecimalField(max_digits=20, decimal_places=0)
    zero = Value(0, output_field=argent)
    qs = qs.annotate(_brut=Coalesce('montant_tarif', 'montant', output_field=argent))
    valeur = Greatest(F('valeur_remise'), Value(Decimal('0')))
    pourcentage = ExpressionWrapper(
        Cast(F('_brut'), FloatField()) * Cast(Least(valeur, Value(Decimal('100'))), FloatField()) / Value(100.0),
        output_field=FloatField(),
    )
    qs = qs.annotate(_remise=Least(F('_brut'), Case(
        When(type_remise='POURCENTAGE', then=Cast(Round(pourcentage), argent)),
        When(type_remise='MONTANT', then=Cast(Round(valeur), argent)),
        default=zero, output_field=argent,
    )))
    return qs.annotate(
        _du=ExpressionWrapper(F('_brut') - F('_remise'), output_field=argent),
        _reste=Greatest(ExpressionWrapper(F('_brut') - F('_remise') - F('montant'), output_field=argent), zero),
    )


def _agregats_cantine():
    return {
        'nb': Count('pk'),
        **{cle: Sum(F(champ)) for cle, champ in (
            ('brut', '_brut'), ('remise', '_remise'), ('du', '_du'),
            ('paye', 'montant'), ('reste', '_reste'),
        )},
    }


def totaux_cantine(qs):
    return {cle: valeur or 0 for cle, valeur in montants_cantine_sql(qs).aggregate(**_agregats_cantine()).items()}


def resumes_periodicites_cantine(abonnements):
    """Un abonnement compte une fois, avec sa remise et son encaissement réels."""
    libelles = dict(AbonnementCantine.Periodicite.choices)
    rang = {code: index for index, code in enumerate(libelles)}
    if hasattr(abonnements, 'query'):
        rows = list(montants_cantine_sql(abonnements).order_by().values('periodicite').annotate(**_agregats_cantine()))
        for row in rows:
            row['code'] = row.pop('periodicite')
            row['label'] = libelles.get(row['code'], row['code'] or 'Non renseignée')
        return sorted(rows, key=lambda row: (rang.get(row['code'], len(rang)), row['label']))
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
