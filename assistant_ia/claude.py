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


def preparer_image(fichier) -> tuple[str, str]:
    """
    Lit un fichier envoyé (UploadedFile Django), le réduit si besoin et renvoie
    (media_type, données base64) prêts pour l'API.
    """
    if fichier.size > TAILLE_MAX_FICHIER:
        raise AssistantIAError("La photo est trop lourde (15 Mo maximum).")

    from PIL import Image, ImageOps  # Pillow est déjà une dépendance de l'application

    try:
        image = Image.open(fichier)
        image = ImageOps.exif_transpose(image)  # remet droites les photos de téléphone
    except Exception as exc:  # noqa: BLE001
        raise AssistantIAError("Le fichier n'est pas une image lisible (JPG ou PNG).") from exc

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

    contenu = [
        {"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}}
        for mt, data in images
    ]
    if texte.strip():
        contenu.append({"type": "text", "text": f"Texte saisi par l'utilisateur :\n{texte.strip()}"})
    if not contenu:
        raise AssistantIAError("Ajoutez une photo ou un texte à analyser.")
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
