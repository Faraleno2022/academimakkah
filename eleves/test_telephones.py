"""Deux numéros enregistrés ensemble, édition rapide et périmètre de l'école."""
from datetime import date
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from paiements.tests.support import MIDDLEWARE_SANS_LICENCE
from synchronisation.engine import build_change_instance, serialize_instance
from .forms import ResponsableForm
from .models import Classe, Ecole, Eleve, Responsable


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class TelephonesResponsableTests(TestCase):
    def setUp(self):
        self.ecole = Ecole.objects.create(nom='École téléphones', adresse='Conakry', telephone='600111222', directeur='D')
        classe = Classe.objects.create(ecole=self.ecole, nom='CE1', niveau='PRIMAIRE_2', annee_scolaire='2026-2027')
        self.parent = Responsable.objects.create(prenom='Parent', nom='Camara', relation='PERE',
            telephone='+224620111222', adresse='Conakry')
        self.eleve = Eleve.objects.create(classe=classe, matricule='TEL-001', prenom='Awa', nom='Camara',
            date_naissance=date(2018, 1, 1), responsable_principal=self.parent)
        self.user = get_user_model().objects.create_user('admin-telephones', password='test')
        profil = self.user.profil
        profil.ecole, profil.role, profil.is_validated, profil.allowed_menus = self.ecole, 'ADMIN', True, ['eleves']
        profil.save()
        self.client.force_login(self.user)
        self.url = reverse('eleves:ajax_modifier_telephone_responsable')

    def envoyer(self, **donnees):
        return self.client.post(self.url, json.dumps({'eleve_id': self.eleve.pk,
            'telephone': '+224620111222', **donnees}), content_type='application/json')

    def test_ajout_et_rechargement_des_deux_numeros(self):
        response = self.envoyer(telephone='620 222 333', telephone_secondaire='+224 622-444-555')
        self.assertEqual(response.status_code, 200)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.telephone, '+224620222333')
        self.assertEqual(self.parent.telephone_secondaire, '+224622444555')
        page = self.client.get(reverse('eleves:liste_eleves'))
        self.assertContains(page, '+224622444555')
        self.assertContains(page, 'Téléphone 2 (facultatif)')
        self.assertEqual(serialize_instance(self.parent)['telephone_secondaire'], '+224622444555')

    def test_effacement_du_second_ne_supprime_pas_le_premier(self):
        self.parent.telephone_secondaire = '+224622444555'
        self.parent.save()
        response = self.envoyer(telephone_secondaire='')
        self.assertEqual(response.status_code, 200)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.telephone, '+224620111222')
        self.assertEqual(self.parent.telephone_secondaire, '')

    def test_deuxieme_invalide_ne_modifie_aucun_numero(self):
        response = self.envoyer(telephone='+224620222333', telephone_secondaire='pas un numéro')
        self.assertEqual(response.status_code, 400)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.telephone, '+224620111222')
        self.assertEqual(self.parent.telephone_secondaire, '')

    def test_ancien_client_omet_second_numero_et_le_conserve(self):
        self.parent.telephone_secondaire = '+224622444555'
        self.parent.save()
        self.assertEqual(self.envoyer().status_code, 200)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.telephone_secondaire, '+224622444555')

    def test_admin_ecole_ne_modifie_pas_un_eleve_d_une_autre_ecole(self):
        autre = Ecole.objects.create(nom='Autre école', adresse='Conakry', telephone='600111444', directeur='D')
        classe = Classe.objects.create(ecole=autre, nom='CE1', niveau='PRIMAIRE_2', annee_scolaire='2026-2027')
        self.eleve.classe = classe
        self.eleve.save()
        self.assertEqual(self.envoyer(telephone_secondaire='+224622444555').status_code, 403)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.telephone_secondaire, '')

    def test_json_invalide_rejete(self):
        for valeur in ([], None, {'telephone': 123}, {'eleve_id': {}, 'telephone': '+224620111222'}):
            response = self.client.post(self.url, json.dumps(valeur), content_type='application/json')
            self.assertEqual(response.status_code, 400)

    def test_formulaire_ancien_conserve_numero_non_affiche(self):
        self.parent.telephone_secondaire = '+224622444555'
        self.parent.save()
        form = ResponsableForm({'prenom': 'Parent', 'nom': 'Camara', 'relation': 'PERE',
            'telephone': '620111222', 'adresse': 'Conakry'}, instance=self.parent)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.telephone_secondaire, '+224622444555')

    def test_synchronisation_recoit_le_deuxieme_numero_et_ancien_payload_le_conserve(self):
        payload = serialize_instance(self.parent)
        payload['telephone_secondaire'] = '+224622444555'
        build_change_instance('eleves.Responsable', self.parent.sync_uuid, payload, ecole=self.ecole, trusted=True)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.telephone_secondaire, '+224622444555')
        del payload['telephone_secondaire']
        build_change_instance('eleves.Responsable', self.parent.sync_uuid, payload, ecole=self.ecole, trusted=True)
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.telephone_secondaire, '+224622444555')
