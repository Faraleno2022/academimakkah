"""Vues du module Test d'accueil.

Toutes les requêtes passent par ``_classes_utilisateur`` : un utilisateur ne
voit et ne modifie que les classes de test de son école (le superutilisateur
voit toutes les écoles, comme dans le reste de l'application).
"""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db import transaction
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from eleves.utils_annee import get_annee_active
from utilisateurs.permissions import can_manage_notes
from utilisateurs.utils import filter_by_user_school, user_school

from . import exports
from .calculs import calculer_resultats, statistiques
from .forms import CandidatAccueilForm, ClasseAccueilForm, MatiereAccueilForm
from .models import CandidatAccueil, ClasseAccueil, MatiereAccueil, NoteAccueil


def _classes_utilisateur(user):
    return filter_by_user_school(ClasseAccueil.objects.select_related('ecole'), user, 'ecole')


def _classe(request, pk):
    return get_object_or_404(_classes_utilisateur(request.user), pk=pk)


def _reponse_fichier(contenu, nom, type_mime, en_ligne=False):
    reponse = HttpResponse(contenu, content_type=type_mime)
    disposition = 'inline' if en_ligne else 'attachment'
    reponse['Content-Disposition'] = f'{disposition}; filename="{nom}"'
    return reponse


@can_manage_notes
def liste_classes(request):
    ecole = user_school(request.user)
    classes = _classes_utilisateur(request.user)
    annees = sorted(set(classes.values_list('annee_scolaire', flat=True)), reverse=True)
    annee = request.GET.get('annee') or ''
    if annee:
        classes = classes.filter(annee_scolaire=annee)

    if request.method == 'POST':
        if ecole is None:
            messages.error(request, "Aucune école n'est associée à votre compte.")
            return redirect('examen_accueil:liste')
        form = ClasseAccueilForm(request.POST, ecole=ecole)
        if form.is_valid():
            classe = form.save(commit=False)
            classe.cree_par = request.user
            classe.save()
            messages.success(request, f"Classe de test « {classe.nom} » créée. Ajoutez maintenant ses matières.")
            return redirect('examen_accueil:detail', classe.pk)
    else:
        annee_defaut = get_annee_active(request, ecole) if ecole else None
        form = ClasseAccueilForm(ecole=ecole, initial={'annee_scolaire': annee_defaut or ''})

    classes = classes.annotate(nb_matieres=Count('matieres', distinct=True),
                               nb_candidats=Count('candidats', distinct=True))
    lignes = [{'classe': c, 'nb_matieres': c.nb_matieres, 'nb_candidats': c.nb_candidats} for c in classes]
    return render(request, 'examen_accueil/liste.html', {
        'lignes': lignes, 'form': form, 'annees': annees, 'annee': annee,
        'ouvrir_formulaire': request.method == 'POST',
    })


@can_manage_notes
def detail_classe(request, pk):
    classe = _classe(request, pk)
    form_matiere = MatiereAccueilForm(classe=classe, prefix='matiere',
                                      initial={'ordre': classe.matieres.count() + 1})
    form_candidat = CandidatAccueilForm(prefix='candidat')

    if request.method == 'POST':
        if 'ajouter_matiere' in request.POST:
            form_matiere = MatiereAccueilForm(request.POST, classe=classe, prefix='matiere')
            if form_matiere.is_valid():
                matiere = form_matiere.save(commit=False)
                matiere.classe = classe
                matiere.save()
                messages.success(request, f"Matière « {matiere.nom} » ajoutée.")
                return redirect('examen_accueil:detail', classe.pk)
        elif 'ajouter_candidat' in request.POST:
            form_candidat = CandidatAccueilForm(request.POST, prefix='candidat')
            if form_candidat.is_valid():
                candidat = form_candidat.save(commit=False)
                candidat.classe = classe
                candidat.save()
                messages.success(request, f"Candidat « {candidat.nom_complet} » ajouté.")
                return redirect(f"{request.path}#candidats")

    return render(request, 'examen_accueil/detail.html', {
        'classe': classe,
        'matieres': classe.matieres.all(),
        'candidats': classe.candidats.all(),
        'nb_notes': NoteAccueil.objects.filter(matiere__classe=classe).count(),
        'form_matiere': form_matiere,
        'form_candidat': form_candidat,
    })


@can_manage_notes
def modifier_classe(request, pk):
    classe = _classe(request, pk)
    form = ClasseAccueilForm(request.POST or None, instance=classe, ecole=classe.ecole)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, "Classe de test mise à jour.")
        return redirect('examen_accueil:detail', classe.pk)
    return render(request, 'examen_accueil/formulaire.html', {
        'form': form, 'classe': classe, 'titre': f"Modifier la classe « {classe.nom} »",
    })


@can_manage_notes
@require_POST
def supprimer_classe(request, pk):
    classe = _classe(request, pk)
    nom = classe.nom
    classe.delete()
    messages.success(request, f"Classe de test « {nom} » supprimée avec ses matières, candidats et notes.")
    return redirect('examen_accueil:liste')


@can_manage_notes
def modifier_matiere(request, pk):
    matiere = get_object_or_404(
        filter_by_user_school(MatiereAccueil.objects.select_related('classe'), request.user, 'classe__ecole'), pk=pk)
    form = MatiereAccueilForm(request.POST or None, instance=matiere, classe=matiere.classe)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, "Matière mise à jour.")
        return redirect('examen_accueil:detail', matiere.classe_id)
    return render(request, 'examen_accueil/formulaire.html', {
        'form': form, 'classe': matiere.classe, 'titre': f"Modifier la matière « {matiere.nom} »",
    })


@can_manage_notes
@require_POST
def supprimer_matiere(request, pk):
    matiere = get_object_or_404(
        filter_by_user_school(MatiereAccueil.objects.all(), request.user, 'classe__ecole'), pk=pk)
    classe_id = matiere.classe_id
    matiere.delete()
    messages.success(request, "Matière supprimée (ses notes ont été effacées).")
    return redirect('examen_accueil:detail', classe_id)


@can_manage_notes
def modifier_candidat(request, pk):
    candidat = get_object_or_404(
        filter_by_user_school(CandidatAccueil.objects.select_related('classe'), request.user, 'classe__ecole'), pk=pk)
    form = CandidatAccueilForm(request.POST or None, instance=candidat)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, "Candidat mis à jour.")
        return redirect('examen_accueil:detail', candidat.classe_id)
    return render(request, 'examen_accueil/formulaire.html', {
        'form': form, 'classe': candidat.classe, 'titre': f"Modifier le candidat « {candidat.nom_complet} »",
    })


@can_manage_notes
@require_POST
def supprimer_candidat(request, pk):
    candidat = get_object_or_404(
        filter_by_user_school(CandidatAccueil.objects.all(), request.user, 'classe__ecole'), pk=pk)
    classe_id = candidat.classe_id
    candidat.delete()
    messages.success(request, "Candidat supprimé.")
    return redirect('examen_accueil:detail', classe_id)


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
def saisie_notes(request, pk):
    classe = _classe(request, pk)
    matieres = list(classe.matieres.all())
    candidats = list(classe.candidats.all())
    existantes = {(n.candidat_id, n.matiere_id): n for n in NoteAccueil.objects.filter(matiere__classe=classe)}
    saisies = {}
    erreurs = []

    if request.method == 'POST':
        a_enregistrer, a_supprimer = [], []
        for candidat in candidats:
            for matiere in matieres:
                cle = f"note_{candidat.pk}_{matiere.pk}"
                if cle not in request.POST:
                    continue
                brute = request.POST[cle]
                saisies[(candidat.pk, matiere.pk)] = brute
                valeur, erreur = _lire_note(brute, classe.note_sur)
                if erreur:
                    erreurs.append(f"{candidat.nom_complet} - {matiere.nom} : {erreur}")
                    continue
                existante = existantes.get((candidat.pk, matiere.pk))
                if valeur is None:
                    if existante:
                        a_supprimer.append(existante.pk)
                elif existante is None or existante.note != valeur:
                    a_enregistrer.append((candidat, matiere, valeur, existante))

        if erreurs:
            messages.error(request, "Aucune note n'a été enregistrée : corrigez les valeurs signalées.")
        else:
            with transaction.atomic():
                for candidat, matiere, valeur, existante in a_enregistrer:
                    note = existante or NoteAccueil(candidat=candidat, matiere=matiere)
                    note.note = valeur
                    note.saisi_par = request.user
                    note.save()
                for note in NoteAccueil.objects.filter(pk__in=a_supprimer):
                    note.delete()
            messages.success(request, f"Notes enregistrées ({len(a_enregistrer)} modifiée(s), "
                                      f"{len(a_supprimer)} effacée(s)).")
            return redirect('examen_accueil:saisie_notes', classe.pk)

    lignes = []
    for candidat in candidats:
        cellules = []
        for matiere in matieres:
            cle = (candidat.pk, matiere.pk)
            if cle in saisies:
                valeur = saisies[cle]
            else:
                note = existantes.get(cle)
                valeur = exports.nombre(note.note) if note else ''
            cellules.append({'nom': f"note_{candidat.pk}_{matiere.pk}", 'valeur': valeur})
        lignes.append({'candidat': candidat, 'cellules': cellules})

    return render(request, 'examen_accueil/saisie_notes.html', {
        'classe': classe, 'matieres': matieres, 'lignes': lignes, 'erreurs': erreurs,
    })


@can_manage_notes
def resultats(request, pk):
    classe = _classe(request, pk)
    lignes = calculer_resultats(classe)
    return render(request, 'examen_accueil/resultats.html', {
        'classe': classe, 'matieres': classe.matieres.all(), 'lignes': lignes,
        'stats': statistiques(classe, lignes),
    })


@can_manage_notes
def resultats_pdf(request, pk):
    classe = _classe(request, pk)
    return _reponse_fichier(exports.resultats_pdf(classe), exports.nom_fichier(classe, 'resultats_test_accueil', 'pdf'),
                            'application/pdf', en_ligne=True)


@can_manage_notes
def resultats_excel(request, pk):
    classe = _classe(request, pk)
    return _reponse_fichier(
        exports.resultats_excel(classe), exports.nom_fichier(classe, 'resultats_test_accueil', 'xlsx'),
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@can_manage_notes
def bulletins_pdf(request, pk):
    classe = _classe(request, pk)
    return _reponse_fichier(exports.bulletins_pdf(classe), exports.nom_fichier(classe, 'bulletins_test_accueil', 'pdf'),
                            'application/pdf', en_ligne=True)


@can_manage_notes
def bulletin_candidat_pdf(request, pk):
    candidat = get_object_or_404(
        filter_by_user_school(CandidatAccueil.objects.select_related('classe__ecole'), request.user, 'classe__ecole'),
        pk=pk)
    contenu = exports.bulletins_pdf(candidat.classe, candidats=[candidat])
    nom = f"bulletin_test_accueil_{exports.slugify(candidat.nom_complet) or candidat.pk}.pdf"
    return _reponse_fichier(contenu, nom, 'application/pdf', en_ligne=True)
