"""Recherche multi-termes utilisable dans les listes financières."""

import re

from django.db.models import Q


def filtrer_termes(queryset, texte, champs, champs_mots=()):
    """Recherche une expression dans un champ ou un nom réparti sur plusieurs champs."""
    termes = re.findall(r"[^\s,;]+", (texte or '').strip())[:12]
    if not termes:
        return queryset
    expression = ' '.join(termes)
    choix = Q()
    for champ in champs:
        choix |= Q(**{f'{champ}__icontains': expression})
    if len(termes) > 1 and champs_mots:
        noms = Q()
        for terme in termes:
            mot = Q()
            for champ in champs_mots:
                mot |= Q(**{f'{champ}__icontains': terme})
            noms &= mot
        choix |= noms
    return queryset.filter(choix)
