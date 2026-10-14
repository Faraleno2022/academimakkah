from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from django.contrib.auth.models import User
from django.core.validators import MinValueValidator, RegexValidator
from django.db import IntegrityError, models
from django.utils import timezone
from eleves.models import Ecole, Eleve
from synchronisation.mixins import SyncTrackedModel


ANNEE_SCOLAIRE_VALIDATOR = RegexValidator(
    regex=r'^\d{4}-\d{4}$',
    message="L'année scolaire doit être au format AAAA-AAAA.",
)

DEMI = Decimal('0.5')

# Part de chaque tranche couverte par un type de paiement (1 = totalité, 0,5 = moitié).
PORTEES_PAIEMENT_BUS = {
    'ANNUEL': {'T1': 1, 'T2': 1, 'T3': 1},
    'T1': {'T1': 1},
    'T2': {'T2': 1},
    'T3': {'T3': 1},
    'T1_T2M': {'T1': 1, 'T2': DEMI},
    'T1_T2': {'T1': 1, 'T2': 1},
    'T2_T3': {'T2': 1, 'T3': 1},
    'T1_T2_T3M': {'T1': 1, 'T2': 1, 'T3': DEMI},
}
PERIODICITES_GRILLE = frozenset(PORTEES_PAIEMENT_BUS)
# Paiements portant sur plusieurs tranches, hors annuel (situation propre à chacun).
PERIODICITES_COMBINEES = frozenset(
    code for code, portee in PORTEES_PAIEMENT_BUS.items() if len(portee) > 1
) - {'ANNUEL'}
ORDRE_TRANCHES = ('T1', 'T2', 'T3')
LIBELLES_TRANCHES = {'T1': '1ère tranche', 'T2': '2ème tranche', 'T3': '3ème tranche'}
# Première tranche remplie par un paiement : l'excédent passe aux suivantes.
TRANCHE_DE_DEPART = {
    code: next(tranche for tranche in ORDRE_TRANCHES if tranche in portee)
    for code, portee in PORTEES_PAIEMENT_BUS.items()
}
# Valeur par défaut de situation_paiements : la remise enregistrée de l'élève.
REMISE_ENREGISTREE = object()


def tranches_couvertes(periodicite):
    """Codes des tranches concernées par un paiement (pour les reçus)."""
    return set(PORTEES_PAIEMENT_BUS.get(periodicite, {}))


class GrilleTarifaireBus(SyncTrackedModel):
    """Tarifs annuels du bus définis par une école et par zone."""

    ecole = models.ForeignKey(
        Ecole,
        on_delete=models.CASCADE,
        related_name='grilles_tarifaires_bus',
    )
    zone = models.CharField(
        max_length=100,
        verbose_name="Zone / circuit",
        help_text="Exemple : Ratoma, Sonfonia ou Circuit A.",
    )
    annee_scolaire = models.CharField(
        max_length=9,
        validators=[ANNEE_SCOLAIRE_VALIDATOR],
        db_index=True,
        verbose_name="Année scolaire",
    )
    tranche_1 = models.DecimalField(
        max_digits=10,
        decimal_places=0,
        validators=[MinValueValidator(Decimal('0'))],
        verbose_name="1ère tranche (GNF)",
    )
    tranche_2 = models.DecimalField(
        max_digits=10,
        decimal_places=0,
        validators=[MinValueValidator(Decimal('0'))],
        verbose_name="2ème tranche (GNF)",
    )
    tranche_3 = models.DecimalField(
        max_digits=10,
        decimal_places=0,
        validators=[MinValueValidator(Decimal('0'))],
        verbose_name="3ème tranche (GNF)",
    )
    date_echeance_tranche_1 = models.DateField(
        null=True, blank=True, verbose_name="Échéance de la 1ère tranche"
    )
    date_echeance_tranche_2 = models.DateField(
        null=True, blank=True, verbose_name="Échéance de la 2ème tranche"
    )
    date_echeance_tranche_3 = models.DateField(
        null=True, blank=True, verbose_name="Échéance de la 3ème tranche"
    )
    actif = models.BooleanField(default=True, db_index=True, verbose_name="Grille active")
    cree_par = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='grilles_bus_creees',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-annee_scolaire', 'zone']
        verbose_name = "Grille tarifaire bus"
        verbose_name_plural = "Grilles tarifaires bus"
        constraints = [
            models.UniqueConstraint(
                fields=['ecole', 'zone', 'annee_scolaire'],
                name='bus_grille_unique_ecole_zone_annee',
            ),
        ]
        indexes = [
            models.Index(fields=['ecole', 'annee_scolaire', 'actif']),
        ]

    def __str__(self):
        return f"{self.zone} - {self.annee_scolaire} ({self.ecole})"

    @property
    def montant_annuel(self):
        return (self.tranche_1 or 0) + (self.tranche_2 or 0) + (self.tranche_3 or 0)

    def tranches_brutes(self):
        """Montants de la grille, avant remise."""
        return {
            'T1': Decimal(self.tranche_1 or 0),
            'T2': Decimal(self.tranche_2 or 0),
            'T3': Decimal(self.tranche_3 or 0),
        }

    def _tranches(self, remise=None):
        """Montant dû de chaque tranche, remise de l'élève déduite."""
        bruts = self.tranches_brutes()
        remises = remise.remises_par_tranche(bruts) if remise else {}
        return {code: brut - remises.get(code, Decimal('0')) for code, brut in bruts.items()}

    def remise_pour(self, eleve):
        eleve_id = getattr(eleve, 'pk', eleve)
        return RemiseBus.objects.filter(eleve_id=eleve_id, grille=self).first()

    def parts_dues(self, periodicite, remise=None):
        """Montant dû de chaque tranche pour ce type de paiement (moitié arrondie au franc)."""
        tranches = self._tranches(remise)
        return {
            code: (tranches[code] * Decimal(fraction)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            for code, fraction in PORTEES_PAIEMENT_BUS.get(periodicite, {}).items()
        }

    def montant_pour(self, periodicite, remise=None):
        return sum(self.parts_dues(periodicite, remise).values(), Decimal('0'))

    def echeance_pour(self, periodicite):
        echeances = {
            'T1': self.date_echeance_tranche_1,
            'T2': self.date_echeance_tranche_2,
            'T3': self.date_echeance_tranche_3,
        }
        dates = [echeances[code] for code in PORTEES_PAIEMENT_BUS.get(periodicite, {}) if echeances[code]]
        return max(dates) if dates else None

    def situation_paiements(self, eleve, exclude_pk=None, remise=REMISE_ENREGISTREE):
        """Situation des tranches de l'élève ; ``remise`` permet un aperçu avant enregistrement."""
        versements = AbonnementBus.objects.filter(eleve=eleve, grille=self)
        if exclude_pk:
            versements = versements.exclude(pk=exclude_pk)
        directs = {
            row['periodicite']: Decimal(row['total'] or 0)
            for row in versements.values('periodicite').annotate(total=models.Sum('montant'))
        }
        if remise is REMISE_ENREGISTREE:
            remise = self.remise_pour(eleve)
        return self.situation_depuis_totaux(directs, remise)

    def situation_depuis_totaux(self, directs, remise=None):
        """Même calcul pour les reçus et les versements préchargés du tableau de bord.

        Chaque paiement remplit d'abord la première tranche qu'il couvre, puis
        l'excédent passe à la tranche suivante, et ainsi de suite. Les paiements
        qui commencent le plus tard (T3, puis T2 / T2 + T3) sont répartis en
        premier : ceux qui commencent en T1 comblent ensuite tout manque, sans
        double compte. Les montants dus tiennent compte de la remise.
        """
        bruts = self.tranches_brutes()
        tranches = self._tranches(remise)
        payes = {code: Decimal('0') for code in ORDRE_TRANCHES}
        for depart in reversed(ORDRE_TRANCHES):
            restant = sum(
                (Decimal(directs.get(code) or 0) for code, debut in TRANCHE_DE_DEPART.items() if debut == depart),
                Decimal('0'),
            )
            if depart == 'T1':
                restant += sum(
                    (Decimal(valeur or 0) for code, valeur in directs.items()
                     if code not in TRANCHE_DE_DEPART), Decimal('0'),
                )
            for code in ORDRE_TRANCHES[ORDRE_TRANCHES.index(depart):]:
                part = min(max(tranches[code] - payes[code], Decimal('0')), restant)
                payes[code] += part
                restant -= part
        situation = {
            code: {
                'brut': bruts[code],
                'remise': bruts[code] - tranches[code],
                'du': tranches[code],
                'paye': payes[code],
                'reste': max(tranches[code] - payes[code], Decimal('0')),
            }
            for code in ORDRE_TRANCHES
        }
        total_paye = sum((Decimal(valeur or 0) for valeur in directs.values()), Decimal('0'))
        du_annuel = sum(tranches.values(), Decimal('0'))
        brut_annuel = sum(bruts.values(), Decimal('0'))
        situation['ANNUEL'] = {
            'brut': brut_annuel,
            'remise': brut_annuel - du_annuel,
            'du': du_annuel,
            'paye': total_paye,
            'reste': max(du_annuel - total_paye, Decimal('0')),
        }
        for code in PERIODICITES_COMBINEES:
            parts = self.parts_dues(code, remise)
            du = sum(parts.values(), Decimal('0'))
            paye = sum(
                (min(situation[tranche]['paye'], part) for tranche, part in parts.items()),
                Decimal('0'),
            )
            situation[code] = {'du': du, 'paye': paye, 'reste': max(du - paye, Decimal('0'))}
        # Plafond d'un reçu : reste de ses tranches et des suivantes (report de l'excédent).
        for code, depart in TRANCHE_DE_DEPART.items():
            situation[code]['plafond'] = sum(
                (situation[tranche]['reste'] for tranche in ORDRE_TRANCHES[ORDRE_TRANCHES.index(depart):]),
                Decimal('0'),
            )
        return situation


class AbonnementBus(SyncTrackedModel):
    class Statut(models.TextChoices):
        ACTIF = 'ACTIF', 'Actif'
        EXPIRE = 'EXPIRE', 'Expiré'
        SUSPENDU = 'SUSPENDU', 'Suspendu'

    class Periodicite(models.TextChoices):
        MENSUEL = 'MENSUEL', 'Mensuel'
        ANNUEL = 'ANNUEL', 'Annuel'
        TRANCHE_1 = 'T1', "1ère Tranche"
        TRANCHE_2 = 'T2', "2ème Tranche"
        TRANCHE_3 = 'T3', "3ème Tranche"
        T1_DEMI_T2 = 'T1_T2M', "Tranche 1 + ½ Tranche 2"
        T1_T2 = 'T1_T2', "Tranche 1 + Tranche 2"
        T2_T3 = 'T2_T3', "Tranche 2 + Tranche 3"
        T1_T2_DEMI_T3 = 'T1_T2_T3M', "Tranche 1 + Tranche 2 + ½ Tranche 3"

    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='abonnements_bus')
    grille = models.ForeignKey(
        GrilleTarifaireBus,
        on_delete=models.SET_NULL,
        related_name='versements',
        null=True,
        blank=True,
        verbose_name="Grille tarifaire",
    )
    annee_scolaire = models.CharField(
        max_length=9,
        validators=[ANNEE_SCOLAIRE_VALIDATOR],
        blank=True,
        db_index=True,
        verbose_name="Année scolaire",
    )
    mode_paiement = models.ForeignKey(
        'paiements.ModePaiement',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='paiements_bus',
        verbose_name="Mode de paiement",
    )
    numero_recu = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        verbose_name="Numéro de reçu",
    )
    reference_externe = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Référence externe du paiement",
        help_text="Numéro du reçu externe, transaction Mobile Money, chèque, etc.",
    )
    montant = models.DecimalField(max_digits=10, decimal_places=0)
    periodicite = models.CharField(
        max_length=10,
        choices=Periodicite.choices,
        default=Periodicite.MENSUEL,
        verbose_name="Type de paiement / périodicité",
        help_text="Choisissez Annuel pour régler la totalité des trois tranches.",
    )
    date_debut = models.DateField(default=timezone.localdate)
    date_expiration = models.DateField(db_index=True)
    statut = models.CharField(max_length=10, choices=Statut.choices, default=Statut.ACTIF, db_index=True)

    # Alertes / relances
    alerte_avant_jours = models.PositiveIntegerField(default=7)
    derniere_relance = models.DateTimeField(null=True, blank=True)

    # Infos logistiques
    zone = models.CharField(max_length=100, blank=True)
    itineraire = models.CharField(max_length=200, blank=True)
    point_arret = models.CharField(max_length=150, blank=True)

    # Contact relance
    contact_parent = models.CharField(max_length=100, blank=True)

    observations = models.TextField(blank=True)

    cree_par = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='paiements_bus_crees',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Abonnement bus'
        verbose_name_plural = 'Abonnements bus'
        indexes = [
            models.Index(fields=['eleve', 'statut']),
            models.Index(fields=['eleve', 'date_expiration']),
            models.Index(fields=['statut', 'date_expiration']),
            models.Index(fields=['eleve', 'grille', 'periodicite']),
        ]

    def __str__(self):
        return f"Bus: {self.eleve} ({self.get_periodicite_display()})"

    def save(self, *args, **kwargs):
        champs_automatiques = set()
        if self.grille_id:
            self.annee_scolaire = self.grille.annee_scolaire
            champs_automatiques.add('annee_scolaire')
            if not self.zone:
                self.zone = self.grille.zone
                champs_automatiques.add('zone')
        if not self.annee_scolaire and self.eleve_id:
            from .annees import annee_depuis_date
            self.annee_scolaire = annee_depuis_date(self.date_debut)
            champs_automatiques.add('annee_scolaire')

        if not self.numero_recu:
            champs_automatiques.add('numero_recu')
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | champs_automatiques
            prefix = f"BUS{timezone.localdate().year}"
            for _ in range(10):
                dernier = (
                    AbonnementBus.objects
                    .filter(numero_recu__startswith=prefix)
                    .order_by('-numero_recu')
                    .first()
                )
                try:
                    sequence = int(dernier.numero_recu[-4:]) + 1 if dernier else 1
                except (TypeError, ValueError):
                    sequence = 1
                self.numero_recu = f"{prefix}{sequence:04d}"
                try:
                    return super().save(*args, **kwargs)
                except IntegrityError:
                    self.numero_recu = None
                    continue
            raise ValueError("Impossible de générer un numéro de reçu bus unique.")
        if kwargs.get('update_fields') is not None and champs_automatiques:
            kwargs['update_fields'] = set(kwargs['update_fields']) | champs_automatiques
        return super().save(*args, **kwargs)

    @property
    def est_proche_expiration(self) -> bool:
        if not self.date_expiration:
            return False
        today = timezone.localdate()
        delta = (self.date_expiration - today).days
        return 0 <= delta <= (self.alerte_avant_jours or 7)

    @property
    def est_expire(self) -> bool:
        if not self.date_expiration:
            return False
        return timezone.localdate() > self.date_expiration


class RemiseBus(SyncTrackedModel):
    """Remise accordée à un élève sur une grille bus (zone et année).

    Elle est déduite des tranches concernées : tous les montants dus (formulaire,
    reçus, carnet, liste du chauffeur) sont ceux de la grille, remise déduite.
    """

    class TypeRemise(models.TextChoices):
        POURCENTAGE = 'POURCENTAGE', 'Pourcentage (%)'
        MONTANT_FIXE = 'MONTANT_FIXE', 'Montant fixe (GNF)'

    class MotifRemise(models.TextChoices):
        FRATRIE = 'FRATRIE', 'Réduction fratrie'
        EMPLOYE = 'EMPLOYE', "Enfant d'employé"
        SOCIALE = 'SOCIALE', 'Réduction sociale'
        MERITE = 'MERITE', 'Réduction mérite'
        PARTENAIRE = 'PARTENAIRE', 'Partenaire'
        GESTE_COMMERCIAL = 'GESTE_COMMERCIAL', 'Geste commercial'
        NE_PAIE_RIEN = 'NE_PAIE_RIEN', 'Ne paie rien'
        AUTRE = 'AUTRE', 'Autre'

    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='remises_bus')
    grille = models.ForeignKey(
        GrilleTarifaireBus, on_delete=models.CASCADE, related_name='remises',
        verbose_name="Grille tarifaire",
    )
    type_remise = models.CharField(
        max_length=15, choices=TypeRemise.choices, default=TypeRemise.POURCENTAGE,
        verbose_name="Type de remise",
    )
    valeur = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0'))],
        verbose_name="Valeur", help_text="Pourcentage (ex : 25) ou montant en GNF.",
    )
    motif = models.CharField(max_length=20, choices=MotifRemise.choices, verbose_name="Motif")
    sur_tranche_1 = models.BooleanField(default=True, verbose_name="Sur la 1ère tranche")
    sur_tranche_2 = models.BooleanField(default=True, verbose_name="Sur la 2ème tranche")
    sur_tranche_3 = models.BooleanField(default=True, verbose_name="Sur la 3ème tranche")
    observations = models.CharField(max_length=255, blank=True, verbose_name="Observations")
    cree_par = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='remises_bus_creees',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']
        verbose_name = "Remise bus"
        verbose_name_plural = "Remises bus"
        constraints = [
            models.UniqueConstraint(fields=['eleve', 'grille'], name='bus_remise_unique_eleve_grille'),
        ]

    def __str__(self):
        return f"Remise bus {self.eleve} ({self.libelle_valeur})"

    @property
    def tranches(self):
        coches = (self.sur_tranche_1, self.sur_tranche_2, self.sur_tranche_3)
        return [code for code, coche in zip(ORDRE_TRANCHES, coches) if coche]

    def remises_par_tranche(self, bruts):
        """Remise de chaque tranche pour les montants bruts de la grille.

        Pourcentage : appliqué à chaque tranche concernée, arrondi au franc.
        Montant fixe : réparti au prorata des tranches concernées (méthode des
        plus forts restes), sans jamais dépasser leur montant.
        """
        zero = Decimal('0')
        remises = {code: zero for code in ORDRE_TRANCHES}
        portee = [code for code in self.tranches if bruts.get(code, zero) > 0]
        valeur = Decimal(self.valeur or 0)
        if valeur <= 0 or not portee:
            return remises
        if self.type_remise == self.TypeRemise.POURCENTAGE:
            taux = min(valeur, Decimal('100'))
            for code in portee:
                remises[code] = (bruts[code] * taux / Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            return remises
        total = sum((bruts[code] for code in portee), zero)
        montant = min(valeur.quantize(Decimal('1'), rounding=ROUND_HALF_UP), total)
        exacts = {code: montant * bruts[code] / total for code in portee}
        for code in portee:
            remises[code] = exacts[code].to_integral_value(rounding=ROUND_FLOOR)
        manque = int(montant - sum(remises.values(), zero))
        for code in sorted(portee, key=lambda c: exacts[c] - remises[c], reverse=True)[:manque]:
            remises[code] += 1
        return remises

    def montant_total(self, grille=None):
        grille = grille or self.grille
        return sum(self.remises_par_tranche(grille.tranches_brutes()).values(), Decimal('0'))

    @property
    def libelle_valeur(self):
        if self.type_remise == self.TypeRemise.POURCENTAGE:
            return f"{Decimal(self.valeur or 0).normalize():f} %".replace('.', ',')
        return f"{Decimal(self.valeur or 0):,.0f} GNF".replace(',', ' ')

    @property
    def libelle_tranches(self):
        if len(self.tranches) == 3:
            return "toutes les tranches"
        return ", ".join(LIBELLES_TRANCHES[code] for code in self.tranches)

    @property
    def libelle(self):
        """« 10 % — Réduction fratrie », suivi des tranches si elles ne sont pas toutes concernées."""
        libelle = f"{self.libelle_valeur} — {self.get_motif_display()}"
        if len(self.tranches) < 3:
            libelle += f" ({self.libelle_tranches})"
        return libelle


def remises_par_eleve_grille(paires):
    """Remises des couples (élève, grille), en une requête : {(eleve_id, grille_id): remise}."""
    paires = {(eleve_id, grille_id) for eleve_id, grille_id in paires if eleve_id and grille_id}
    if not paires:
        return {}
    remises = RemiseBus.objects.filter(
        eleve_id__in={e for e, _g in paires}, grille_id__in={g for _e, g in paires},
    )
    return {(r.eleve_id, r.grille_id): r for r in remises if (r.eleve_id, r.grille_id) in paires}


class AbonnementCantine(SyncTrackedModel):
    """Modèle pour gérer les abonnements à la cantine scolaire"""
    
    class Statut(models.TextChoices):
        ACTIF = 'ACTIF', 'Actif'
        EXPIRE = 'EXPIRE', 'Expiré'
        SUSPENDU = 'SUSPENDU', 'Suspendu'
    
    class Periodicite(models.TextChoices):
        JOURNALIER = 'JOURNALIER', 'Journalier'
        HEBDOMADAIRE = 'HEBDOMADAIRE', 'Hebdomadaire'
        MENSUEL = 'MENSUEL', 'Mensuel'
        TRIMESTRIEL = 'TRIMESTRIEL', 'Trimestriel'
        DEUX_TRIMESTRES = 'DEUX_TRIMESTRES', '2 trimestres'
        TROIS_TRIMESTRES = 'TROIS_TRIMESTRES', '3 trimestres'
        ANNUEL = 'ANNUEL', 'Annuel'
    
    class TypeRepas(models.TextChoices):
        DEJEUNER = 'DEJEUNER', 'Déjeuner uniquement'
        GOUTER = 'GOUTER', 'Goûter uniquement'
        COMPLET = 'COMPLET', 'Déjeuner + Goûter'
        REPAS_10H = 'REPAS_10H', 'Repas de 10 h'
        REPAS_14H = 'REPAS_14H', 'Repas de 14 h'
        REPAS_10H_14H = 'REPAS_10H_14H', 'Repas de 10 h + 14 h'
    
    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='abonnements_cantine')
    annee_scolaire = models.CharField(
        max_length=9, validators=[ANNEE_SCOLAIRE_VALIDATOR], blank=True,
        db_index=True, verbose_name="Année scolaire",
    )
    montant = models.DecimalField(max_digits=10, decimal_places=0, verbose_name="Montant encaissé (GNF)")
    montant_tarif = models.DecimalField(max_digits=10, decimal_places=0, null=True, blank=True,
                                       verbose_name="Tarif avant remise (GNF)")
    type_remise = models.CharField(max_length=12, choices=[('POURCENTAGE', 'Pourcentage'), ('MONTANT', 'Montant fixe')],
                                   blank=True, verbose_name="Type de remise")
    valeur_remise = models.DecimalField(max_digits=10, decimal_places=2, default=0,
                                        verbose_name="Valeur de la remise")
    reference_externe = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Référence externe du paiement",
        help_text="Numéro du reçu externe, transaction Mobile Money, chèque, etc.",
    )
    periodicite = models.CharField(
        max_length=20,
        choices=Periodicite.choices,
        default=Periodicite.MENSUEL,
        verbose_name="Type de paiement / périodicité",
    )
    type_repas = models.CharField(
        max_length=20,
        choices=TypeRepas.choices,
        default=TypeRepas.DEJEUNER,
        verbose_name="Type ou horaire du repas",
        help_text="Créneau de repas choisi pour l'élève.",
    )
    
    date_debut = models.DateField(default=timezone.localdate, verbose_name="Date de début")
    date_expiration = models.DateField(db_index=True, verbose_name="Date d'expiration")
    statut = models.CharField(max_length=10, choices=Statut.choices, default=Statut.ACTIF, db_index=True)
    
    # Alertes / relances
    alerte_avant_jours = models.PositiveIntegerField(default=7, verbose_name="Alerte avant (jours)")
    derniere_relance = models.DateTimeField(null=True, blank=True, verbose_name="Dernière relance")
    
    # Régime alimentaire et allergies
    regime_alimentaire = models.CharField(
        max_length=100, 
        blank=True, 
        verbose_name="Régime alimentaire",
        help_text="Ex: Végétarien, Sans porc, Halal, etc."
    )
    allergies = models.TextField(blank=True, verbose_name="Allergies alimentaires")
    
    # Contact relance
    contact_parent = models.CharField(max_length=100, blank=True, verbose_name="Contact parent")
    
    observations = models.TextField(blank=True, verbose_name="Observations")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-updated_at']
        verbose_name = 'Abonnement cantine'
        verbose_name_plural = 'Abonnements cantine'
        indexes = [
            models.Index(fields=['eleve', 'statut']),
            models.Index(fields=['eleve', 'date_expiration']),
            models.Index(fields=['statut', 'date_expiration']),
        ]
    
    def __str__(self):
        return f"Cantine: {self.eleve} ({self.get_periodicite_display()})"

    def save(self, *args, **kwargs):
        if not self.annee_scolaire:
            from .annees import annee_depuis_date
            self.annee_scolaire = annee_depuis_date(self.date_debut)
            if kwargs.get('update_fields') is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'annee_scolaire'}
        return super().save(*args, **kwargs)

    @property
    def montant_brut(self):
        return Decimal(self.montant_tarif if self.montant_tarif is not None else self.montant or 0)

    @property
    def montant_remise(self):
        brut = self.montant_brut
        valeur = max(Decimal(self.valeur_remise or 0), Decimal('0'))
        if self.type_remise == 'POURCENTAGE':
            valeur = (brut * min(valeur, Decimal('100')) / Decimal('100')).quantize(
                Decimal('1'), rounding=ROUND_HALF_UP)
        elif self.type_remise != 'MONTANT':
            valeur = Decimal('0')
        return min(brut, valeur.quantize(Decimal('1'), rounding=ROUND_HALF_UP))

    @property
    def montant_du(self):
        return self.montant_brut - self.montant_remise

    @property
    def deja_paye(self):
        """Le montant du reçu cantine est l'encaissement réel de l'élève."""
        return Decimal(self.montant or 0)

    @property
    def reste_a_payer(self):
        return max(self.montant_du - self.deja_paye, Decimal('0'))

    @property
    def solde(self):
        return self.montant_du - self.deja_paye
    
    @property
    def est_proche_expiration(self) -> bool:
        """Vérifie si l'abonnement est proche de l'expiration"""
        if not self.date_expiration:
            return False
        today = timezone.localdate()
        delta = (self.date_expiration - today).days
        return 0 <= delta <= (self.alerte_avant_jours or 7)
    
    @property
    def est_expire(self) -> bool:
        """Vérifie si l'abonnement est expiré"""
        if not self.date_expiration:
            return False
        return timezone.localdate() > self.date_expiration
    
    @property
    def jours_restants(self) -> int:
        """Retourne le nombre de jours restants avant expiration"""
        if not self.date_expiration:
            return 0
        today = timezone.localdate()
        delta = (self.date_expiration - today).days
        return max(0, delta)
