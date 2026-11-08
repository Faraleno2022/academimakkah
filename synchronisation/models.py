import hashlib
import secrets
import uuid
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.db import models
from django.utils.crypto import constant_time_compare
from django.utils import timezone

from eleves.models import Ecole
from .mixins import SyncTrackedModel


# Jetons déjà vérifiés, par (appareil, empreinte stockée). check_password
# (PBKDF2, un million d'itérations) coûte 1 à 2 s : le refaire à chaque
# requête, toutes les quelques secondes et pour chaque poste, retardait la
# synchronisation et occupait le serveur. Le jeton est aléatoire (32 octets),
# un SHA-256 suffit pour reconnaître ensuite le même jeton. Changer le jeton
# change la clé : l'ancien n'est plus reconnu.
_JETONS_VERIFIES = {}

# Code de liaison d'un poste à distance : court (dicté au téléphone ou envoyé
# par WhatsApp), sans caractères ambigus (0/O, 1/I/L), à usage unique.
ALPHABET_CODE_LIAISON = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
LONGUEUR_CODE_LIAISON = 8
DUREE_CODE_LIAISON = timedelta(days=3)


def normaliser_code_liaison(code):
    return ''.join(c for c in str(code or '').upper() if c in ALPHABET_CODE_LIAISON)


def empreinte_code_liaison(code):
    return hashlib.sha256(normaliser_code_liaison(code).encode('utf-8')).hexdigest()


class SyncDevice(models.Model):
    ecole = models.ForeignKey(Ecole, on_delete=models.CASCADE, related_name='sync_devices')
    nom = models.CharField(max_length=120)
    device_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    token_hash = models.CharField(max_length=255)
    actif = models.BooleanField(default=True, db_index=True)
    derniere_connexion = models.DateTimeField(null=True, blank=True)
    # Liaison à distance : seule l'empreinte du code est conservée.
    code_liaison_hash = models.CharField(max_length=64, blank=True, db_index=True)
    code_liaison_expire = models.DateTimeField(null=True, blank=True)
    lie_le = models.DateTimeField(null=True, blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Appareil de synchronisation'
        verbose_name_plural = 'Appareils de synchronisation'
        indexes = [
            models.Index(fields=['ecole', 'actif']),
            models.Index(fields=['derniere_connexion']),
        ]

    def definir_token(self, token):
        self.token_hash = make_password(token)

    def verifier_token(self, token):
        if not token:
            return False
        empreinte = hashlib.sha256(token.encode('utf-8')).hexdigest()
        cle = (self.pk, self.token_hash)
        connue = _JETONS_VERIFIES.get(cle)
        if connue is not None:
            return constant_time_compare(connue, empreinte)
        if not check_password(token, self.token_hash):
            return False
        _JETONS_VERIFIES[cle] = empreinte
        return True

    def nouveau_code_liaison(self):
        """Prépare un code de liaison (affiché une fois) ; à enregistrer par l'appelant."""
        code = ''.join(secrets.choice(ALPHABET_CODE_LIAISON) for _ in range(LONGUEUR_CODE_LIAISON))
        self.code_liaison_hash = empreinte_code_liaison(code)
        self.code_liaison_expire = timezone.now() + DUREE_CODE_LIAISON
        return f'{code[:4]}-{code[4:]}'

    @classmethod
    def par_code_liaison(cls, code):
        """Poste actif dont le code est valide (non utilisé, non expiré), sinon None."""
        if len(normaliser_code_liaison(code)) != LONGUEUR_CODE_LIAISON:
            return None
        return cls.objects.select_related('ecole').filter(
            code_liaison_hash=empreinte_code_liaison(code),
            code_liaison_expire__gt=timezone.now(),
            actif=True,
        ).first()

    def marquer_connexion(self):
        self.derniere_connexion = timezone.now()
        self.save(update_fields=['derniere_connexion', 'date_modification'])

    def __str__(self):
        return f'{self.nom} - {self.ecole}'


class SyncChange(models.Model):
    OPERATION_CREATE = 'CREATE'
    OPERATION_UPDATE = 'UPDATE'
    OPERATION_DELETE = 'DELETE'
    OPERATION_CHOICES = [
        (OPERATION_CREATE, 'Creation'),
        (OPERATION_UPDATE, 'Modification'),
        (OPERATION_DELETE, 'Suppression'),
    ]

    STATUT_PENDING = 'PENDING'
    STATUT_APPLIED = 'APPLIED'
    STATUT_FAILED = 'FAILED'
    STATUT_CHOICES = [
        (STATUT_PENDING, 'En attente'),
        (STATUT_APPLIED, 'Applique'),
        (STATUT_FAILED, 'Echec'),
    ]

    ecole = models.ForeignKey(Ecole, on_delete=models.CASCADE, related_name='sync_changes')
    device = models.ForeignKey(SyncDevice, on_delete=models.SET_NULL, null=True, blank=True, related_name='changes')
    # Identité du changement sur son poste, pour acquitter sans rejouer un envoi.
    source_event_id = models.UUIDField(null=True, blank=True)
    # Identifiant de réception local indexable, sans recherche dans tout le JSON.
    server_change_id = models.PositiveBigIntegerField(null=True, blank=True)
    model_label = models.CharField(max_length=120)
    object_uuid = models.UUIDField(null=True, blank=True, db_index=True)
    operation = models.CharField(max_length=10, choices=OPERATION_CHOICES)
    payload = models.JSONField(default=dict, blank=True)
    statut = models.CharField(max_length=12, choices=STATUT_CHOICES, default=STATUT_PENDING, db_index=True)
    erreur = models.TextField(blank=True)
    date_creation = models.DateTimeField(auto_now_add=True, db_index=True)
    date_application = models.DateTimeField(null=True, blank=True)
    # Ordre de diffusion aux postes, attribué seulement une fois la ligne
    # validée en base (voir synchronisation.sequence). L'id ne convient pas :
    # il est réservé à l'insertion, et un gros lot reçu d'un poste revenu en
    # ligne devient visible après des lignes d'id supérieur.
    sequence = models.PositiveBigIntegerField(null=True, blank=True, unique=True)

    class Meta:
        verbose_name = 'Changement synchronise'
        verbose_name_plural = 'Changements synchronises'
        constraints = [
            models.UniqueConstraint(fields=['device', 'source_event_id'], name='sync_device_source_event_unique'),
        ]
        indexes = [
            models.Index(fields=['ecole', 'statut', 'date_creation']),
            models.Index(fields=['ecole', 'statut', 'date_application'], name='sync_school_applied_date'),
            models.Index(fields=['model_label', 'object_uuid']),
            models.Index(fields=['ecole', 'server_change_id', 'statut'], name='sync_school_server_status'),
            models.Index(fields=['ecole', 'sequence'], name='sync_school_sequence'),
            models.Index(fields=['ecole', 'statut', 'id'], name='sync_school_status_id'),
        ]

    def __str__(self):
        return f'{self.operation} {self.model_label} ({self.statut})'


class SyncSequence(models.Model):
    """Compteur unique (pk=1) de l'ordre de diffusion ; sa ligne sert de verrou."""
    valeur = models.PositiveBigIntegerField(default=0)


class SyncOwnership(models.Model):
    """École propriétaire des objets synchronisés sans relation d'école directe."""
    ecole = models.ForeignKey(Ecole, on_delete=models.CASCADE)
    model_label = models.CharField(max_length=120)
    object_uuid = models.UUIDField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['model_label', 'object_uuid'], name='sync_object_school_unique')]


class SyncAlias(models.Model):
    """Ancien sync_uuid d'un objet fusionné avec son doublon d'un autre poste.

    Des changements déjà partis désignent encore cet identifiant : sans cette
    trace, leurs enfants (états d'une période fusionnée...) resteraient refusés.
    """
    model_label = models.CharField(max_length=120)
    ancien_uuid = models.UUIDField()
    object_pk = models.PositiveBigIntegerField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['model_label', 'ancien_uuid'], name='sync_alias_unique')]


class SyncCheckpoint(models.Model):
    """Avancement local d'un poste, conservé entre deux exécutions."""
    device_id = models.UUIDField(unique=True)
    snapshot_cursor = models.TextField(blank=True)
    initial_complete = models.BooleanField(default=False)
    last_change_id = models.PositiveBigIntegerField(default=0)
    # Version du registre déjà reçue. Un poste mis à jour rattrape l'historique
    # des modèles ajoutés depuis, que le serveur ne lui envoyait pas avant.
    registre = models.PositiveSmallIntegerField(default=1)
    rattrapage_id = models.PositiveBigIntegerField(default=0)


class SyncMediaTransfer(models.Model):
    """File durable des fichiers, indépendante des changements métier."""
    ecole = models.ForeignKey(Ecole, on_delete=models.CASCADE)
    model_label = models.CharField(max_length=120)
    object_uuid = models.UUIDField()
    field_name = models.CharField(max_length=64)
    direction = models.CharField(max_length=4, choices=[('PUSH', 'Envoi'), ('PULL', 'Réception')])
    name = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64, blank=True)
    complete = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)
    retry_at = models.DateTimeField(default=timezone.now)
    error = models.TextField(blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=['ecole', 'model_label', 'object_uuid', 'field_name', 'direction'],
            name='sync_media_object_direction',
        )]
        indexes = [models.Index(fields=['ecole', 'complete', 'retry_at'], name='sync_media_pending')]
