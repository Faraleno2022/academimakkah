"""
Appel à Claude pour lire une photo (fiche de notes, reçu, bordereau) ou un texte,
et rendre un JSON conforme à un schéma (sorties structurées).

Configuration (fichier .env, ou /home/myschoolgn/.env sur PythonAnywhere) :
    ANTHROPIC_API_KEY=sk-ant-...           obligatoire
    ASSISTANT_IA_MODEL=claude-opus-5-5     optionnel
"""

import base64
import io
import json
import logging
import os
import unicodedata
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

MODELE_PAR_DEFAUT = "claude-opus-5-5"

# Taille maximale d'une photo envoyée : au-delà, l'image est réduite (les photos de
# téléphone font souvent 4000 px et plusieurs Mo, inutile pour lire une fiche).
COTE_MAX_PX = 2000
TAILLE_MAX_FICHIER = 15 * 1024 * 1024  # refus au-delà de 15 Mo envoyés par le navigateur


class AssistantIAError(Exception):
    """Erreur à afficher telle quelle à l'utilisateur (message en français)."""


def est_configure() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


PAGES_PDF_MAX = 40
TEXTE_MAX = 150_000  # caractères tirés d'un Word / Excel / CSV
LIGNES_EXCEL_MAX = 3000


def _texte_cellule(valeur) -> str:
    """Valeur Excel -> texte lisible (14.0 -> « 14 », date -> JJ/MM/AAAA)."""
    import datetime

    if valeur is None:
        return ""
    if isinstance(valeur, datetime.datetime):
        return valeur.strftime("%d/%m/%Y") if valeur.time() == datetime.time(0) else valeur.strftime("%d/%m/%Y %H:%M")
    if isinstance(valeur, datetime.date):
        return valeur.strftime("%d/%m/%Y")
    if isinstance(valeur, float) and valeur.is_integer():
        return str(int(valeur))
    return str(valeur).strip()


def _texte_excel(fichier) -> str:
    from openpyxl import load_workbook

    try:
        classeur = load_workbook(fichier, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise AssistantIAError("Le fichier Excel est illisible (enregistrez-le au format .xlsx).") from exc
    morceaux, restantes = [], LIGNES_EXCEL_MAX
    try:
        for feuille in classeur.worksheets[:6]:
            lignes = []
            for ligne in feuille.iter_rows(values_only=True):
                cellules = [_texte_cellule(c) for c in ligne]
                while cellules and not cellules[-1]:
                    cellules.pop()
                if not any(cellules):
                    continue
                lignes.append(" | ".join(cellules))
                restantes -= 1
                if restantes <= 0:
                    break
            if lignes:
                morceaux.append(f"--- Feuille « {feuille.title} » ---\n" + "\n".join(lignes))
            if restantes <= 0:
                break
    finally:
        classeur.close()
    return "\n\n".join(morceaux)


def _texte_word(archive) -> str:
    """Texte d'un .docx : paragraphes et tableaux, dans l'ordre du document."""
    from xml.etree import ElementTree

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

    def texte(noeud) -> str:
        morceaux = []
        for n in noeud.iter():
            if n.tag == f"{W}t":
                morceaux.append(n.text or "")
            elif n.tag in (f"{W}tab",):
                morceaux.append("\t")
            elif n.tag in (f"{W}br", f"{W}cr"):
                morceaux.append("\n")
        return "".join(morceaux).strip()

    try:
        corps = ElementTree.fromstring(archive.read("word/document.xml")).find(f"{W}body")
    except Exception as exc:  # noqa: BLE001
        raise AssistantIAError("Le fichier Word est illisible (enregistrez-le au format .docx).") from exc
    lignes = []
    for bloc in list(corps) if corps is not None else []:
        if bloc.tag == f"{W}p":
            t = texte(bloc)
            if t:
                lignes.append(t)
        elif bloc.tag == f"{W}tbl":
            for rangee in bloc.iter(f"{W}tr"):
                cellules = [" ".join(texte(p) for p in c.iter(f"{W}p")).strip() for c in rangee.iter(f"{W}tc")]
                if any(cellules):
                    lignes.append(" | ".join(cellules))
    return "\n".join(lignes)


def _bloc_texte(nom: str, texte: str) -> tuple[str, str]:
    texte = (texte or "").strip()
    if not texte:
        raise AssistantIAError(f"Le fichier « {nom} » ne contient aucun texte lisible.")
    if len(texte) > TEXTE_MAX:
        raise AssistantIAError(f"Le fichier « {nom} » est trop long : gardez seulement la partie à importer.")
    return "text/plain", f"Contenu du fichier « {nom} » :\n{texte}"


def preparer_image(fichier) -> tuple[str, str]:
    """
    Prépare un fichier envoyé (UploadedFile Django) pour l'API. Renvoie (type, données) :
    - photo (JPG, PNG…)      -> ("image/jpeg", base64), réduite si besoin ;
    - PDF (texte ou scanné)  -> ("application/pdf", base64), lu directement par Claude ;
    - Word .docx, Excel .xlsx, CSV, TXT -> ("text/plain", texte extrait).
    """
    import zipfile

    nom = os.path.basename(getattr(fichier, "name", "") or "fichier")
    extension = os.path.splitext(nom)[1].lower()
    if fichier.size > TAILLE_MAX_FICHIER:
        raise AssistantIAError(f"Le fichier « {nom} » est trop lourd (15 Mo maximum).")

    entete = fichier.read(8)
    fichier.seek(0)

    if entete.startswith(b"%PDF") or extension == ".pdf":
        donnees = fichier.read()
        try:
            from pypdf import PdfReader

            pages = len(PdfReader(io.BytesIO(donnees)).pages)
        except Exception as exc:  # noqa: BLE001
            raise AssistantIAError(f"Le PDF « {nom} » est illisible ou protégé par mot de passe.") from exc
        if pages > PAGES_PDF_MAX:
            raise AssistantIAError(
                f"Le PDF « {nom} » fait {pages} pages ({PAGES_PDF_MAX} maximum) : envoyez seulement les pages utiles."
            )
        return "application/pdf", base64.standard_b64encode(donnees).decode("ascii")

    if entete.startswith(b"PK"):  # .docx et .xlsx sont des archives zip
        try:
            archive = zipfile.ZipFile(fichier)
            membres = set(archive.namelist())
        except zipfile.BadZipFile as exc:
            raise AssistantIAError(f"Le fichier « {nom} » est endommagé.") from exc
        if "word/document.xml" in membres:
            return _bloc_texte(nom, _texte_word(archive))
        if "xl/workbook.xml" in membres:
            fichier.seek(0)
            return _bloc_texte(nom, _texte_excel(fichier))
        raise AssistantIAError(f"Le format du fichier « {nom} » n'est pas pris en charge (PDF, Word, Excel ou photo).")

    if extension in (".doc", ".xls") or entete.startswith(b"\xd0\xcf\x11\xe0"):
        raise AssistantIAError(
            f"« {nom} » est à l'ancien format Word/Excel : enregistrez-le en .docx, .xlsx ou PDF, puis réessayez."
        )

    if extension in (".csv", ".txt"):
        brut = fichier.read()
        for encodage in ("utf-8-sig", "cp1252"):
            try:
                return _bloc_texte(nom, brut.decode(encodage))
            except UnicodeDecodeError:
                continue
        raise AssistantIAError(f"Le fichier « {nom} » n'est pas un texte lisible.")

    from PIL import Image, ImageOps  # Pillow est déjà une dépendance de l'application

    try:
        image = Image.open(fichier)
        image = ImageOps.exif_transpose(image)  # remet droites les photos de téléphone
    except Exception as exc:  # noqa: BLE001
        raise AssistantIAError(
            f"« {nom} » n'est pas lisible : envoyez une photo (JPG, PNG), un PDF, un Word (.docx) ou un Excel (.xlsx)."
        ) from exc

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    image.thumbnail((COTE_MAX_PX, COTE_MAX_PX))

    tampon = io.BytesIO()
    image.save(tampon, format="JPEG", quality=85)
    return "image/jpeg", base64.standard_b64encode(tampon.getvalue()).decode("ascii")


def extraire_json(consigne: str, schema: dict, images=(), texte: str = "") -> dict:
    """
    Envoie la consigne + les images + le texte à Claude et renvoie le JSON obtenu.
    Lève AssistantIAError avec un message lisible en cas de problème.
    """
    if not est_configure():
        raise AssistantIAError(
            "L'assistant n'est pas configuré : la clé ANTHROPIC_API_KEY est absente du fichier .env."
        )
    try:
        import anthropic
    except ImportError as exc:
        raise AssistantIAError(
            "La bibliothèque « anthropic » n'est pas installée (pip install anthropic)."
        ) from exc

    contenu = []
    for mt, data in images:  # fichiers préparés par preparer_image : photo, PDF ou texte extrait
        if mt == "text/plain":
            contenu.append({"type": "text", "text": data})
        elif mt == "application/pdf":
            contenu.append({"type": "document", "source": {"type": "base64", "media_type": mt, "data": data}})
        else:
            contenu.append({"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}})
    if texte.strip():
        contenu.append({"type": "text", "text": f"Texte saisi par l'utilisateur :\n{texte.strip()}"})
    if not contenu:
        raise AssistantIAError("Ajoutez une photo, un fichier (PDF, Word, Excel) ou un texte à analyser.")
    contenu.append({"type": "text", "text": "Analyse le document ci-dessus selon les consignes."})

    client = anthropic.Anthropic(timeout=120.0)
    try:
        reponse = client.beta.messages.create(
            model=os.getenv("ASSISTANT_IA_MODEL") or MODELE_PAR_DEFAUT,
            max_tokens=16000,
            system=consigne,
            messages=[{"role": "user", "content": contenu}],
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": schema},
            },
            # Si le modèle refuse par erreur (filtre de sécurité), l'API relance
            # automatiquement sur le modèle de secours recommandé.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as exc:
        raise AssistantIAError("La clé ANTHROPIC_API_KEY est invalide.") from exc
    except anthropic.RateLimitError as exc:
        raise AssistantIAError("L'assistant est très sollicité : réessayez dans une minute.") from exc
    except anthropic.APIConnectionError as exc:
        raise AssistantIAError("Pas de connexion internet vers l'assistant.") from exc
    except anthropic.APIStatusError as exc:
        logger.error("Erreur API Claude %s : %s", exc.status_code, exc.message)
        raise AssistantIAError("L'assistant a rencontré une erreur. Réessayez.") from exc

    if reponse.stop_reason == "refusal":
        raise AssistantIAError("L'assistant n'a pas pu traiter ce document.")
    if reponse.stop_reason == "max_tokens":
        raise AssistantIAError("Le document est trop long : photographiez-le en plusieurs parties.")

    texte_json = next((b.text for b in reponse.content if b.type == "text"), "")
    try:
        return json.loads(texte_json)
    except json.JSONDecodeError as exc:
        logger.error("Réponse non JSON de l'assistant : %s", texte_json[:300])
        raise AssistantIAError("Réponse illisible de l'assistant. Réessayez.") from exc


def normaliser(texte: str) -> str:
    """Minuscules, sans accents ni ponctuation : « Aïssatou BAH » -> « aissatou bah »."""
    sans_accents = unicodedata.normalize("NFKD", texte or "").encode("ascii", "ignore").decode()
    propre = "".join(c if c.isalnum() else " " for c in sans_accents.lower())
    return " ".join(propre.split())


def ressemblance(a: str, b: str) -> float:
    """Score 0..1 entre deux noms, insensible à l'ordre prénom/nom."""
    a, b = normaliser(a), normaliser(b)
    if not a or not b:
        return 0.0
    direct = SequenceMatcher(None, a, b).ratio()
    trie = SequenceMatcher(None, " ".join(sorted(a.split())), " ".join(sorted(b.split()))).ratio()
    return max(direct, trie)
