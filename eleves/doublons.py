"""
Doublons d'élèves dans une même classe.

- Détection : deux fiches de la même classe portant le même nom (ordre prénom/nom,
  accents, casse et espaces indifférents) sont un doublon « certain » ; des noms très
  proches (une lettre d'écart) sont « à vérifier ».
- Proposition : on garde la fiche qui porte le plus de données (paiements, notes,
  abonnements…), à défaut la plus ancienne ; les autres fiches SANS données sont
  proposées à la corbeille. Une fiche qui a des données n'est jamais proposée d'office :
  il faut d'abord la vérifier.
- Correction : mise à la corbeille (restaurable depuis Administration › Corbeille).
"""

from collections import defaultdict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render

from assistant_ia.claude import normaliser, ressemblance
from utilisateurs.utils import filter_by_user_school, user_is_admin

from .models import Classe, Eleve
from .utils_annee import get_annee_active

SEUIL_PROCHE = 0.92

# Données rattachées à une fiche, avec un libellé lisible (les autres sont comptées en « autres »)
LIBELLES = {
    "paiements": "paiement(s)",
    "abonnements_bus": "abonnement(s) bus",
    "abonnements_cantine": "abonnement(s) cantine",
    "notes_mensuelles": "note(s) mensuelle(s)",
    "compositions": "composition(s)",
    "notes_evaluations": "note(s)",
    "notes_test_accueil": "note(s) du test d'accueil",
    "presences": "présence(s)",
}
IGNOREES = {"historique", "echeanciers"}  # créées automatiquement : ne prouvent pas un usage réel


def cle_nom(eleve) -> str:
    """« BAH  Mamadou » et « Mamadou Bah » -> « bah mamadou »."""
    return " ".join(sorted(normaliser(f"{eleve.prenom} {eleve.nom}").split()))


def donnees_rattachees(eleve):
    """{libellé: nombre} des données liées à la fiche, et leur total."""
    detail, total = {}, 0
    for relation in Eleve._meta.related_objects:
        if relation.one_to_one or not relation.on_delete or relation.on_delete.__name__ != "CASCADE":
            continue
        nom = relation.get_accessor_name()
        if nom in IGNOREES:
            continue
        nombre = getattr(eleve, nom).count()
        if nombre:
            libelle = LIBELLES.get(nom, "autre(s) donnée(s)")
            detail[libelle] = detail.get(libelle, 0) + nombre
            total += nombre
    return detail, total


def _fiche(eleve):
    detail, total = donnees_rattachees(eleve)
    responsable = eleve.responsable_principal
    return {
        "eleve": eleve,
        "donnees": detail,
        "total": total,
        "responsable": f"{responsable.prenom} {responsable.nom}".strip() or responsable.telephone if responsable else "",
        "telephone": responsable.telephone if responsable else "",
    }


def detecter(classes):
    """Groupes de doublons, classe par classe : [{classe, certain, fiches: [...] }]."""
    groupes = []
    eleves = (
        Eleve.objects.filter(classe__in=classes)
        .select_related("classe", "responsable_principal")
        .order_by("classe__nom", "pk")
    )
    par_classe = defaultdict(list)
    for e in eleves:
        par_classe[e.classe].append(e)

    for classe, liste in par_classe.items():
        par_cle = defaultdict(list)
        for e in liste:
            par_cle[cle_nom(e)].append(e)
        # Doublons certains : même nom
        for membres in par_cle.values():
            if len(membres) > 1:
                groupes.append({"classe": classe, "certain": True, "fiches": membres})
        # À vérifier : noms très proches (une lettre d'écart), une seule fois par paire
        cles = sorted(par_cle)
        for i, a in enumerate(cles):
            for b in cles[i + 1:]:
                if a[:1] != b[:1] and a.split()[-1][:1] != b.split()[-1][:1]:
                    continue
                if ressemblance(a, b) >= SEUIL_PROCHE:
                    groupes.append({"classe": classe, "certain": False, "fiches": par_cle[a][:1] + par_cle[b][:1]})

    for g in groupes:
        g["fiches"] = [_fiche(e) for e in g["fiches"]]
        # Fiche gardée : la plus riche en données, puis la plus ancienne
        garde = max(g["fiches"], key=lambda f: (f["total"], -f["eleve"].pk))
        for f in g["fiches"]:
            f["garder"] = f is garde
            # Proposée à la corbeille seulement si certaine ET vide
            f["proposee"] = g["certain"] and not f["garder"] and f["total"] == 0
        g["garde"] = garde
        g["a_verifier"] = any(not f["garder"] and f["total"] for f in g["fiches"])
    groupes.sort(key=lambda g: (not g["certain"], g["classe"].nom, cle_nom(g["fiches"][0]["eleve"])))
    return groupes


def peut_corriger(user) -> bool:
    profil = getattr(user, "profil", None)
    return bool(user.is_superuser or user_is_admin(user) or getattr(profil, "est_compte_principal", False))


def _classes_visibles(request):
    classes = filter_by_user_school(Classe.objects.select_related("ecole"), request.user, "ecole")
    ecole = getattr(getattr(request.user, "profil", None), "ecole", None)
    annee = get_annee_active(request, ecole) if ecole else None
    if annee:
        classes = classes.filter(annee_scolaire=annee)
    return classes.order_by("nom")


@login_required
def doublons_eleves(request):
    classes = _classes_visibles(request)
    classe_id = request.GET.get("classe") or request.POST.get("classe") or ""
    choisies = classes.filter(pk=classe_id) if classe_id.isdigit() else classes

    if request.method == "POST":
        if not peut_corriger(request.user):
            messages.error(request, "Seul un administrateur ou le compte principal peut corriger les doublons.")
            return redirect(request.get_full_path())
        return _corriger(request, choisies, classe_id)

    groupes = detecter(choisies)
    return render(request, "eleves/doublons.html", {
        "titre_page": "Doublons d'élèves",
        "classes": classes,
        "classe_id": classe_id,
        "groupes": groupes,
        "nb_certains": sum(1 for g in groupes if g["certain"]),
        "nb_proposees": sum(1 for g in groupes for f in g["fiches"] if f["proposee"]),
        "peut_corriger": peut_corriger(request.user),
    })


def _corriger(request, classes, classe_id):
    from administration.audit import mettre_eleve_en_corbeille
    from utilisateurs.models import JournalActivite

    ids = [int(x) for x in request.POST.getlist("corbeille") if x.isdigit()]
    eleves = list(Eleve.objects.filter(pk__in=ids, classe__in=classes).select_related("classe"))
    retour = redirect(f"{request.path}?classe={classe_id}" if classe_id else request.path)
    if not eleves:
        messages.warning(request, "Aucune fiche cochée.")
        return retour

    # Sécurité : on ne met jamais à la corbeille la dernière fiche d'un nom dans sa classe
    restantes = defaultdict(int)
    for e in Eleve.objects.filter(classe__in={e.classe for e in eleves}):
        restantes[(e.classe_id, cle_nom(e))] += 1
    garder = {}
    with transaction.atomic():
        noms = []
        for e in eleves:
            cle = (e.classe_id, cle_nom(e))
            if restantes[cle] <= 1:
                garder[e.pk] = e
                continue
            restantes[cle] -= 1
            noms.append(f"{e.prenom} {e.nom} ({e.matricule})")
            mettre_eleve_en_corbeille(e, request=request, motif=f"Doublon dans la classe {e.classe.nom}")
        if noms:
            JournalActivite.objects.create(
                user=request.user, action="SUPPRESSION", type_objet="ELEVE",
                description="Correction des doublons : mise à la corbeille de " + ", ".join(noms),
                adresse_ip=request.META.get("REMOTE_ADDR", ""),
                user_agent=request.META.get("HTTP_USER_AGENT", "")[:200],
            )
    if noms:
        messages.success(
            request,
            f"{len(noms)} fiche(s) en double placée(s) dans la corbeille (restaurables depuis "
            f"Administration › Corbeille) : " + ", ".join(noms),
        )
    if garder:
        messages.warning(
            request,
            "Non supprimée(s), car c'est la dernière fiche de ce nom dans la classe : "
            + ", ".join(f"{e.prenom} {e.nom} ({e.matricule})" for e in garder.values()),
        )
    return retour
