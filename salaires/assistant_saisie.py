"""
Assistant IA des avances de salaire.

Claude lit un cahier ou une liste d'avances (un ou plusieurs membres du personnel) ; la page
affiche une table de corrections ; chaque ligne validée passe par la vue « Ajouter une
avance » : mêmes contrôles (période ouverte, éligibilité) et recalcul du salaire net.
"""

import logging
import re

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from assistant_ia import lot
from assistant_ia.claude import AssistantIAError, extraire_json, preparer_image
from utilisateurs.utils import user_is_superadmin, user_school

from .forms import AvanceSalaireForm
from .models import AvanceSalaire

logger = logging.getLogger(__name__)


def _formulaire(request, donnees=None):
    ecole = user_school(request.user)
    # Seul le superutilisateur voit le personnel de toutes les écoles ; un administrateur
    # d'école (rôle ADMIN) reste limité à la sienne.
    restreindre = not user_is_superadmin(request.user) and ecole is not None
    return AvanceSalaireForm(donnees, ecole=ecole if restreindre else None)


def _avance_identique(personne, periode_id, montant, jour):
    """Même personne, même période, même montant, même date : avance déjà enregistrée."""
    if personne is None or not str(periode_id or "").isdigit() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(jour or "")):
        return None
    try:
        montant = int(float(montant))
    except (TypeError, ValueError):
        return None
    return AvanceSalaire.objects.filter(
        enseignant=personne, periode_id=periode_id or None, montant=montant, date_avance=jour
    ).order_by("-pk").first()


def _alerte_avance(avance) -> str:
    return (f"Avance déjà enregistrée le {avance.date_avance:%d/%m/%Y} pour cette période : "
            "ne la cochez que s'il s'agit d'une autre avance.")


@login_required
@require_POST
def assistant_avances(request):
    empreintes = lot.empreintes_document(request)
    form = _formulaire(request)
    enseignants = list(form.fields["enseignant"].queryset)
    periodes = list(form.fields["periode"].queryset)
    modes = list(form.fields["mode_paiement"].queryset)
    if not enseignants:
        return JsonResponse({"ok": False, "erreur": "Aucun membre du personnel actif."}, status=400)
    if not periodes:
        return JsonResponse({"ok": False, "erreur": "Aucune période de paie ouverte : créez-la d'abord."}, status=400)

    schema = lot.schema_liste("avances", {
        "personne": {"type": "string"}, "enseignant_id": lot.ENTIER_OU_NUL,
        "montant": lot.NOMBRE_OU_NUL, "date": {"type": "string"},
        "mode_paiement_id": lot.ENTIER_OU_NUL, "reference": {"type": "string"}, "motif": {"type": "string"},
    })
    liste_personnel = "\n".join(f"{e.id} | {e.nom_complet}" for e in enseignants)
    liste_modes = "\n".join(f"{m.id} | {m.nom}" for m in modes) or "(aucun)"
    consigne = f"""Tu aides la comptabilité d'une école guinéenne à saisir des avances sur salaire dans
MySchoolGN. On te donne la photo d'un cahier d'avances, d'une liste, d'un reçu, ou un texte.
Relève CHAQUE avance du document, dans l'ordre.

Personnel de l'école (id | nom complet) :
{liste_personnel}

Modes de paiement (id | nom) :
{liste_modes}

Pour chaque avance :
- personne : le nom tel qu'il est écrit sur le document.
- enseignant_id : l'id de la personne de la liste qui correspond (l'ordre prénom/nom, les accents
  ou une faute d'orthographe peuvent varier) ; null si personne ne correspond avec certitude.
- montant : en francs guinéens, nombre entier ; « 500 000 GNF » = 500000 ; null si illisible.
- date : au format AAAA-MM-JJ, "" si absente. Aujourd'hui : {timezone.localdate().isoformat()}.
- mode_paiement_id : le mode de la liste (espèces, Orange Money…), null si inconnu.
- reference : numéro de reçu ou de transaction, "" si absent.
- motif : motif de l'avance s'il est écrit, sinon "".
{lot.REGLES_COMMUNES}"""

    try:
        images = [preparer_image(f) for f in request.FILES.getlist("photos")[:4]]
        resultat = extraire_json(consigne, schema, images=images, texte=request.POST.get("texte", ""))
    except AssistantIAError as exc:
        return JsonResponse({"ok": False, "erreur": str(exc)}, status=400)

    par_id = {e.id: e for e in enseignants}
    ids_modes = {m.id for m in modes}
    lignes = []
    for lu in resultat.get("avances", [])[:lot.MAX_LIGNES]:
        douteux, alerte = list(lu["champs_douteux"]), ""
        personne = par_id.get(lu["enseignant_id"])
        if personne is None:
            # Second essai côté serveur, par ressemblance du nom
            personne, suggestions = lot.trouver(enseignants, lu["personne"], lambda e: e.nom_complet)
            if personne is None:
                alerte = f"« {lu['personne'] or 'nom non lu'} » non retrouvé dans le personnel : choisissez la personne."
                if suggestions:
                    alerte += " Suggestions : " + " ; ".join(e.nom_complet for e, _ in suggestions)
        montant = lu["montant"] if lu["montant"] and lu["montant"] > 0 else None
        for cle, manque in (("enseignant_id", personne is None or "personne" in douteux), ("montant", montant is None)):
            if manque:
                douteux.append(cle)
        jour = lu["date"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", lu["date"] or "") else timezone.localdate().isoformat()
        identique = _avance_identique(personne, periodes[0].id, montant, jour)
        if identique:
            alerte = _alerte_avance(identique)
        lignes.append({
            "valeurs": {
                "enseignant_id": personne.id if personne else "", "periode_id": periodes[0].id,
                "montant": "" if montant is None else int(montant),
                "date": jour,
                "mode_paiement_id": lu["mode_paiement_id"] if lu["mode_paiement_id"] in ids_modes else "",
                "reference": lu["reference"], "motif": lu["motif"],
            },
            "douteux": douteux,
            "remarque": (f"Lu : {lu['personne']}. " if lu["personne"] else "") + lu["remarque"],
            "alerte": alerte,
            "coche": bool(personne and montant is not None and not identique),
        })
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucune avance n'a été lue sur ce document."}, status=400)

    logger.info("Assistant avances : %s ligne(s) lue(s) par %s", len(lignes), request.user.username)
    return lot.reponse_lecture(request, "avances", empreintes, **{
        "lignes": lignes,
        "intro": f"<strong>{len(lignes)} avance(s) lue(s).</strong> Corrigez si besoin (orange = à contrôler), décochez "
                 "les lignes à ne pas enregistrer, puis validez. Le salaire net de chaque personne est recalculé.",
        "colonnes": [
            lot.colonne("enseignant_id", "Personnel", "select", [(e.id, e.nom_complet) for e in enseignants], 220),
            lot.colonne("periode_id", "Période de paie", "select", [(p.id, str(p)) for p in periodes], 190),
            lot.colonne("montant", "Montant (GNF)", "number", largeur=130),
            lot.colonne("date", "Date", "date", largeur=140),
            lot.colonne("mode_paiement_id", "Mode", "select", [(m.id, m.nom) for m in modes], 140),
            lot.colonne("reference", "Référence", largeur=120),
            lot.colonne("motif", "Motif", largeur=170),
        ],
    })


@login_required
@require_POST
def assistant_avances_importer(request):
    from .views import ajouter_avance

    lignes, corps = lot.lignes_du_corps(request)
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucune ligne à enregistrer."}, status=400)

    personnel = {str(e.id): e for e in _formulaire(request).fields["enseignant"].queryset}
    crees, echecs = [], []
    for ligne in lignes:
        index = ligne.get("_index")
        personne = personnel.get(lot.texte(ligne, "enseignant_id"))
        if personne is None:
            echecs.append({"index": index, "nom": "(personne non choisie)", "erreur": "choisissez le membre du personnel"})
            continue
        nom = personne.nom_complet
        donnees = {
            "enseignant": personne.pk, "periode": lot.texte(ligne, "periode_id"),
            "montant": lot.chiffres(ligne.get("montant")),
            "date_avance": lot.texte(ligne, "date") or timezone.localdate().isoformat(),
            "mode_paiement": lot.texte(ligne, "mode_paiement_id"),
            "reference_externe": lot.texte(ligne, "reference"),
            "motif": lot.texte(ligne, "motif") or "Saisi avec l'assistant photo.",
        }
        identique = _avance_identique(personne, donnees["periode"], donnees["montant"], donnees["date_avance"])
        if identique:
            echecs.append({"index": index, "nom": nom, "erreur": _alerte_avance(identique).replace(
                "ne la cochez que", "saisissez-la par le formulaire")})
            continue
        form = _formulaire(request, donnees)
        if not form.is_valid():
            echecs.append({"index": index, "nom": nom, "erreur": lot.erreurs_formulaire(form)})
            continue
        dernier = AvanceSalaire.objects.filter(enseignant=personne).order_by("-pk").values_list("pk", flat=True).first() or 0
        try:
            reponse, erreurs = lot.executer_vue(request, ajouter_avance, donnees)
        except Exception:  # noqa: BLE001
            logger.exception("Assistant avances : échec pour %s", nom)
            echecs.append({"index": index, "nom": nom, "erreur": "erreur à l'enregistrement"})
            continue
        cree = AvanceSalaire.objects.filter(enseignant=personne, pk__gt=dernier).order_by("-pk").first()
        if reponse.status_code == 302 and cree:
            montant = f"{int(cree.montant):,}".replace(",", " ")
            crees.append({"index": index, "texte": f"{nom} – {montant} GNF ({cree.periode})"})
        else:
            echecs.append({"index": index, "nom": nom, "erreur": " ".join(erreurs)
                           or "avance refusée : vérifiez que cette personne est payée sur cette période"})

    lot.noter_import(request, "avances", lot.empreintes_du_corps(corps), len(crees))
    logger.info("Assistant avances : %s enregistrée(s), %s refusée(s), par %s", len(crees), len(echecs), request.user.username)
    return JsonResponse({"ok": True, "crees": crees, "echecs": echecs})


@login_required
@require_POST
def assistant_avances_excel(request):
    lignes, _ = lot.lignes_du_corps(request)
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucune ligne à exporter."}, status=400)
    form = _formulaire(request)
    personnel = {str(e.id): e.nom_complet for e in form.fields["enseignant"].queryset}
    periodes = {str(p.id): str(p) for p in form.fields["periode"].queryset}
    modes = {str(m.id): m.nom for m in form.fields["mode_paiement"].queryset}

    def ligne_excel(l):
        montant = lot.chiffres(l.get("montant"))
        return [
            personnel.get(lot.texte(l, "enseignant_id"), ""), periodes.get(lot.texte(l, "periode_id"), ""),
            int(montant) if montant else "", lot.texte(l, "date"),
            modes.get(lot.texte(l, "mode_paiement_id"), ""), lot.texte(l, "reference"), lot.texte(l, "motif"),
        ]

    return lot.reponse_excel(
        "avances_assistant.xlsx", "Avances",
        ["Personnel", "Période", "Montant (GNF)", "Date", "Mode de paiement", "Référence", "Motif"],
        [ligne_excel(l) for l in lignes],
    )
