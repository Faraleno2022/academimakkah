"""
Assistant IA des abonnements de bus et de cantine.

Claude lit un reçu, un bordereau ou une liste (un ou plusieurs élèves) ; la page affiche
une table de corrections ; chaque ligne validée est enregistrée par la vue habituelle
(« Nouveau paiement Bus » / « Nouvel abonnement Cantine ») : mêmes formulaires, mêmes
plafonds, même numéro de reçu.
"""

import logging

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from assistant_ia import lot
from assistant_ia.claude import AssistantIAError, extraire_json, preparer_image
from utilisateurs.utils import user_is_superadmin, user_school

from .forms import AbonnementBusForm, AbonnementCantineForm
from .models import AbonnementBus, AbonnementCantine

logger = logging.getLogger(__name__)

OBSERVATION = "Saisi avec l'assistant photo."


def _ecole_ou_refus(request):
    ecole = None if user_is_superadmin(request.user) else user_school(request.user)
    if not user_is_superadmin(request.user) and ecole is None:
        return None, JsonResponse({"ok": False, "erreur": "Aucune école n'est associée à votre compte."}, status=403)
    return ecole, None


def _lire(request, consigne, schema):
    images = [preparer_image(f) for f in request.FILES.getlist("photos")[:4]]
    return extraire_json(consigne, schema, images=images, texte=request.POST.get("texte", ""))


def _date(valeur) -> str:
    import re
    return valeur if re.fullmatch(r"\d{4}-\d{2}-\d{2}", valeur or "") else ""


def _entete(quoi) -> str:
    return (f"Tu aides la caisse d'une école guinéenne à saisir {quoi} dans MySchoolGN. On te donne la photo "
            "d'un reçu, d'un bordereau, d'une capture Orange Money / MTN ou d'une liste, ou un texte. "
            "Relève CHAQUE ligne du document, dans l'ordre (un reçu = une ligne, une liste = plusieurs).")


REGLES_ELEVE = f"""- eleve_nom : nom et prénom de l'élève (pas celui du parent qui paie).
- matricule et classe de l'élève s'ils apparaissent, sinon "".
- montant : en francs guinéens, nombre entier ; « 1 500 000 GNF » = 1500000 ; null si illisible.
- date : au format AAAA-MM-JJ, "" si absente. Aujourd'hui : {{aujourdhui}}.
- reference : numéro de transaction, de reçu ou de bordereau, "" si absent."""


# ------------------------------------------------------------------------- BUS

def _options_bus(ecole):
    form = AbonnementBusForm(ecole=ecole)
    grilles = list(form.fields["grille"].queryset)
    tranches = [(code, libelle) for _, groupe in form.fields["periodicite"].choices for code, libelle in groupe]
    modes = list(form.fields["mode_paiement"].queryset)
    return form, grilles, tranches, modes


@login_required
@require_POST
def assistant_bus(request):
    ecole, refus = _ecole_ou_refus(request)
    if refus:
        return refus
    form, grilles, tranches, modes = _options_bus(ecole)
    if not grilles:
        return JsonResponse({"ok": False, "erreur": "Créez d'abord une grille tarifaire de bus."}, status=400)

    schema = lot.schema_liste("paiements", {
        "eleve_nom": {"type": "string"}, "matricule": {"type": "string"}, "classe": {"type": "string"},
        "montant": lot.NOMBRE_OU_NUL, "date": {"type": "string"},
        "grille_id": lot.ENTIER_OU_NUL,
        "tranche": {"type": "string", "enum": [c for c, _ in tranches] + [""]},
        "mode_paiement_id": lot.ENTIER_OU_NUL, "reference": {"type": "string"},
    })
    liste_zones = "\n".join(f"{g.id} | {g.zone} | {g.annee_scolaire}" for g in grilles)
    liste_tranches = "\n".join(f"{c} | {l}" for c, l in tranches)
    liste_modes = "\n".join(f"{m.id} | {m.nom}" for m in modes) or "(aucun)"
    entete = _entete("des paiements de transport scolaire (bus)")
    consigne = f"""{entete}

Zones de bus (id | zone | année scolaire) :
{liste_zones}

Tranches possibles (code | libellé) :
{liste_tranches}

Modes de paiement (id | nom) :
{liste_modes}

Pour chaque paiement :
{REGLES_ELEVE.format(aujourdhui=timezone.localdate().isoformat())}
- grille_id : la zone de la liste qui correspond (quartier, circuit, zone), null si inconnue.
- tranche : le code de la liste (1ère tranche, annuel…), "" si le document ne le dit pas.
- mode_paiement_id : le mode de la liste (espèces, Orange Money…), null si inconnu.
{lot.REGLES_COMMUNES}"""

    try:
        resultat = _lire(request, consigne, schema)
    except AssistantIAError as exc:
        return JsonResponse({"ok": False, "erreur": str(exc)}, status=400)

    from paiements.referentiels import mode_paiement_par_defaut

    eleves = form.fields["eleve"].queryset
    ids_grilles, ids_modes = {g.id for g in grilles}, {m.id for m in modes}
    mode_defaut = mode_paiement_par_defaut()
    lignes = []
    for lu in resultat.get("paiements", [])[:lot.MAX_LIGNES]:
        douteux = list(lu["champs_douteux"])
        eleve, suggestions = lot.trouver_eleve(eleves, lu["eleve_nom"], lu["matricule"], lu["classe"])
        matricule, debut, alerte = lot.cellule_eleve(eleve, suggestions, lu["eleve_nom"])
        grille = lu["grille_id"] if lu["grille_id"] in ids_grilles else (grilles[0].id if len(grilles) == 1 else "")
        mode = lu["mode_paiement_id"] if lu["mode_paiement_id"] in ids_modes else (mode_defaut.pk if mode_defaut else "")
        montant = lu["montant"] if lu["montant"] and lu["montant"] > 0 else None
        for cle, manque in (("matricule", not eleve), ("montant", montant is None), ("grille_id", not grille),
                            ("tranche", not lu["tranche"]), ("eleve_nom", False)):
            if manque:
                douteux.append(cle)
        if "eleve_nom" in douteux:
            douteux.append("matricule")
        lignes.append({
            "valeurs": {
                "matricule": matricule or lu["matricule"], "montant": "" if montant is None else int(montant),
                "date": _date(lu["date"]) or timezone.localdate().isoformat(), "grille_id": grille,
                "tranche": lu["tranche"], "mode_paiement_id": mode, "reference": lu["reference"],
            },
            "douteux": douteux, "remarque": f"{debut} {lu['remarque']}".strip(), "alerte": alerte,
            "coche": bool(eleve and montant is not None and grille and lu["tranche"]),
        })
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucun paiement de bus n'a été lu sur ce document."}, status=400)

    logger.info("Assistant bus : %s ligne(s) lue(s) par %s", len(lignes), request.user.username)
    return JsonResponse({
        "ok": True, "lignes": lignes,
        "intro": f"<strong>{len(lignes)} paiement(s) de bus lu(s).</strong> Corrigez si besoin (orange = à contrôler), "
                 "décochez les lignes à ne pas enregistrer, puis validez. Un reçu est créé pour chaque ligne.",
        "colonnes": [
            lot.colonne("matricule", "Matricule", largeur=140),
            lot.colonne("grille_id", "Zone", "select", [(g.id, f"{g.zone} ({g.annee_scolaire})") for g in grilles], 170),
            lot.colonne("tranche", "Tranche", "select", tranches, 170),
            lot.colonne("montant", "Montant (GNF)", "number", largeur=130),
            lot.colonne("date", "Date", "date", largeur=140),
            lot.colonne("mode_paiement_id", "Mode", "select", [(m.id, m.nom) for m in modes], 140),
            lot.colonne("reference", "Référence", largeur=130),
        ],
    })


def _importer(request, modele, formulaire, vue, donnees_de, description):
    """Boucle commune bus / cantine : une ligne = un passage par la vue habituelle."""
    ecole, refus = _ecole_ou_refus(request)
    if refus:
        return refus
    lignes, _ = lot.lignes_du_corps(request)
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucune ligne à enregistrer."}, status=400)

    eleves = formulaire(ecole=ecole).fields["eleve"].queryset
    crees, echecs = [], []
    for ligne in lignes:
        index, matricule = ligne.get("_index"), lot.texte(ligne, "matricule")
        eleve = eleves.filter(matricule__iexact=matricule).first() if matricule else None
        if eleve is None:
            echecs.append({"index": index, "nom": matricule or "(sans matricule)",
                           "erreur": "élève introuvable dans votre école : vérifiez le matricule"})
            continue
        nom = f"{eleve.prenom} {eleve.nom}"
        donnees = donnees_de(ligne, eleve)
        form = formulaire(donnees, ecole=ecole)
        if not form.is_valid():
            echecs.append({"index": index, "nom": nom, "erreur": lot.erreurs_formulaire(form)})
            continue
        dernier = modele.objects.filter(eleve=eleve).order_by("-pk").values_list("pk", flat=True).first() or 0
        try:
            reponse, erreurs = lot.executer_vue(request, vue, donnees)
        except Exception:  # noqa: BLE001
            logger.exception("Assistant %s : échec pour %s", modele.__name__, nom)
            echecs.append({"index": index, "nom": nom, "erreur": "erreur à l'enregistrement"})
            continue
        cree = modele.objects.filter(eleve=eleve, pk__gt=dernier).order_by("-pk").first()
        if reponse.status_code == 302 and cree:
            crees.append({"index": index, "texte": description(nom, cree)})
        else:
            echecs.append({"index": index, "nom": nom, "erreur": " ".join(erreurs) or "enregistrement refusé"})

    logger.info("Assistant %s : %s enregistré(s), %s refusé(s), par %s",
                modele.__name__, len(crees), len(echecs), request.user.username)
    return JsonResponse({"ok": True, "crees": crees, "echecs": echecs})


def _gnf(montant) -> str:
    return f"{int(montant):,}".replace(",", " ") + " GNF"


@login_required
@require_POST
def assistant_bus_importer(request):
    from .views import abonnement_create

    def donnees(ligne, eleve):
        return {
            "eleve": eleve.pk, "grille": lot.texte(ligne, "grille_id"), "periodicite": lot.texte(ligne, "tranche"),
            "montant": lot.chiffres(ligne.get("montant")),
            "date_debut": lot.texte(ligne, "date") or timezone.localdate().isoformat(),
            "mode_paiement": lot.texte(ligne, "mode_paiement_id"),
            "reference_externe": lot.texte(ligne, "reference"), "observations": OBSERVATION,
        }

    return _importer(
        request, AbonnementBus, AbonnementBusForm, abonnement_create, donnees,
        lambda nom, abo: f"{nom} – {_gnf(abo.montant)} (reçu {abo.numero_recu})",
    )


@login_required
@require_POST
def assistant_bus_excel(request):
    ecole, refus = _ecole_ou_refus(request)
    if refus:
        return refus
    lignes, _ = lot.lignes_du_corps(request)
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucune ligne à exporter."}, status=400)
    form, grilles, tranches, modes = _options_bus(ecole)
    eleves = {e.matricule.upper(): e for e in form.fields["eleve"].queryset if e.matricule}
    zones = {str(g.id): f"{g.zone} ({g.annee_scolaire})" for g in grilles}
    libelles_tranches, libelles_modes = dict(tranches), {str(m.id): m.nom for m in modes}

    def ligne_excel(l):
        e = eleves.get(lot.texte(l, "matricule").upper())
        montant = lot.chiffres(l.get("montant"))
        return [
            lot.texte(l, "matricule"), f"{e.prenom} {e.nom}" if e else "", e.classe.nom if e and e.classe else "",
            zones.get(lot.texte(l, "grille_id"), ""), libelles_tranches.get(lot.texte(l, "tranche"), ""),
            int(montant) if montant else "", lot.texte(l, "date"),
            libelles_modes.get(lot.texte(l, "mode_paiement_id"), ""), lot.texte(l, "reference"),
        ]

    return lot.reponse_excel(
        "bus_assistant.xlsx", "Bus",
        ["Matricule", "Élève", "Classe", "Zone", "Tranche", "Montant (GNF)", "Date", "Mode de paiement", "Référence"],
        [ligne_excel(l) for l in lignes],
    )


# --------------------------------------------------------------------- CANTINE

PERIODES_CANTINE = list(AbonnementCantine.Periodicite.choices)
REPAS_CANTINE = list(AbonnementCantine.TypeRepas.choices)


@login_required
@require_POST
def assistant_cantine(request):
    ecole, refus = _ecole_ou_refus(request)
    if refus:
        return refus
    schema = lot.schema_liste("abonnements", {
        "eleve_nom": {"type": "string"}, "matricule": {"type": "string"}, "classe": {"type": "string"},
        "montant": lot.NOMBRE_OU_NUL, "date": {"type": "string"},
        "periodicite": {"type": "string", "enum": [c for c, _ in PERIODES_CANTINE] + [""]},
        "type_repas": {"type": "string", "enum": [c for c, _ in REPAS_CANTINE] + [""]},
        "reference": {"type": "string"},
    })
    liste_periodes = "\n".join(f"{c} | {l}" for c, l in PERIODES_CANTINE)
    liste_repas = "\n".join(f"{c} | {l}" for c, l in REPAS_CANTINE)
    entete = _entete("des abonnements de cantine")
    consigne = f"""{entete}

Périodicités possibles (code | libellé) :
{liste_periodes}

Types de repas possibles (code | libellé) :
{liste_repas}

Pour chaque abonnement :
{REGLES_ELEVE.format(aujourdhui=timezone.localdate().isoformat())}
  (date = date de début de l'abonnement ou, à défaut, date du paiement)
- periodicite : le code de la liste (mois → MENSUEL, trimestre → TRIMESTRIEL…), "" si inconnue.
- type_repas : le code de la liste, "" si le document ne le dit pas.
{lot.REGLES_COMMUNES}"""

    try:
        resultat = _lire(request, consigne, schema)
    except AssistantIAError as exc:
        return JsonResponse({"ok": False, "erreur": str(exc)}, status=400)

    eleves = AbonnementCantineForm(ecole=ecole).fields["eleve"].queryset
    lignes = []
    for lu in resultat.get("abonnements", [])[:lot.MAX_LIGNES]:
        douteux = list(lu["champs_douteux"])
        eleve, suggestions = lot.trouver_eleve(eleves, lu["eleve_nom"], lu["matricule"], lu["classe"])
        matricule, debut, alerte = lot.cellule_eleve(eleve, suggestions, lu["eleve_nom"])
        montant = lu["montant"] if lu["montant"] is not None and lu["montant"] >= 0 else None
        for cle, manque in (("matricule", not eleve or "eleve_nom" in douteux), ("montant", montant is None),
                            ("periodicite", not lu["periodicite"]), ("type_repas", not lu["type_repas"])):
            if manque:
                douteux.append(cle)
        lignes.append({
            "valeurs": {
                "matricule": matricule or lu["matricule"], "montant": "" if montant is None else int(montant),
                "date": _date(lu["date"]) or timezone.localdate().isoformat(),
                "periodicite": lu["periodicite"] or "MENSUEL", "type_repas": lu["type_repas"] or "DEJEUNER",
                "reference": lu["reference"],
            },
            "douteux": douteux, "remarque": f"{debut} {lu['remarque']}".strip(), "alerte": alerte,
            "coche": bool(eleve and montant is not None),
        })
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucun abonnement de cantine n'a été lu sur ce document."}, status=400)

    logger.info("Assistant cantine : %s ligne(s) lue(s) par %s", len(lignes), request.user.username)
    return JsonResponse({
        "ok": True, "lignes": lignes,
        "intro": f"<strong>{len(lignes)} abonnement(s) de cantine lu(s).</strong> Corrigez si besoin (orange = à contrôler), "
                 "décochez les lignes à ne pas enregistrer, puis validez.",
        "colonnes": [
            lot.colonne("matricule", "Matricule", largeur=140),
            lot.colonne("periodicite", "Périodicité", "select", PERIODES_CANTINE, 150),
            lot.colonne("type_repas", "Repas", "select", REPAS_CANTINE, 190),
            lot.colonne("montant", "Montant (GNF)", "number", largeur=130),
            lot.colonne("date", "Début", "date", largeur=140),
            lot.colonne("reference", "Référence", largeur=130),
        ],
    })


@login_required
@require_POST
def assistant_cantine_importer(request):
    from .views_cantine import creer_abonnement_cantine

    def donnees(ligne, eleve):
        return {
            "eleve": eleve.pk, "montant": lot.chiffres(ligne.get("montant")),
            "periodicite": lot.texte(ligne, "periodicite"), "type_repas": lot.texte(ligne, "type_repas"),
            "date_debut": lot.texte(ligne, "date") or timezone.localdate().isoformat(),
            "statut": "ACTIF", "alerte_avant_jours": "7",
            "reference_externe": lot.texte(ligne, "reference"), "observations": OBSERVATION,
        }

    return _importer(
        request, AbonnementCantine, AbonnementCantineForm, creer_abonnement_cantine, donnees,
        lambda nom, abo: f"{nom} – {_gnf(abo.montant)}, {abo.get_periodicite_display()} "
                         "(jusqu'au " + abo.date_expiration.strftime("%d/%m/%Y") + ")",
    )


@login_required
@require_POST
def assistant_cantine_excel(request):
    ecole, refus = _ecole_ou_refus(request)
    if refus:
        return refus
    lignes, _ = lot.lignes_du_corps(request)
    if not lignes:
        return JsonResponse({"ok": False, "erreur": "Aucune ligne à exporter."}, status=400)
    eleves = {e.matricule.upper(): e for e in AbonnementCantineForm(ecole=ecole).fields["eleve"].queryset if e.matricule}
    periodes, repas = dict(PERIODES_CANTINE), dict(REPAS_CANTINE)

    def ligne_excel(l):
        e = eleves.get(lot.texte(l, "matricule").upper())
        montant = lot.chiffres(l.get("montant"))
        return [
            lot.texte(l, "matricule"), f"{e.prenom} {e.nom}" if e else "", e.classe.nom if e and e.classe else "",
            periodes.get(lot.texte(l, "periodicite"), ""), repas.get(lot.texte(l, "type_repas"), ""),
            int(montant) if montant else "", lot.texte(l, "date"), lot.texte(l, "reference"),
        ]

    return lot.reponse_excel(
        "cantine_assistant.xlsx", "Cantine",
        ["Matricule", "Élève", "Classe", "Périodicité", "Repas", "Montant (GNF)", "Début", "Référence"],
        [ligne_excel(l) for l in lignes],
    )
