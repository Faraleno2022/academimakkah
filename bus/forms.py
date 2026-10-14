from decimal import Decimal, ROUND_HALF_UP

from django import forms

from eleves.models import Classe, Eleve
from paiements.models import ModePaiement
from paiements.referentiels import mode_paiement_par_defaut

from .models import (
    PERIODICITES_GRILLE, REMISE_ENREGISTREE, TRANCHE_DE_DEPART, AbonnementBus, AbonnementCantine,
    GrilleTarifaireBus, RemiseBus,
)
from .remises import PORTEE_PLAFOND, construire_remise, enregistrer_remise


class EleveAbonnementChoiceField(forms.ModelChoiceField):
    """Affiche le matricule avant le prénom et le nom dans les listes."""

    def label_from_instance(self, eleve):
        matricule = eleve.matricule or "Sans matricule"
        return f"{matricule} — {eleve.prenom} {eleve.nom}"


class ClasseAbonnementChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, classe):
        ecole = getattr(classe, 'ecole', None)
        return f"{classe.nom} — {ecole.nom}" if ecole else classe.nom


def _configurer_selection_eleve(form, ecole=None):
    classes = Classe.objects.select_related('ecole')
    eleves = Eleve.objects.select_related('classe', 'classe__ecole')
    if ecole:
        classes = classes.filter(ecole=ecole)
        eleves = eleves.filter(classe__ecole=ecole)
    form.fields['classe'].queryset = classes.order_by(
        'ecole__nom', 'nom', '-annee_scolaire'
    )
    form.fields['eleve'].queryset = eleves.order_by(
        'classe__nom', 'nom', 'prenom', 'matricule'
    )
    form.fields['classe'].empty_label = "Toutes les classes"
    form.fields['eleve'].empty_label = "Sélectionner un élève"

    eleve_initial = form.initial.get('eleve')
    if getattr(form.instance, 'pk', None) and getattr(form.instance, 'eleve_id', None):
        eleve_initial = form.instance.eleve
    if eleve_initial:
        if not isinstance(eleve_initial, Eleve):
            try:
                eleve_initial = eleves.get(pk=eleve_initial)
            except (Eleve.DoesNotExist, TypeError, ValueError):
                eleve_initial = None
        if eleve_initial and eleve_initial.classe_id:
            form.fields['classe'].initial = eleve_initial.classe_id
            form.initial.setdefault('classe', eleve_initial.classe_id)


class GrilleTarifaireBusForm(forms.ModelForm):
    class Meta:
        model = GrilleTarifaireBus
        fields = [
            'ecole', 'zone', 'annee_scolaire',
            'tranche_1', 'date_echeance_tranche_1',
            'tranche_2', 'date_echeance_tranche_2',
            'tranche_3', 'date_echeance_tranche_3', 'actif',
        ]
        widgets = {
            'ecole': forms.Select(attrs={'class': 'form-select'}),
            'zone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ex. Ratoma'}),
            'annee_scolaire': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '2026-2027'}),
            'tranche_1': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'tranche_2': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'tranche_3': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'date_echeance_tranche_1': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'date_echeance_tranche_2': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'date_echeance_tranche_3': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
            'actif': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }

    def __init__(self, *args, ecole=None, allow_school_choice=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.ecole_autorisee = ecole
        if ecole and not allow_school_choice:
            self.fields['ecole'].queryset = self.fields['ecole'].queryset.filter(pk=ecole.pk)
            self.fields['ecole'].initial = ecole
            self.fields['ecole'].widget = forms.HiddenInput()

    def clean_ecole(self):
        ecole = self.cleaned_data['ecole']
        if self.ecole_autorisee and ecole.pk != self.ecole_autorisee.pk:
            raise forms.ValidationError("Vous ne pouvez créer une grille que pour votre école.")
        return ecole

    def clean(self):
        cleaned = super().clean()
        montants = [cleaned.get(f'tranche_{numero}') or 0 for numero in (1, 2, 3)]
        if not any(montant > 0 for montant in montants):
            raise forms.ValidationError("Au moins une tranche doit avoir un montant supérieur à zéro.")
        echeances = [
            cleaned.get(f'date_echeance_tranche_{numero}') for numero in (1, 2, 3)
        ]
        dates_definies = [date_value for date_value in echeances if date_value]
        if dates_definies != sorted(dates_definies):
            raise forms.ValidationError("Les dates d'échéance doivent être dans l'ordre des tranches.")
        return cleaned


CASE_TRANCHE = forms.CheckboxInput(attrs={'class': 'form-check-input remise-champ'})


class AbonnementBusForm(forms.ModelForm):
    # Remise de l'élève sur la grille choisie : appliquée seulement si la caisse
    # la modifie (remise_modifiee=1) ; sinon la remise enregistrée est conservée.
    remise_type = forms.ChoiceField(
        choices=[('', 'Aucune remise')] + list(RemiseBus.TypeRemise.choices), required=False,
        widget=forms.Select(attrs={'class': 'form-select remise-champ'}), label="Remise",
    )
    remise_valeur = forms.DecimalField(
        required=False, min_value=Decimal('0'), max_digits=10, decimal_places=2, label="Valeur",
        widget=forms.NumberInput(attrs={'class': 'form-control remise-champ', 'step': 'any', 'min': 0}),
    )
    remise_motif = forms.ChoiceField(
        choices=[('', 'Choisir le motif')] + list(RemiseBus.MotifRemise.choices), required=False,
        widget=forms.Select(attrs={'class': 'form-select remise-champ'}), label="Motif",
    )
    remise_sur_tranche_1 = forms.BooleanField(required=False, initial=True, widget=CASE_TRANCHE, label="Tranche 1")
    remise_sur_tranche_2 = forms.BooleanField(required=False, initial=True, widget=CASE_TRANCHE, label="Tranche 2")
    remise_sur_tranche_3 = forms.BooleanField(required=False, initial=True, widget=CASE_TRANCHE, label="Tranche 3")
    remise_modifiee = forms.CharField(required=False, widget=forms.HiddenInput())

    classe = ClasseAbonnementChoiceField(
        queryset=Classe.objects.none(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'}),
        label="Filtrer par classe",
    )
    eleve = EleveAbonnementChoiceField(
        queryset=Eleve.objects.none(),
        widget=forms.Select(attrs={'class': 'form-select'}),
        label="Élève",
    )

    class Meta:
        model = AbonnementBus
        fields = [
            'classe', 'eleve', 'grille', 'periodicite', 'montant', 'date_debut',
            'mode_paiement', 'reference_externe', 'itineraire', 'point_arret',
            'contact_parent', 'observations',
        ]
        labels = {
            'itineraire': "Itinéraire",
            'point_arret': "Point d'arrêt",
            'contact_parent': "Contact parent",
        }
        widgets = {
            'itineraire': forms.TextInput(attrs={
                'class': 'form-control', 'list': 'itinerairesBus',
                'placeholder': 'Ex. Circuit A — Kipé / Cosa',
            }),
            'point_arret': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ex. Carrefour Kipé'}),
            'contact_parent': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+224XXXXXXXXX'}),
            'grille': forms.Select(attrs={'class': 'form-select'}),
            'periodicite': forms.Select(attrs={'class': 'form-select'}),
            'montant': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'date_debut': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'mode_paiement': forms.Select(attrs={'class': 'form-select'}),
            'reference_externe': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'N° reçu externe, transaction Mobile Money, chèque…',
            }),
            'observations': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

    def __init__(self, *args, ecole=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ecole_autorisee = ecole
        _configurer_selection_eleve(self, ecole)
        grilles = GrilleTarifaireBus.objects.filter(actif=True).select_related('ecole')
        if ecole:
            grilles = grilles.filter(ecole=ecole)
        if self.instance and self.instance.pk and self.instance.grille_id:
            grilles = GrilleTarifaireBus.objects.filter(
                pk__in=list(grilles.values_list('pk', flat=True)) + [self.instance.grille_id]
            ).select_related('ecole')
        self.fields['grille'].queryset = grilles.order_by('-annee_scolaire', 'zone')
        self.fields['grille'].empty_label = "Sélectionner la zone et l'année"
        self.fields['mode_paiement'].queryset = ModePaiement.objects.filter(actif=True).order_by('nom')
        if not self.instance.pk and not self.initial.get('mode_paiement'):
            self.fields['mode_paiement'].initial = mode_paiement_par_defaut()
        self.fields['date_debut'].label = "Date du paiement"
        self.fields['periodicite'].label = "Type de paiement / tranche"
        P = AbonnementBus.Periodicite
        self.fields['periodicite'].choices = [
            ("Une tranche", [
                (P.TRANCHE_1, "1ère tranche"),
                (P.TRANCHE_2, "2ème tranche"),
                (P.TRANCHE_3, "3ème tranche"),
            ]),
            ("Plusieurs tranches", [
                (P.T1_DEMI_T2, "Tranche 1 + ½ Tranche 2"),
                (P.T1_T2, "Tranche 1 + Tranche 2"),
                (P.T2_T3, "Tranche 2 + Tranche 3"),
                (P.T1_T2_DEMI_T3, "Tranche 1 + Tranche 2 + ½ Tranche 3"),
                (P.ANNUEL, "Tranche 1 + Tranche 2 + Tranche 3 (annuel)"),
            ]),
        ]
        if self.instance and self.instance.pk and self.instance.periodicite not in PERIODICITES_GRILLE:
            self.fields['periodicite'].choices = [
                (self.instance.periodicite, self.instance.get_periodicite_display()),
                *self.fields['periodicite'].choices,
            ]
        if self.instance and self.instance.pk and not self.instance.grille_id:
            self.fields['grille'].required = False
        if self.instance and self.instance.pk and not self.instance.mode_paiement_id:
            self.fields['mode_paiement'].required = False
        self.remise_cible = REMISE_ENREGISTREE
        if self.instance and self.instance.pk and self.instance.grille_id:
            existante = self.instance.grille.remise_pour(self.instance.eleve_id)
            if existante:
                self.initial.update({
                    'remise_type': existante.type_remise,
                    'remise_valeur': existante.valeur.normalize(),
                    'remise_motif': existante.motif,
                    **{f'remise_sur_tranche_{n}': getattr(existante, f'sur_tranche_{n}') for n in (1, 2, 3)},
                })

    def _remise_saisie(self, cleaned, eleve, grille):
        """Remise saisie dans le formulaire (non enregistrée), None pour « Aucune remise »."""
        type_remise = cleaned.get('remise_type')
        if not type_remise:
            return None
        valeur = cleaned.get('remise_valeur')
        tranches = [code for n, code in enumerate(('T1', 'T2', 'T3'), 1) if cleaned.get(f'remise_sur_tranche_{n}')]
        if not valeur or valeur <= 0:
            self.add_error('remise_valeur', "Indiquez la valeur de la remise.")
        elif type_remise == RemiseBus.TypeRemise.POURCENTAGE and valeur > 100:
            self.add_error('remise_valeur', "Un pourcentage ne peut pas dépasser 100 %.")
        if not cleaned.get('remise_motif'):
            self.add_error('remise_motif', "Indiquez le motif de la remise.")
        if not tranches:
            self.add_error('remise_sur_tranche_1', "Cochez au moins une tranche concernée par la remise.")
        return construire_remise(eleve, grille, type_remise, valeur, cleaned.get('remise_motif'), tranches)

    def enregistrer_remise(self, user):
        """À appeler après l'enregistrement du reçu : applique la remise modifiée par la caisse."""
        abo = self.instance
        if self.remise_cible is REMISE_ENREGISTREE or not abo.grille_id:
            return
        enregistrer_remise(abo.eleve, abo.grille, self.remise_cible, user)

    def clean(self):
        cleaned = super().clean()
        eleve = cleaned.get('eleve')
        classe = cleaned.get('classe')
        grille = cleaned.get('grille')
        periodicite = cleaned.get('periodicite')
        montant = cleaned.get('montant')
        if cleaned.get('remise_modifiee') == '1' and grille and eleve:
            self.remise_cible = self._remise_saisie(cleaned, eleve, grille)

        if montant is not None and montant <= 0:
            self.add_error('montant', "Le montant du paiement doit être supérieur à zéro.")

        if classe and eleve and eleve.classe_id != classe.pk:
            self.add_error('eleve', "L'élève sélectionné n'appartient pas à cette classe.")

        if not grille and not (self.instance and self.instance.pk):
            self.add_error('grille', "Sélectionnez une grille tarifaire bus.")
            return cleaned

        if grille and eleve:
            ecole_eleve_id = getattr(getattr(eleve, 'classe', None), 'ecole_id', None)
            if ecole_eleve_id != grille.ecole_id:
                self.add_error('eleve', "L'élève et la grille doivent appartenir à la même école.")
            if self.ecole_autorisee and grille.ecole_id != self.ecole_autorisee.pk:
                self.add_error('grille', "Cette grille n'appartient pas à votre école.")

        if grille and eleve and periodicite in PERIODICITES_GRILLE and montant is not None:
            situation = grille.situation_paiements(
                eleve,
                exclude_pk=self.instance.pk if self.instance and self.instance.pk else None,
                remise=self.remise_cible,
            )
            # L'excédent d'une tranche passe à la suivante : seul le reste des
            # tranches restantes (remise déduite) limite le montant.
            plafond = situation[periodicite]['plafond']
            if montant > plafond:
                portee = PORTEE_PLAFOND[TRANCHE_DE_DEPART[periodicite]]
                montant_plafond = f"{plafond:,.0f}".replace(',', ' ')
                self.add_error(
                    'montant',
                    f"Le montant dépasse le reste à payer {portee} ({montant_plafond} GNF, remise déduite).",
                )
        return cleaned


class AbonnementCantineForm(forms.ModelForm):
    annee_scolaire = forms.CharField(
        required=False, label="Année scolaire",
        widget=forms.Select(attrs={'class': 'form-select'}),
    )
    classe = ClasseAbonnementChoiceField(
        queryset=Classe.objects.none(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-select'}),
        label="Filtrer par classe",
    )
    eleve = EleveAbonnementChoiceField(
        queryset=Eleve.objects.none(),
        widget=forms.Select(attrs={'class': 'form-select'}),
        label="Élève",
    )

    class Meta:
        model = AbonnementCantine
        fields = [
            'classe', 'eleve', 'annee_scolaire', 'montant_tarif', 'type_remise', 'valeur_remise', 'montant',
            'periodicite', 'type_repas', 'date_debut',
            'statut', 'alerte_avant_jours', 'regime_alimentaire', 'allergies', 
            'contact_parent', 'reference_externe', 'observations'
        ]
        widgets = {
            'date_debut': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'montant_tarif': forms.NumberInput(attrs={'class': 'form-control', 'min': 0}),
            'type_remise': forms.Select(attrs={'class': 'form-control'}),
            'valeur_remise': forms.NumberInput(attrs={'class': 'form-control', 'min': 0, 'step': 'any'}),
            'montant': forms.NumberInput(attrs={'class': 'form-control', 'placeholder': 'Montant en GNF'}),
            'periodicite': forms.Select(attrs={'class': 'form-control'}),
            'type_repas': forms.Select(attrs={'class': 'form-control'}),
            'statut': forms.Select(attrs={'class': 'form-control'}),
            'alerte_avant_jours': forms.NumberInput(attrs={'class': 'form-control', 'value': 7}),
            'regime_alimentaire': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ex: Végétarien, Halal, etc.'}),
            'allergies': forms.Textarea(attrs={'class': 'form-control', 'rows': 3, 'placeholder': 'Indiquez les allergies alimentaires'}),
            'contact_parent': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+224XXXXXXXXX'}),
            'reference_externe': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'N° reçu externe, transaction Mobile Money, chèque…',
            }),
            'observations': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }

    def __init__(self, *args, ecole=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ecole_autorisee = ecole
        _configurer_selection_eleve(self, ecole)
        classes = Classe.objects.filter(ecole=ecole) if ecole else Classe.objects.all()
        self.annees_autorisees = set(classes.values_list('annee_scolaire', flat=True).distinct())
        if self.instance.pk and self.instance.annee_scolaire:
            self.annees_autorisees.add(self.instance.annee_scolaire)
        self.fields['annee_scolaire'].widget.choices = [('', "Année de l'élève")] + [
            (annee, annee) for annee in sorted(self.annees_autorisees, reverse=True)
        ]
        self.fields['montant_tarif'].required = False
        self.fields['valeur_remise'].required = False
        if self.instance.pk and self.instance.montant_tarif is None:
            self.initial['montant_tarif'] = self.instance.montant

    def clean(self):
        cleaned = super().clean()
        classe = cleaned.get('classe')
        eleve = cleaned.get('eleve')
        annee = cleaned.get('annee_scolaire')
        if not annee:
            annee = self.instance.annee_scolaire or getattr(getattr(eleve, 'classe', None), 'annee_scolaire', '')
            cleaned['annee_scolaire'] = annee
        if annee not in self.annees_autorisees:
            self.add_error('annee_scolaire', "Choisissez une année scolaire de cet établissement.")
        if classe and eleve and eleve.classe_id != classe.pk:
            self.add_error('eleve', "L'élève sélectionné n'appartient pas à cette classe.")
        if self.ecole_autorisee and eleve:
            ecole_eleve_id = getattr(getattr(eleve, 'classe', None), 'ecole_id', None)
            if ecole_eleve_id != self.ecole_autorisee.pk:
                self.add_error('eleve', "Cet élève n'appartient pas à votre école.")
        tarif = cleaned.get('montant_tarif')
        montant = cleaned.get('montant')
        if tarif is None and montant is not None:
            tarif = montant
            cleaned['montant_tarif'] = tarif
        if cleaned.get('valeur_remise') is None:
            cleaned['valeur_remise'] = Decimal('0')
        valeur = cleaned.get('valeur_remise') or Decimal('0')
        type_remise = cleaned.get('type_remise')
        if tarif is not None and tarif < 0:
            self.add_error('montant_tarif', "Le tarif doit être positif ou nul.")
        if montant is not None and montant < 0:
            self.add_error('montant', "Le montant encaissé doit être positif ou nul.")
        if valeur < 0:
            self.add_error('valeur_remise', "La remise doit être positive ou nulle.")
        if valeur > 0 and not type_remise:
            self.add_error('type_remise', "Choisissez le type de remise.")
        if type_remise and valeur <= 0:
            self.add_error('valeur_remise', "Indiquez une remise supérieure à zéro.")
        if type_remise == 'POURCENTAGE' and valeur > 100:
            self.add_error('valeur_remise', "La remise ne peut pas dépasser 100 %.")
        if tarif is not None and type_remise == 'MONTANT' and valeur > tarif:
            self.add_error('valeur_remise', "La remise ne peut pas dépasser le tarif.")
        if tarif is not None and montant is not None and not self.errors:
            remise = (tarif * valeur / Decimal('100')).quantize(Decimal('1'), rounding=ROUND_HALF_UP) if type_remise == 'POURCENTAGE' else (valeur if type_remise == 'MONTANT' else Decimal('0'))
            if montant > tarif - remise:
                self.add_error('montant', "Le montant encaissé dépasse le tarif après remise.")
        return cleaned

    def save(self, commit=True):
        from .periodes import fin_abonnement
        abonnement = super().save(commit=False)
        abonnement.date_expiration = fin_abonnement(abonnement.date_debut, abonnement.periodicite, 'cantine')
        if commit:
            abonnement.save()
            self.save_m2m()
        return abonnement


class RemiseBusForm(forms.ModelForm):
    """Remise d'un élève sur une grille bus, depuis la liste des remises."""

    eleve = EleveAbonnementChoiceField(
        queryset=Eleve.objects.none(), widget=forms.Select(attrs={'class': 'form-select'}), label="Élève",
    )

    class Meta:
        model = RemiseBus
        fields = [
            'eleve', 'grille', 'type_remise', 'valeur', 'motif',
            'sur_tranche_1', 'sur_tranche_2', 'sur_tranche_3', 'observations',
        ]
        widgets = {
            'grille': forms.Select(attrs={'class': 'form-select'}),
            'type_remise': forms.Select(attrs={'class': 'form-select'}),
            'valeur': forms.NumberInput(attrs={'class': 'form-control', 'step': 'any', 'min': 0}),
            'motif': forms.Select(attrs={'class': 'form-select'}),
            'sur_tranche_1': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'sur_tranche_2': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'sur_tranche_3': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'observations': forms.TextInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, ecole=None, **kwargs):
        super().__init__(*args, **kwargs)
        eleves = Eleve.objects.select_related('classe', 'classe__ecole')
        grilles = GrilleTarifaireBus.objects.select_related('ecole')
        if ecole:
            eleves = eleves.filter(classe__ecole=ecole)
            grilles = grilles.filter(ecole=ecole)
        self.fields['eleve'].queryset = eleves.order_by('classe__nom', 'nom', 'prenom', 'matricule')
        self.fields['eleve'].empty_label = "Sélectionner un élève"
        self.fields['grille'].queryset = grilles.order_by('-annee_scolaire', 'zone')
        self.fields['grille'].empty_label = "Sélectionner la zone et l'année"
        if self.instance.pk:
            # L'élève et la grille identifient la remise : on ne les change pas ici.
            self.fields['eleve'].disabled = True
            self.fields['grille'].disabled = True

    def clean(self):
        cleaned = super().clean()
        eleve, grille = cleaned.get('eleve'), cleaned.get('grille')
        valeur = cleaned.get('valeur')
        if valeur is not None and valeur <= 0:
            self.add_error('valeur', "Indiquez la valeur de la remise.")
        if (cleaned.get('type_remise') == RemiseBus.TypeRemise.POURCENTAGE
                and valeur is not None and valeur > 100):
            self.add_error('valeur', "Un pourcentage ne peut pas dépasser 100 %.")
        if not any(cleaned.get(f'sur_tranche_{n}') for n in (1, 2, 3)):
            self.add_error('sur_tranche_1', "Cochez au moins une tranche concernée par la remise.")
        if eleve and grille:
            if getattr(eleve.classe, 'ecole_id', None) != grille.ecole_id:
                self.add_error('eleve', "L'élève et la grille doivent appartenir à la même école.")
            elif not self.instance.pk and RemiseBus.objects.filter(eleve=eleve, grille=grille).exists():
                self.add_error('eleve', "Cet élève a déjà une remise sur cette grille : modifiez-la depuis la liste.")
        return cleaned
