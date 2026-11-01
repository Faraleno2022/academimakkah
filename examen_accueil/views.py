"""Vues du module Test d'accueil.

Organisation calquée sur le module Notes : classes, matières, élèves, saisie
des notes, consultation et bulletins. Les classes et les élèves sont ceux de
l'école (application ``eleves``) ; un utilisateur ne voit que ceux de son
école. Les matières, notes et moyennes restent propres au module.
"""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from eleves.models import Classe, Eleve
from eleves.suivi_listes import annoter_accueil, classes_visibles
from utilisateurs.permissions import can_manage_notes
from utilisateurs.utils import filter_by_user_school

from . import exports
from .calculs import calculer_resultats, statistiques
from .forms import MatiereAccueilForm, ParametreAccueilForm
from .models import MatiereAccueil, NoteAccueil, ParametreAccueil

SECTIONS = [
    ('tableau_bord', 'Accueil', 'fas fa-th-large'),
    ('classes', 'Classes', 'fas fa-school'),
    ('matieres', 'Matières', 'fas fa-book-open'),
    ('eleves', 'Élèves', 'fas fa-user-graduate'),
    ('saisie_notes', 'Saisie des notes', 'fas fa-edit'),
    ('consultation', 'Consultation', 'fas fa-eye'),
    ('bulletins', 'Bulletins', 'fas fa-file-alt'),
]


# ---------------------------------------------------------------- utilitaires

def _toutes_classes(user):
    """Classes de l'école de l'utilisateur, toutes années confondues."""
    return filter_by_user_school(Classe.objects.select_related('ecole'), user, 'ecole')


def _classe(request, classe_id):
    return get_object_or_404(_toutes_classes(request.user), pk=classe_id)


def _classe_choisie(request):
    """Classe passée en ``?classe=`` (ou ``None``)."""
    try:
        classe_id = int(request.GET.get('classe') or request.POST.get('classe') or 0)
    except (TypeError, ValueError):
        return None
    return _toutes_classes(request.user).filter(pk=classe_id).first() if classe_id else None


def _url_section(nom, classe, **params):
    url = reverse(f'examen_accueil:{nom}')
    if classe is not None:
        params = {'classe': classe.pk, **params}
    requete = '&'.join(f'{cle}={valeur}' for cle, valeur in params.items() if valeur not in (None, ''))
    return f'{url}?{requete}' if requete else url


def _contexte(request, section, classe=None, **extra):
    return {
        'section': section,
        'sections': [{'nom': nom, 'titre': titre, 'icone': icone, 'url': _url_section(nom, classe)}
                     for nom, titre, icone in SECTIONS],
        'classes': classes_visibles(request),
        'classe': classe,
        'parametre': ParametreAccueil.pour(classe) if classe is not None else None,
        **extra,
    }


def _reponse_fichier(contenu, nom, type_mime, en_ligne=False):
    reponse = HttpResponse(contenu, content_type=type_mime)
    disposition = 'inline' if en_ligne else 'attachment'
    reponse['Content-Disposition'] = f'{disposition}; filename="{nom}"'
    return reponse


def eleves_a_evaluer(classe, tous=False):
    """Élèves de la classe proposés au test.

    Par défaut : les nouveaux inscrits (concernés par le test d'accueil, comme
    dans la liste des élèves) et ceux qui ont déjà une note ; ``tous`` ajoute
    les réinscrits.
    """
    eleves = annoter_accueil(Eleve.pedagogiques.filter(classe=classe))
    if not tous:
        deja_notes = NoteAccueil.objects.filter(matiere__classe=classe).values('eleve_id')
        eleves = eleves.filter(Q(accueil_concerne=True) | Q(pk__in=deja_notes))
    return eleves.order_by('nom', 'prenom')


def classe_notes_correspondante(classe):
    """Classe du module Notes portant le même nom (même année de préférence)."""
    from notes.classes_utils import normaliser_nom_classe
    from notes.models import ClasseNote

    cible = normaliser_nom_classe(classe.nom)
    candidates = [c for c in ClasseNote.objects.filter(ecole_id=classe.ecole_id)
                  if normaliser_nom_classe(c.nom) == cible]
    candidates.sort(key=lambda c: c.annee_scolaire, reverse=True)
    candidates.sort(key=lambda c: c.annee_scolaire != classe.annee_scolaire)
    return candidates[0] if candidates else None


# ------------------------------------------------------------ tableau de bord

@can_manage_notes
def tableau_bord(request):
    classes = classes_visibles(request)
    resume = classes.aggregate(
        nb_classes=Count('id', distinct=True),
        nb_classes_pretes=Count('id', filter=Q(matieres_test_accueil__isnull=False), distinct=True),
        nb_evalues=Count('matieres_test_accueil__notes__eleve', distinct=True),
    )
    return render(request, 'examen_accueil/tableau_bord.html', _contexte(request, 'tableau_bord', resume=resume))


# -------------------------------------------------------------------- classes

@can_manage_notes
def classes(request):
    lignes = classes_visibles(request).select_related('parametre_test_accueil').annotate(
        nb_eleves=Count('eleves', filter=Q(eleves__import_verrouille=False), distinct=True),
        nb_matieres=Count('matieres_test_accueil', distinct=True),
        nb_evalues=Count('matieres_test_accueil__notes__eleve', distinct=True),
    )
    lignes = [{'classe': c, 'parametre': getattr(c, 'parametre_test_accueil', None) or ParametreAccueil.defaut(c)}
              for c in lignes]
    return render(request, 'examen_accueil/classes.html', _contexte(request, 'classes', lignes=lignes))


@can_manage_notes
def parametres_classe(request, classe_id):
    classe = _classe(request, classe_id)
    parametre = ParametreAccueil.pour(classe)
    form = ParametreAccueilForm(request.POST or None, instance=parametre)
    if request.method == 'POST' and form.is_valid():
        note_sur = form.cleaned_data['note_sur']
        if NoteAccueil.objects.filter(matiere__classe=classe, note__gt=note_sur).exists():
            form.add_error('note_sur', f"Des notes déjà saisies dépassent {note_sur} : corrigez-les d'abord.")
        else:
            parametre = form.save(commit=False)
            parametre.classe = classe
            if parametre.pk is None:
                parametre.cree_par = request.user
            parametre.save()
            messages.success(request, f"Paramètres du test d'accueil de {classe.nom} enregistrés.")
            return redirect('examen_accueil:classes')
    return render(request, 'examen_accueil/formulaire.html', _contexte(
        request, 'classes', classe, form=form,
        titre=f"Paramètres du test d'accueil - {classe.nom} ({parametre.libelle_niveau})",
        retour=reverse('examen_accueil:classes')))


# ------------------------------------------------------------------- matières

@can_manage_notes
def matieres(request):
    classe = _classe_choisie(request)
    form = None
    if classe is not None:
        form = MatiereAccueilForm(classe=classe, initial={'ordre': classe.matieres_test_accueil.count() + 1})
        if request.method == 'POST':
            if 'reprendre_notes' in request.POST:
                return _reprendre_matieres_notes(request, classe)
            form = MatiereAccueilForm(request.POST, classe=classe)
            if form.is_valid():
                matiere = form.save(commit=False)
                matiere.classe = classe
                matiere.save()
                messages.success(request, f"Matière « {matiere.nom} » ajoutée.")
                return redirect(_url_section('matieres', classe))
    liste = list(MatiereAccueil.objects.filter(classe=classe).annotate(nb_notes=Count('notes'))) if classe else []
    return render(request, 'examen_accueil/matieres.html', _contexte(
        request, 'matieres', classe, form=form, matieres=liste,
        total_coefficients=sum((m.coefficient for m in liste), Decimal('0')),
        classe_notes=classe_notes_correspondante(classe) if classe else None))


def _reprendre_matieres_notes(request, classe):
    """Copie (sans lien) les matières et coefficients de la classe du module Notes."""
    classe_notes = classe_notes_correspondante(classe)
    if classe_notes is None:
        messages.warning(request, "Aucune classe du module Notes ne correspond à cette classe.")
        return redirect(_url_section('matieres', classe))
    existantes = {nom.lower() for nom in classe.matieres_test_accueil.values_list('nom', flat=True)}
    ordre = classe.matieres_test_accueil.count()
    ajoutees = 0
    for matiere in classe_notes.matieres.filter(actif=True).order_by('nom'):
        if matiere.nom.strip().lower() in existantes:
            continue
        ordre += 1
        MatiereAccueil.objects.create(classe=classe, nom=matiere.nom.strip(),
                                      coefficient=matiere.coefficient or Decimal('1'), ordre=ordre)
        existantes.add(matiere.nom.strip().lower())
        ajoutees += 1
    if ajoutees:
        messages.success(request, f"{ajoutees} matière(s) reprise(s) du module Notes. Ajustez les coefficients si besoin.")
    else:
        messages.info(request, "Toutes les matières du module Notes sont déjà présentes.")
    return redirect(_url_section('matieres', classe))


def _matiere(request, pk):
    return get_object_or_404(
        filter_by_user_school(MatiereAccueil.objects.select_related('classe'), request.user, 'classe__ecole'), pk=pk)


@can_manage_notes
def modifier_matiere(request, pk):
    matiere = _matiere(request, pk)
    form = MatiereAccueilForm(request.POST or None, instance=matiere, classe=matiere.classe)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, "Matière mise à jour.")
        return redirect(_url_section('matieres', matiere.classe))
    return render(request, 'examen_accueil/formulaire.html', _contexte(
        request, 'matieres', matiere.classe, form=form, titre=f"Modifier la matière « {matiere.nom} »",
        retour=_url_section('matieres', matiere.classe)))


@can_manage_notes
@require_POST
def supprimer_matiere(request, pk):
    matiere = _matiere(request, pk)
    classe = matiere.classe
    matiere.delete()
    messages.success(request, "Matière supprimée (ses notes de test ont été effacées).")
    return redirect(_url_section('matieres', classe))


# --------------------------------------------------------------------- élèves

@can_manage_notes
def eleves(request):
    classe = _classe_choisie(request)
    tous = request.GET.get('tous') == '1'
    lignes, nb_matieres = [], 0
    if classe is not None:
        nb_matieres = classe.matieres_test_accueil.count()
        resultats = {r['eleve'].pk: r for r in calculer_resultats(classe)}
        for eleve in eleves_a_evaluer(classe, tous):
            lignes.append({'eleve': eleve, 'resultat': resultats.pop(eleve.pk, None)})
        # Élèves notés puis sortis de la classe : leurs résultats restent visibles.
        lignes += [{'eleve': r['eleve'], 'resultat': r, 'ailleurs': True} for r in resultats.values()]
    return render(request, 'examen_accueil/eleves.html', _contexte(
        request, 'eleves', classe, lignes=lignes, tous=tous, nb_matieres=nb_matieres))


# --------------------------------------------------------------------- saisie

def _lire_note(brute, note_sur):
    """Valeur saisie → (Decimal | None, erreur | None). Accepte la virgule."""
    texte = (brute or '').strip().replace(',', '.')
    if not texte:
        return None, None
    try:
        valeur = Decimal(texte)
    except InvalidOperation:
        return None, f"« {brute} » n'est pas un nombre"
    if not valeur.is_finite() or valeur < 0 or valeur > note_sur:
        return None, f"{brute} est hors du barème (0 à {note_sur})"
    return valeur.quantize(Decimal('0.01')), None


@can_manage_notes
def saisie_notes(request):
    classe = _classe_choisie(request)
    tous = (request.GET.get('tous') or request.POST.get('tous')) == '1'
    contexte = _contexte(request, 'saisie_notes', classe, tous=tous)
    if classe is None:
        return render(request, 'examen_accueil/saisie_notes.html', contexte)

    parametre = ParametreAccueil.pour(classe)
    matieres_classe = list(classe.matieres_test_accueil.all())
    liste = list(eleves_a_evaluer(classe, tous))
    existantes = {(n.eleve_id, n.matiere_id): n for n in NoteAccueil.objects.filter(matiere__classe=classe)}
    saisies = {}
    erreurs = []

    if request.method == 'POST':
        a_enregistrer, a_supprimer = [], []
        for eleve in liste:
            for matiere in matieres_classe:
                cle = f"note_{eleve.pk}_{matiere.pk}"
                if cle not in request.POST:
                    continue
                brute = request.POST[cle]
                saisies[(eleve.pk, matiere.pk)] = brute
                valeur, erreur = _lire_note(brute, parametre.note_sur)
                if erreur:
                    erreurs.append(f"{eleve.nom_complet} - {matiere.nom} : {erreur}")
                    continue
                existante = existantes.get((eleve.pk, matiere.pk))
                if valeur is None:
                    if existante:
                        a_supprimer.append(existante.pk)
                elif existante is None or existante.note != valeur:
                    a_enregistrer.append((eleve, matiere, valeur, existante))

        if erreurs:
            messages.error(request, "Aucune note n'a été enregistrée : corrigez les valeurs signalées.")
        else:
            with transaction.atomic():
                for eleve, matiere, valeur, existante in a_enregistrer:
                    note = existante or NoteAccueil(eleve=eleve, matiere=matiere)
                    note.note = valeur
                    note.saisi_par = request.user
                    note.save()
                for note in NoteAccueil.objects.filter(pk__in=a_supprimer):
                    note.delete()
                # Même indicateur que la liste des élèves : « évalué au test d'accueil ».
                for eleve in {e for e, _, _, _ in a_enregistrer if not e.evaluation_accueil_effectuee}:
                    eleve.evaluation_accueil_effectuee = True
                    eleve.save(update_fields=['evaluation_accueil_effectuee', 'date_modification'])
            messages.success(request, f"Notes enregistrées ({len(a_enregistrer)} modifiée(s), "
                                      f"{len(a_supprimer)} effacée(s)).")
            return redirect(_url_section('saisie_notes', classe, tous='1' if tous else ''))

    lignes = []
    for eleve in liste:
        cellules = []
        for matiere in matieres_classe:
            cle = (eleve.pk, matiere.pk)
            if cle in saisies:
                valeur = saisies[cle]
            else:
                note = existantes.get(cle)
                valeur = exports.nombre(note.note) if note else ''
            cellules.append({'nom': f"note_{eleve.pk}_{matiere.pk}", 'valeur': valeur})
        lignes.append({'eleve': eleve, 'cellules': cellules})

    contexte.update(parametre=parametre, matieres=matieres_classe, lignes=lignes, erreurs=erreurs)
    return render(request, 'examen_accueil/saisie_notes.html', contexte)


# ---------------------------------------------------------------- consultation

@can_manage_notes
def consultation(request):
    classe = _classe_choisie(request)
    contexte = _contexte(request, 'consultation', classe)
    if classe is not None:
        parametre = ParametreAccueil.pour(classe)
        lignes = calculer_resultats(classe, parametre)
        contexte.update(parametre=parametre, matieres=classe.matieres_test_accueil.all(), lignes=lignes,
                        stats=statistiques(lignes))
    return render(request, 'examen_accueil/consultation.html', contexte)


@can_manage_notes
def resultats_pdf(request, classe_id):
    classe = _classe(request, classe_id)
    return _reponse_fichier(exports.resultats_pdf(classe), exports.nom_fichier(classe, 'resultats_test_accueil', 'pdf'),
                            'application/pdf', en_ligne=True)


@can_manage_notes
def resultats_excel(request, classe_id):
    classe = _classe(request, classe_id)
    return _reponse_fichier(
        exports.resultats_excel(classe), exports.nom_fichier(classe, 'resultats_test_accueil', 'xlsx'),
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ------------------------------------------------------------------ bulletins

@can_manage_notes
def bulletins(request):
    classe = _classe_choisie(request)
    contexte = _contexte(request, 'bulletins', classe)
    if classe is not None:
        lignes = calculer_resultats(classe)
        contexte.update(lignes=sorted(lignes, key=lambda r: (r['eleve'].nom.upper(), r['eleve'].prenom.upper())),
                        parametre=ParametreAccueil.pour(classe))
    return render(request, 'examen_accueil/bulletins.html', contexte)


@can_manage_notes
def bulletins_pdf(request, classe_id):
    classe = _classe(request, classe_id)
    return _reponse_fichier(exports.bulletins_pdf(classe), exports.nom_fichier(classe, 'bulletins_test_accueil', 'pdf'),
                            'application/pdf', en_ligne=True)


@can_manage_notes
def bulletin_eleve_pdf(request, classe_id, eleve_id):
    classe = _classe(request, classe_id)
    eleve = get_object_or_404(Eleve.objects.filter(notes_test_accueil__matiere__classe=classe).distinct(), pk=eleve_id)
    contenu = exports.bulletins_pdf(classe, eleves=[eleve])
    nom = f"bulletin_test_accueil_{exports.slugify(eleve.nom_complet) or eleve.pk}.pdf"
    return _reponse_fichier(contenu, nom, 'application/pdf', en_ligne=True)
