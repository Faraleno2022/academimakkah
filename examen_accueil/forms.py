from django import forms

from .models import CandidatAccueil, ClasseAccueil, MatiereAccueil


class _BootstrapMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = 'form-select' if isinstance(field.widget, forms.Select) else 'form-control'
            field.widget.attrs.setdefault('class', css)


class ClasseAccueilForm(_BootstrapMixin, forms.ModelForm):
    class Meta:
        model = ClasseAccueil
        fields = ['nom', 'niveau_vise', 'annee_scolaire', 'date_test', 'note_sur', 'moyenne_admission', 'observations']
        widgets = {
            'date_test': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'observations': forms.Textarea(attrs={'rows': 2}),
        }

    def __init__(self, *args, ecole=None, **kwargs):
        self.ecole = ecole
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        nom, annee = cleaned.get('nom'), cleaned.get('annee_scolaire')
        if self.ecole and nom and annee:
            doublon = ClasseAccueil.objects.filter(ecole=self.ecole, nom__iexact=nom.strip(), annee_scolaire=annee)
            if self.instance.pk:
                doublon = doublon.exclude(pk=self.instance.pk)
            if doublon.exists():
                self.add_error('nom', "Une classe de test porte déjà ce nom pour cette année scolaire.")
        return cleaned

    def _post_clean(self):
        # La moyenne d'admission est comparée au barème par ClasseAccueil.clean().
        if self.ecole is not None:
            self.instance.ecole = self.ecole
        super()._post_clean()


class MatiereAccueilForm(_BootstrapMixin, forms.ModelForm):
    class Meta:
        model = MatiereAccueil
        fields = ['nom', 'coefficient', 'ordre']

    def __init__(self, *args, classe=None, **kwargs):
        self.classe = classe
        super().__init__(*args, **kwargs)

    def clean_nom(self):
        nom = self.cleaned_data['nom'].strip()
        if self.classe is not None:
            doublon = MatiereAccueil.objects.filter(classe=self.classe, nom__iexact=nom)
            if self.instance.pk:
                doublon = doublon.exclude(pk=self.instance.pk)
            if doublon.exists():
                raise forms.ValidationError("Cette matière existe déjà dans la classe.")
        return nom


class CandidatAccueilForm(_BootstrapMixin, forms.ModelForm):
    class Meta:
        model = CandidatAccueil
        fields = ['nom', 'prenom', 'sexe', 'date_naissance', 'lieu_naissance', 'ecole_origine', 'telephone']
        widgets = {'date_naissance': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')}

    def clean_nom(self):
        return self.cleaned_data['nom'].strip().upper()

    def clean_prenom(self):
        return self.cleaned_data['prenom'].strip()
