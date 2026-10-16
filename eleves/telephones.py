"""Validation commune aux deux numéros de l'édition rapide."""
import re


def normaliser_telephone(valeur, obligatoire=True):
    if not isinstance(valeur, str):
        raise ValueError('Le téléphone doit être du texte.')
    telephone = re.sub(r'[\s().-]', '', valeur)
    if not telephone and not obligatoire:
        return ''
    if not re.fullmatch(r'(?:\+?224)?[0-9]{8,9}', telephone):
        raise ValueError('Format invalide. Utilisez 8 à 9 chiffres ou +224XXXXXXXXX.')
    chiffres = telephone.lstrip('+')
    if chiffres.startswith('224') and len(chiffres) in (11, 12):
        chiffres = chiffres[3:]
    return '+224' + chiffres
