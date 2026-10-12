"""Révision des données validées, pour l'affichage et les caches de pages."""
import hashlib

from utilisateurs.utils import user_school
from .models import SyncChange


def revision_ecole(ecole, *, toutes_ecoles=False):
    if ecole is None and not toutes_ecoles:
        return ''
    ecole_id = getattr(ecole, 'pk', ecole)
    changements = SyncChange.objects.all()
    if not toutes_ecoles:
        changements = changements.filter(ecole_id=ecole_id)
    # Les deux recherches par école utilisent les index des derniers changements.
    dernier = changements.order_by('-id').values_list('id', flat=True).first() or 0
    applique = changements.filter(
        statut=SyncChange.STATUT_APPLIED, date_application__isnull=False,
    ).order_by('-date_application').values_list('date_application', flat=True).first()
    valeur = f'{ecole_id if ecole_id else "global"}:{dernier}:{applique.isoformat() if applique else ""}'
    return hashlib.blake2s(valeur.encode(), digest_size=16).hexdigest()


def revision_pour_requete(request):
    if not request.user.is_authenticated or hasattr(request, 'acces_enseignant'):
        return ''
    if not hasattr(request, '_sync_revision'):
        request._sync_revision = revision_ecole(
            user_school(request.user), toutes_ecoles=request.user.is_superuser,
        )
    return request._sync_revision


class SyncPageRevisionMiddleware:
    """Prend la révision avant la lecture des données par la vue HTML."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == 'GET' and not request.path.startswith(('/api/', '/static/', '/media/')):
            revision_pour_requete(request)
        return self.get_response(request)
