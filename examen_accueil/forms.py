from django import forms

from .models import MatiereAccueil, ParametreAccueil


class _BootstrapMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css = 'form-select' if isinstance(field.widget, forms.Select) else 'form-control'
            field.widget.attrs.setdefault('class', css)


class ParametreAccueilForm(_BootstrapMixin, forms.ModelForm):
    class Meta:
        model = ParametreAccueil
        fields = ['date_test', 'note_sur', 'moyenne_admission', 'observations']
        widgets = {
            'date_test': forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d'),
            'observations': forms.Textarea(attrs={'rows': 2}),
        }


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
