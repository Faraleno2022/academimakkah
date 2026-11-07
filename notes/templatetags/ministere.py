"""Nom du ministère de tutelle pour les gabarits HTML (bulletins, certificats).

Usage : {% load ministere %} puis {% ministere_nom %} ou {% ministere_nom sigle=True %}.
"""
from django import template

from ecole_moderne.ministere import MINISTERE_NOM, MINISTERE_NOM_SIGLE

register = template.Library()


@register.simple_tag
def ministere_nom(sigle=False, majuscules=False):
    nom = MINISTERE_NOM_SIGLE if sigle else MINISTERE_NOM
    return nom.upper() if majuscules else nom
