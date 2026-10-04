from decimal import ROUND_HALF_UP, Decimal

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
    'T1_T2_T3M': {'T1': 1, 'T2': 1, 'T3': DEMI},
}
PERIODICITES_GRILLE = frozenset(PORTEES_PAIEMENT_BUS)
# Paiements répartis sur T1, puis T2, puis T3 (comme l'annuel).
PERIODICITES_CUMULEES = frozenset({'ANNUEL', 'T1_T2M', 'T1_T2', 'T1_T2_T3M'})


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

    def _tranches(self):
        return {
            'T1': Decimal(self.tranche_1 or 0),
            'T2': Decimal(self.tranche_2 or 0),
            'T3': Decimal(self.tranche_3 or 0),
        }

    def parts_dues(self, periodicite):
        """Montant dû de chaque tranche pour ce type de paiement (moitié arrondie au franc)."""
        tranches = self._tranches()
        return {
            code: (tranches[code] * Decimal(fraction)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
            for code, fraction in PORTEES_PAIEMENT_BUS.get(periodicite, {}).items()
        }

    def montant_pour(self, periodicite):
        return sum(self.parts_dues(periodicite).values(), Decimal('0'))

    def echeance_pour(self, periodicite):
        echeances = {
            'T1': self.date_echeance_tranche_1,
            'T2': self.date_echeance_tranche_2,
            'T3': self.date_echeance_tranche_3,
        }
        dates = [echeances[code] for code in PORTEES_PAIEMENT_BUS.get(periodicite, {}) if echeances[code]]
        return max(dates) if dates else None

    def situation_paiements(self, eleve, exclude_pk=None):
        """Répartit un paiement annuel sur T1, T2 puis T3 sans double compte."""
        versements = AbonnementBus.objects.filter(eleve=eleve, grille=self)
        if exclude_pk:
            versements = versements.exclude(pk=exclude_pk)
        directs = {
            row['periodicite']: Decimal(row['total'] or 0)
            for row in versements.values('periodicite').annotate(total=models.Sum('montant'))
        }
        return self.situation_depuis_totaux(directs)

    def situation_depuis_totaux(self, directs):
        """Même calcul pour les reçus et les versements préchargés du tableau de bord.

        Les paiements annuels et combinés (T1 + ½ T2, T1 + T2...) remplissent
        T1, puis T2, puis T3, sans double compte avec les paiements directs.
        """
        cumul_restant = sum(
            (Decimal(directs.get(code) or 0) for code in PERIODICITES_CUMULEES), Decimal('0')
        )
        situation = {}
        for code, montant_du in self._tranches().items():
            paiement_direct = Decimal(directs.get(code) or 0)
            manque = max(montant_du - paiement_direct, Decimal('0'))
            part_cumulee = min(manque, cumul_restant)
            cumul_restant -= part_cumulee
            montant_paye = paiement_direct + part_cumulee
            situation[code] = {
                'du': montant_du,
                'paye': montant_paye,
                'reste': max(montant_du - montant_paye, Decimal('0')),
            }
        total_paye = sum(
            (Decimal(directs.get(code) or 0) for code in PERIODICITES_GRILLE), Decimal('0')
        )
        situation['ANNUEL'] = {
            'du': Decimal(self.montant_annuel or 0),
            'paye': total_paye,
            'reste': max(Decimal(self.montant_annuel or 0) - total_paye, Decimal('0')),
        }
        for code in PERIODICITES_CUMULEES - {'ANNUEL'}:
            parts = self.parts_dues(code)
            du = sum(parts.values(), Decimal('0'))
            paye = sum(
                (min(situation[tranche]['paye'], part) for tranche, part in parts.items()),
                Decimal('0'),
            )
            situation[code] = {'du': du, 'paye': paye, 'reste': max(du - paye, Decimal('0'))}
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
            self.annee_scolaire = (
                getattr(getattr(self.eleve, 'classe', None), 'annee_scolaire', '') or ''
            )
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
        ANNUEL = 'ANNUEL', 'Annuel'
    
    class TypeRepas(models.TextChoices):
        DEJEUNER = 'DEJEUNER', 'Déjeuner uniquement'
        GOUTER = 'GOUTER', 'Goûter uniquement'
        COMPLET = 'COMPLET', 'Déjeuner + Goûter'
        REPAS_10H = 'REPAS_10H', 'Repas de 10 h'
        REPAS_14H = 'REPAS_14H', 'Repas de 14 h'
        REPAS_10H_14H = 'REPAS_10H_14H', 'Repas de 10 h + 14 h'
    
    eleve = models.ForeignKey(Eleve, on_delete=models.CASCADE, related_name='abonnements_cantine')
    montant = models.DecimalField(max_digits=10, decimal_places=0, verbose_name="Montant (GNF)")
    reference_externe = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Référence externe du paiement",
        help_text="Numéro du reçu externe, transaction Mobile Money, chèque, etc.",
    )
    periodicite = models.CharField(
        max_length=15,
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
