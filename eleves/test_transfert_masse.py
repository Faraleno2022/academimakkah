"""Liste des élèves : filtre par classe et transfert groupé vers une autre classe."""
import re
from datetime import date
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from eleves.models import Classe, Ecole, Eleve, HistoriqueEleve, Responsable
from utilisateurs.models import Profil

MIDDLEWARE_SANS_LICENCE = [m for m in settings.MIDDLEWARE if 'licence_middleware' not in m]


def scripts_es5(html, marqueur):
    """Blocs <script> portant le marqueur, sans commentaires. Un seul « let »,
    « const » ou fonction fléchée et Safari 9 (vieux iPad) ignore tout le bloc."""
    blocs = [b for b in re.findall(r'<script>(.*?)</script>', html, re.S) if marqueur in b]
    return [re.sub(r'//[^\n]*', '', re.sub(r'/\*.*?\*/', '', b, flags=re.S)) for b in blocs]


INTERDITS_ES5 = (r'\blet\s', r'\bconst\s', r'=>', r'`', r'\.\.\.', r'\.closest\(',
                 r'requestSubmit', r'querySelectorAll\([^)]*\)\.forEach')


@override_settings(MIDDLEWARE=MIDDLEWARE_SANS_LICENCE)
class TransfertMasseTest(TestCase):
    def setUp(self):
        User = get_user_model()
        self.ecole = Ecole.objects.create(
            nom='École Transfert', adresse='Conakry', telephone='+224620600001', directeur='Direction',
        )
        self.autre_ecole = Ecole.objects.create(
            nom='École Voisine', adresse='Kindia', telephone='+224620600002', directeur='Direction',
        )
        self.classe_a = Classe.objects.create(
            ecole=self.ecole, nom='CP1 A', niveau='PRIMAIRE_1', annee_scolaire='2025-2026',
        )
        self.classe_b = Classe.objects.create(
            ecole=self.ecole, nom='CP1 B', niveau='PRIMAIRE_1', annee_scolaire='2025-2026',
        )
        self.classe_ancienne = Classe.objects.create(
            ecole=self.ecole, nom='CP1 C', niveau='PRIMAIRE_1', annee_scolaire='2024-2025',
        )
        self.classe_etrangere = Classe.objects.create(
            ecole=self.autre_ecole, nom='CP1 X', niveau='PRIMAIRE_1', annee_scolaire='2025-2026',
        )
        self.responsable = Responsable.objects.create(
            prenom='Parent', nom='Transfert', relation='PERE', telephone='+224620600003', adresse='Conakry',
        )
        self.e1 = self._eleve('TR-001', 'Aminata', 'Camara', self.classe_a)
        self.e2 = self._eleve('TR-002', 'Mamadou', 'Diallo', self.classe_a)
        self.e3 = self._eleve('TR-003', 'Fatoumata', 'Bah', self.classe_b)
        self.etranger = self._eleve('TR-900', 'Ibrahima', 'Sow', self.classe_etrangere)

        self.directeur = User.objects.create_user('directeur-transfert', 'd@t.gn', 'x')
        Profil.objects.filter(user=self.directeur).update(
            role='DIRECTEUR', ecole=self.ecole, is_validated=True, actif=True,
        )
        self.directeur = User.objects.get(pk=self.directeur.pk)
        self.client.force_login(self.directeur)

    def _eleve(self, matricule, prenom, nom, classe):
        return Eleve.objects.create(
            matricule=matricule, prenom=prenom, nom=nom, sexe='F',
            date_naissance=date(2018, 1, 1), classe=classe, date_inscription=date(2025, 9, 1),
            responsable_principal=self.responsable, statut='ACTIF',
        )

    def _transferer(self, eleves, classe, **extra):
        donnees = {
            'eleve_id': [str(e.pk) for e in eleves],
            'classe_destination': str(classe.pk),
            'next': reverse('eleves:liste_eleves') + '?classe_id=%s' % self.classe_a.pk,
        }
        donnees.update(extra)
        return self.client.post(reverse('eleves:transferer_eleves_masse'), donnees)

    # --- Filtre par classe -------------------------------------------------

    def test_filtre_par_classe_n_affiche_que_ses_eleves_et_reste_selectionne(self):
        response = self.client.get(reverse('eleves:liste_eleves'), {'classe_id': self.classe_a.pk})
        self.assertEqual(response.status_code, 200)
        affiches = {e.pk for e in response.context['page_obj']}
        self.assertEqual(affiches, {self.e1.pk, self.e2.pk})
        self.assertEqual(response.context['selected_classe_id'], str(self.classe_a.pk))
        # La pagination et le retour après transfert conservent le filtre.
        self.assertIn('classe_id=%s' % self.classe_a.pk, response.context['filtres_url'])
        self.assertContains(response, 'value="%s" selected' % self.classe_a.pk)

    def test_filtre_par_classe_se_combine_avec_la_recherche(self):
        response = self.client.get(reverse('eleves:liste_eleves'), {
            'classe_id': self.classe_a.pk, 'recherche': 'DIALLO',
        })
        self.assertEqual([e.pk for e in response.context['page_obj']], [self.e2.pk])

    def test_filtre_avec_une_classe_d_une_autre_ecole_est_ignore(self):
        response = self.client.get(reverse('eleves:liste_eleves'), {'classe_id': self.classe_etrangere.pk})
        affiches = {e.pk for e in response.context['page_obj']}
        self.assertEqual(affiches, {self.e1.pk, self.e2.pk, self.e3.pk})
        self.assertEqual(response.context['selected_classe_id'], '')
        self.assertNotIn('classe_id', response.context['filtres_url'])

    def test_la_page_propose_le_transfert_et_tout_cocher(self):
        response = self.client.get(reverse('eleves:liste_eleves'))
        self.assertContains(response, 'id="form-transfert-masse"')
        self.assertContains(response, 'id="select-all-eleves"')
        self.assertContains(response, 'name="eleve_id" form="form-transfert-masse"')
        blocs = scripts_es5(response.content.decode(), 'form-transfert-masse')
        self.assertEqual(len(blocs), 1)
        for interdit in INTERDITS_ES5:
            self.assertNotRegex(blocs[0], interdit)

    # --- Transfert groupé ----------------------------------------------------

    def test_transfert_de_plusieurs_eleves_change_classe_matricule_et_historique(self):
        anciens_matricules = {self.e1.pk: self.e1.matricule, self.e2.pk: self.e2.matricule}
        response = self._transferer([self.e1, self.e2], self.classe_b)
        self.assertRedirects(
            response, reverse('eleves:liste_eleves') + '?classe_id=%s' % self.classe_a.pk,
            fetch_redirect_response=False,
        )
        for eleve in (self.e1, self.e2):
            eleve.refresh_from_db()
            self.assertEqual(eleve.classe_id, self.classe_b.pk)
            self.assertNotEqual(eleve.matricule, anciens_matricules[eleve.pk])
            historique = HistoriqueEleve.objects.get(eleve=eleve, action='CHANGEMENT_CLASSE')
            self.assertIn('CP1 A', historique.description)
            self.assertIn('CP1 B', historique.description)
            self.assertEqual(historique.utilisateur, self.directeur)
        textes = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('2 élève(s) transféré(s) vers CP1 B' in t for t in textes), textes)
        # Aucune classe de notes configurée ici : un seul avertissement pour la classe.
        self.assertEqual(sum('Aucune configuration de notes pour CP1 B' in t for t in textes), 1, textes)
        self.assertEqual(sum('Aucune grille tarifaire pour CP1 B' in t for t in textes), 1, textes)

    def test_transfert_par_lots_enchaines(self):
        with patch('eleves.views.TAILLE_LOT_TRANSFERT', 1):
            response = self._transferer([self.e1, self.e2], self.classe_b)
            # Premier lot traité : la page de progression porte la suite.
            self.assertEqual(response.status_code, 200)
            self.assertTemplateUsed(response, 'eleves/transfert_masse_progression.html')
            ctx = response.context
            self.assertEqual((ctx['traites'], ctx['total']), (1, 2))
            blocs = scripts_es5(response.content.decode(), 'suite-transfert')
            self.assertEqual(len(blocs), 1)
            for interdit in INTERDITS_ES5:
                self.assertNotRegex(blocs[0], interdit)
            response = self.client.post(reverse('eleves:transferer_eleves_masse'), {
                'classe_destination': str(self.classe_b.pk),
                'eleve_ids': ctx['restants'],
                'transferes': ctx['transferes'],
                'echecs': ctx['echecs'],
                'avertissements': ctx['avertissements'],
                'next': ctx['next'],
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            set(Eleve.objects.filter(classe=self.classe_b).values_list('pk', flat=True)),
            {self.e1.pk, self.e2.pk, self.e3.pk},
        )
        textes = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('2 élève(s) transféré(s)' in t for t in textes), textes)

    def test_recapitulatif_nomme_les_echecs(self):
        # e3 est déjà dans la classe choisie, l'élève étranger n'est pas visible.
        response = self._transferer([self.e1, self.e3, self.etranger], self.classe_b)
        self.assertEqual(response.status_code, 302)
        self.e1.refresh_from_db()
        self.etranger.refresh_from_db()
        self.assertEqual(self.e1.classe_id, self.classe_b.pk)
        self.assertEqual(self.etranger.classe_id, self.classe_etrangere.pk)
        textes = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('1 élève(s) transféré(s)' in t for t in textes), textes)
        echec = next(t for t in textes if 'non transféré' in t)
        self.assertIn('2 élève(s) non transféré(s)', echec)
        # Les noms sont mis en majuscules à l'enregistrement.
        self.assertIn('FATOUMATA BAH (déjà dans cette classe)', echec)
        self.assertIn('IBRAHIMA SOW (introuvable', echec)

    # --- Refus -------------------------------------------------------------

    def test_refus_pour_un_sous_utilisateur_sans_le_menu_eleves(self):
        User = get_user_model()
        secretaire = User.objects.create_user('secretaire-transfert', 's@t.gn', 'x')
        Profil.objects.filter(user=secretaire).update(
            role='SECRETAIRE', ecole=self.ecole, is_validated=True, actif=True,
            compte_principal=self.directeur.profil, allowed_menus=['paiements'],
        )
        self.client.force_login(User.objects.get(pk=secretaire.pk))
        response = self._transferer([self.e1], self.classe_b)
        self.assertEqual(response.status_code, 403)
        self.e1.refresh_from_db()
        self.assertEqual(self.e1.classe_id, self.classe_a.pk)

    def test_refus_de_la_vue_pour_un_role_sans_droit_meme_hors_middleware(self):
        """La vue refuse d'elle-même, sans compter sur le middleware des menus."""
        User = get_user_model()
        secretaire = User.objects.create_user('secretaire-sans-menu', 's2@t.gn', 'x')
        Profil.objects.filter(user=secretaire).update(
            role='SECRETAIRE', ecole=self.ecole, is_validated=True, actif=True,
            compte_principal=self.directeur.profil, allowed_menus=['paiements'],
        )
        self.client.force_login(User.objects.get(pk=secretaire.pk))
        sans_menus = [m for m in MIDDLEWARE_SANS_LICENCE if 'MenuPermissionMiddleware' not in m]
        with self.settings(MIDDLEWARE=sans_menus):
            response = self._transferer([self.e1], self.classe_b)
        self.assertEqual(response.status_code, 403)
        self.e1.refresh_from_db()
        self.assertEqual(self.e1.classe_id, self.classe_a.pk)

    def test_refus_en_lecture_seule(self):
        Profil.objects.filter(user=self.directeur).update(lecture_seule=True)
        response = self._transferer([self.e1], self.classe_b)
        self.assertNotEqual(response.status_code, 200)
        self.e1.refresh_from_db()
        self.assertEqual(self.e1.classe_id, self.classe_a.pk)
        self.assertFalse(HistoriqueEleve.objects.filter(eleve=self.e1, action='CHANGEMENT_CLASSE').exists())
        # La barre de transfert n'est pas proposée en consultation.
        self.assertNotContains(self.client.get(reverse('eleves:liste_eleves')), 'id="form-transfert-masse"')

    def test_refus_d_une_classe_d_une_autre_ecole(self):
        response = self._transferer([self.e1, self.e2], self.classe_etrangere)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Eleve.objects.filter(classe=self.classe_a).count(), 2)
        textes = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any('Classe de destination invalide' in t for t in textes), textes)

    def test_refus_d_une_classe_d_une_autre_annee(self):
        response = self._transferer([self.e1, self.e2], self.classe_ancienne)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Eleve.objects.filter(classe=self.classe_a).count(), 2)
        self.assertFalse(HistoriqueEleve.objects.filter(action='CHANGEMENT_CLASSE').exists())

    def test_get_refuse(self):
        response = self.client.get(reverse('eleves:transferer_eleves_masse'))
        self.assertEqual(response.status_code, 405)
