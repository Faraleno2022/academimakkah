"""Réinitialisation du mot de passe par vérification du téléphone (desktop uniquement)."""
import os
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from utilisateurs.models import Profil
from utilisateurs.security_views import DESKTOP_RESET_SESSION_KEY


@override_settings(
    MIDDLEWARE=MIDDLEWARE_SANS_LICENCE, ALLOWED_HOSTS=['testserver'], SECURE_SSL_REDIRECT=False,
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
)
class ResetMotDePasseDesktopTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('directeur', password='ancien-mdp-123')
        Profil.objects.filter(user=self.user).update(telephone='+224620000011')
        self.url = reverse('utilisateurs:password_reset_info')

    def _verifier(self, telephone, username='directeur'):
        return self.client.post(self.url, {'etape': 'verifier', 'username': username, 'telephone': telephone})

    def _modifier(self, mdp='nouveau-mdp-456'):
        return self.client.post(self.url, {'etape': 'modifier', 'new_password1': mdp, 'new_password2': mdp})

    def _mot_de_passe_change(self):
        return User.objects.get(pk=self.user.pk).check_password('nouveau-mdp-456')

    def test_serveur_web_affiche_seulement_le_contact_administrateur(self):
        with patch.dict(os.environ, {'OFFLINE_MODE': '0'}):
            self._verifier('+224620000011')
            reponse = self._modifier()
        self.assertContains(reponse, 'Contactez l')
        self.assertNotIn(DESKTOP_RESET_SESSION_KEY, self.client.session)
        self.assertFalse(self._mot_de_passe_change())

    @patch.dict(os.environ, {'OFFLINE_MODE': '1'})
    def test_telephone_du_profil_permet_de_changer_le_mot_de_passe(self):
        for saisie in ('+224620000011', '620 00 00 11', '00224620000011'):
            with self.subTest(saisie=saisie):
                self.assertRedirects(self._verifier(saisie), self.url)
        self.assertRedirects(self._modifier(), reverse('utilisateurs:login'), fetch_redirect_response=False)
        self.assertTrue(self._mot_de_passe_change())
        self.assertNotIn(DESKTOP_RESET_SESSION_KEY, self.client.session)

    @patch.dict(os.environ, {'OFFLINE_MODE': '1'})
    def test_nom_d_utilisateur_insensible_a_la_casse(self):
        self.assertRedirects(self._verifier('+224620000011', username='DIRECTEUR'), self.url)
        self._modifier()
        self.assertTrue(self._mot_de_passe_change())

    @patch.dict(os.environ, {'OFFLINE_MODE': '1'})
    def test_aucun_numero_de_secours_universel(self):
        # Ancien numéro de secours codé en dur : il ne doit plus rien ouvrir.
        self.assertContains(self._verifier('625196629'), 'incorrect')
        self.assertNotIn(DESKTOP_RESET_SESSION_KEY, self.client.session)

    @patch.dict(os.environ, {'OFFLINE_MODE': '1'})
    def test_profil_sans_telephone_refuse(self):
        Profil.objects.filter(user=self.user).update(telephone='')
        self.assertContains(self._verifier(''), 'incorrect')
        self._modifier()
        self.assertFalse(self._mot_de_passe_change())

    @patch.dict(os.environ, {'OFFLINE_MODE': '1'})
    def test_mauvais_numero_refuse(self):
        reponse = self._verifier('620999999')
        self.assertContains(reponse, 'incorrect')
        self._modifier()
        self.assertFalse(self._mot_de_passe_change())

    @patch.dict(os.environ, {'OFFLINE_MODE': '1'})
    def test_utilisateur_inconnu_refuse(self):
        self.assertContains(self._verifier('+224620000011', username='inconnu'), 'incorrect')
        self.assertNotIn(DESKTOP_RESET_SESSION_KEY, self.client.session)

    @patch.dict(os.environ, {'OFFLINE_MODE': '1'})
    def test_blocage_apres_trop_de_tentatives(self):
        for _ in range(5):
            self._verifier('620999999')
        reponse = self._verifier('+224620000011')
        self.assertContains(reponse, 'Trop de tentatives')
        self.assertNotIn(DESKTOP_RESET_SESSION_KEY, self.client.session)

    @patch.dict(os.environ, {'OFFLINE_MODE': '1'})
    def test_verification_expiree_redemande_le_telephone(self):
        self._verifier('+224620000011')
        session = self.client.session
        session[DESKTOP_RESET_SESSION_KEY] = {'user_id': self.user.pk, 'at': 0}
        session.save()
        reponse = self._modifier()
        self.assertContains(reponse, 'name="telephone"')
        self.assertFalse(self._mot_de_passe_change())

    @patch.dict(os.environ, {'OFFLINE_MODE': '1'})
    def test_confirmation_differente_refusee(self):
        self._verifier('+224620000011')
        reponse = self.client.post(self.url, {
            'etape': 'modifier', 'new_password1': 'nouveau-mdp-456', 'new_password2': 'autre-mdp-789',
        })
        self.assertContains(reponse, 'name="new_password1"')
        self.assertFalse(self._mot_de_passe_change())
