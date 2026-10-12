from django import forms
from decimal import Decimal
from .models import RemiseReduction, PaiementRemise


class PaiementRemiseForm(forms.Form):
    """Formulaire pour appliquer des remises à un paiement"""

    TRANCHE_CHOICES = [
        ('1', '1ère tranche'),
        ('2', '2ème tranche'),
        ('3', '3ème tranche'),
    ]

    BASE_CALCUL_CHOICES = [
        ('paiement_echeance', "Paiement à l'échéance"),
        ('tranches_dues', 'Montant des postes sélectionnés'),
    ]

    remises = forms.ModelMultipleChoiceField(
        queryset=RemiseReduction.objects.filter(actif=True),
        widget=forms.CheckboxSelectMultiple(attrs={
            'class': 'form-check-input'
        }),
        required=False,
        label="Remises disponibles"
    )

    montant_original = forms.DecimalField(
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'readonly': True
        }),
        label="Montant original"
    )

    deduire_du_montant = forms.BooleanField(
        required=False,
        label="Le montant indiqué est le tarif avant remise",
        help_text="À cocher uniquement si ce montant n'a pas encore été encaissé. "
                  "La remise sera déduite pour obtenir le net à encaisser.",
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )

    # Nouveau: pourcentage scolarité sélectionnable par l'utilisateur (1 à 10%)
    POURCENT_CHOICES = [("", "— Choisir —")] + [(str(i), f"{i}%") for i in range(1, 101)]
    pourcentage_scolarite = forms.ChoiceField(
        choices=POURCENT_CHOICES,
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'}),
        label="Remise scolarité (%)"
    )

    inclure_inscription = forms.BooleanField(
        required=False,
        label="Inclure les frais d'inscription / réinscription",
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input tranche-checkbox'}),
    )

    # Tranches concernées, auxquelles l'école peut autoriser l'ajout de l'admission.
    tranches = forms.MultipleChoiceField(
        choices=TRANCHE_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'form-check-input'}),
        label="Tranches concernées"
    )

    # Base sur laquelle le pourcentage/montant de remise est calculé
    base_calcul = forms.ChoiceField(
        choices=BASE_CALCUL_CHOICES,
        required=False,
        initial='paiement_echeance',
        widget=forms.RadioSelect,
        label="Base de calcul"
    )

    # Motif obligatoire dès qu'une remise est appliquée
    motif = forms.ChoiceField(
        choices=[("", "— Choisir —")] + RemiseReduction.MOTIF_CHOICES,
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'}),
        label="Motif de la remise"
    )

    def __init__(self, *args, **kwargs):
        paiement = kwargs.pop('paiement', None)
        super().__init__(*args, **kwargs)

        if paiement:
            self.remise_inscription_autorisee = paiement.eleve.classe.ecole.autoriser_remise_inscription
            self.fields['inclure_inscription'].initial = any(
                (ligne.regle_calcul or {}).get('inclure_inscription') for ligne in paiement.remises.all()
            )
            self.fields['montant_original'].initial = paiement.montant
            self.fields['deduire_du_montant'].initial = any(
                'montant_avant_remise' in (ligne.regle_calcul or {})
                for ligne in paiement.remises.all()
            )
            # Filtrer les remises valides à la date du paiement
            today = paiement.date_paiement
            self.fields['remises'].queryset = RemiseReduction.objects.filter(
                actif=True,
                date_debut__lte=today,
                date_fin__gte=today
            )
        else:
            self.remise_inscription_autorisee = False

    def clean(self):
        cleaned_data = super().clean()
        remises = cleaned_data.get('remises') or []
        pct_str = cleaned_data.get('pourcentage_scolarite') or ''
        motif = cleaned_data.get('motif') or ''
        tranches = cleaned_data.get('tranches') or []
        inclure_inscription = cleaned_data.get('inclure_inscription', False)
        if inclure_inscription and not self.remise_inscription_autorisee:
            self.add_error('inclure_inscription', "Cette école n'autorise pas les remises sur l'inscription et la réinscription.")
        pct_value = int(pct_str) if pct_str else {
            'NE_PAIE_RIEN': 100, 'MOITIE': 50,
        }.get(motif, 0)
        applying_remise = bool(remises) or pct_value > 0
        if applying_remise:
            if not tranches and not inclure_inscription:
                self.add_error('tranches', "Sélectionnez au moins une tranche ou les frais d'inscription / réinscription.")
            if not motif:
                self.add_error('motif', "Le motif de la remise est obligatoire.")
        # Le pourcentage réutilise une remise du catalogue : une sélection
        # simultanée tenterait de l'associer deux fois au même paiement.
        if pct_value > 0 and any(
            remise.nom == f"Remise scolarité {pct_value}%"
            and remise.type_remise == 'POURCENTAGE'
            and remise.valeur == pct_value
            and remise.motif == motif
            for remise in remises
        ):
            self.add_error(
                'pourcentage_scolarite',
                "Cette remise est déjà cochée dans les remises disponibles. "
                "Décochez-la pour utiliser le pourcentage ou le raccourci du motif.",
            )
        return cleaned_data
    
    def calculate_total_remise(self, montant_base):
        """Calcule le montant total des remises sélectionnées"""
        remises = self.cleaned_data.get('remises', [])
        total_remise = Decimal('0')
        
        for remise in remises:
            montant_remise = remise.calculer_remise(montant_base)
            total_remise += montant_remise
        
        return min(total_remise, montant_base)  # La remise ne peut pas dépasser le montant
    
    def get_remises_details(self, montant_base):
        """Retourne les détails de chaque remise appliquée"""
        remises = self.cleaned_data.get('remises', [])
        details = []
        
        for remise in remises:
            montant_remise = remise.calculer_remise(montant_base)
            details.append({
                'remise': remise,
                'montant': montant_remise,
                'description': f"{remise.nom} - {montant_remise:,.0f} GNF".replace(',', ' ')
            })
        
        return details


class CalculateurRemiseForm(forms.Form):
    """Formulaire pour calculer les remises en temps réel"""
    
    montant = forms.DecimalField(
        widget=forms.NumberInput(attrs={
            'class': 'form-control',
            'placeholder': 'Montant en GNF',
            'min': '0',
            'step': '1000'
        }),
        label="Montant du paiement"
    )
    
    remise_id = forms.ModelChoiceField(
        queryset=RemiseReduction.objects.filter(actif=True),
        widget=forms.Select(attrs={
            'class': 'form-select'
        }),
        required=False,
        empty_label="Sélectionner une remise",
        label="Remise à appliquer"
    )
    
    def calculate_remise_preview(self):
        """Calcule un aperçu de la remise"""
        if not self.is_valid():
            return None
            
        montant = self.cleaned_data.get('montant')
        remise = self.cleaned_data.get('remise_id')
        
        if not montant or not remise:
            return None
            
        montant_remise = remise.calculer_remise(montant)
        montant_final = montant - montant_remise
        
        return {
            'montant_original': montant,
            'montant_remise': montant_remise,
            'montant_final': montant_final,
            'pourcentage_remise': (montant_remise / montant * 100) if montant > 0 else 0,
            'remise_nom': remise.nom,
            'remise_type': remise.get_type_remise_display()
        }
