"""Tests de l'assistant IA de saisie des notes (l'appel à Claude est simulé)."""

from io import BytesIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from assistant_ia.claude import AssistantIAError, preparer_image, ressemblance
from eleves.models import Classe, Ecole, Eleve, Responsable

from .models import ClasseNote, MatiereNote


def _photo_png():
    from PIL import Image

    tampon = BytesIO()
    Image.new("RGB", (3000, 1500), "white").save(tampon, format="PNG")
    return SimpleUploadedFile("fiche.png", tampon.getvalue(), content_type="image/png")


@override_settings(
    MIDDLEWARE=[m for m in settings.MIDDLEWARE if m != 'ecole_moderne.licence_middleware.LicenceMiddleware'],
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
)
class AssistantNotesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ecole = Ecole.objects.create(nom='École assistant', adresse='Conakry', telephone='+224620000001', directeur='Direction')
        cls.autre_ecole = Ecole.objects.create(nom='Autre école', adresse='Conakry', telephone='+224620000002', directeur='Direction')
        cls.user = get_user_model().objects.create_user(username='principal-ia', password='test')
        profil = cls.user.profil
        profil.ecole, profil.est_compte_principal, profil.is_validated = cls.ecole, True, True
        profil.save()
        resp = Responsable.objects.create(prenom='Parent', nom='Test', relation='PERE', telephone='+224620000003', adresse='Conakry')

        classe = Classe.objects.create(ecole=cls.ecole, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        cls.classe_note = ClasseNote.objects.create(ecole=cls.ecole, nom='7A', niveau='COLLEGE_7', niveau_enseignement='SECONDAIRE', annee_scolaire='2026-2027')
        cls.aminata = Eleve.objects.create(matricule='7A-001', prenom='Aminata', nom='Diallo', sexe='F', classe=classe, responsable_principal=resp, statut='ACTIF')
        cls.mamadou = Eleve.objects.create(matricule='7A-002', prenom='Mamadou', nom='Bah', sexe='M', classe=classe, responsable_principal=resp, statut='ACTIF')
        cls.math = MatiereNote.objects.create(classe=cls.classe_note, nom='Mathématiques', code='MAT', coefficient=1)
        cls.fr = MatiereNote.objects.create(classe=cls.classe_note, nom='Français', code='FR', coefficient=1)

        autre_classe = Classe.objects.create(ecole=cls.autre_ecole, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        cls.autre_classe_note = ClasseNote.objects.create(ecole=cls.autre_ecole, nom='7A', niveau='COLLEGE_7', niveau_enseignement='SECONDAIRE', annee_scolaire='2026-2027')
        cls.intrus = Eleve.objects.create(matricule='X-001', prenom='Intrus', nom='Autre', sexe='M', classe=autre_classe, responsable_principal=resp, statut='ACTIF')

    def setUp(self):
        self.client.force_login(self.user)

    def _poster(self, reponse_ia, **donnees):
        with mock.patch('notes.assistant_saisie.extraire_json', return_value=reponse_ia) as appel:
            rep = self.client.post(
                reverse('notes:assistant_notes'),
                {'classe_id': self.classe_note.pk, 'texte': 'Diallo Aminata 14', **donnees},
            )
        return rep, appel

    def test_garde_seulement_la_classe_et_signale_hors_bareme(self):
        rep, appel = self._poster({
            'notes': [
                {'eleve_id': self.aminata.pk, 'matiere_id': self.math.pk, 'note': 14.5, 'absent': False, 'douteux': False, 'remarque': ''},
                {'eleve_id': self.mamadou.pk, 'matiere_id': self.fr.pk, 'note': 25, 'absent': False, 'douteux': False, 'remarque': ''},
                {'eleve_id': self.intrus.pk, 'matiere_id': self.math.pk, 'note': 10, 'absent': False, 'douteux': False, 'remarque': ''},
                {'eleve_id': self.mamadou.pk, 'matiere_id': self.math.pk, 'note': None, 'absent': True, 'douteux': False, 'remarque': ''},
            ],
            'non_reconnus': ['Sow Ibrahima 12'],
        })
        self.assertEqual(rep.status_code, 200, rep.content)
        data = rep.json()
        self.assertTrue(data['ok'])
        self.assertEqual(len(data['notes']), 3)  # l'élève d'une autre école est écarté
        hors_bareme = next(n for n in data['notes'] if n['note'] == 25)
        self.assertTrue(hors_bareme['douteux'])
        self.assertEqual(data['non_reconnus'], ['Sow Ibrahima 12'])

        consigne = appel.call_args.args[0]
        self.assertIn('AMINATA DIALLO', consigne.upper())
        self.assertNotIn('INTRUS', consigne.upper())  # seuls les élèves de la classe sont envoyés

    def test_classe_d_une_autre_ecole_refusee(self):
        with mock.patch('notes.assistant_saisie.extraire_json') as appel:
            rep = self.client.post(reverse('notes:assistant_notes'), {'classe_id': self.autre_classe_note.pk, 'texte': 'x'})
        self.assertEqual(rep.status_code, 404)
        appel.assert_not_called()

    def test_erreur_assistant_renvoyee_lisiblement(self):
        with mock.patch('notes.assistant_saisie.extraire_json', side_effect=AssistantIAError('Pas de connexion internet vers l\'assistant.')):
            rep = self.client.post(reverse('notes:assistant_notes'), {'classe_id': self.classe_note.pk, 'texte': 'x'})
        self.assertEqual(rep.status_code, 400)
        self.assertIn('connexion', rep.json()['erreur'])

    def test_rien_n_est_enregistre(self):
        from .models import CompositionNote, NoteMensuelle

        self._poster({'notes': [{'eleve_id': self.aminata.pk, 'matiere_id': self.math.pk, 'note': 12, 'absent': False, 'douteux': False, 'remarque': ''}], 'non_reconnus': []})
        self.assertFalse(NoteMensuelle.objects.exists())
        self.assertFalse(CompositionNote.objects.exists())

    def test_la_page_de_saisie_affiche_le_bouton(self):
        rep = self.client.get(reverse('notes:saisie_intelligente'), {'classe_id': self.classe_note.pk, 'type_note': 'composition', 'periode': 'TRIMESTRE_1'})
        self.assertEqual(rep.status_code, 200)
        self.assertContains(rep, 'Remplir depuis une photo')
        self.assertContains(rep, reverse('notes:assistant_notes'))


class OutilsAssistantTests(TestCase):
    def test_photo_reduite_et_convertie(self):
        import base64
        from PIL import Image

        media_type, data = preparer_image(_photo_png())
        self.assertEqual(media_type, 'image/jpeg')
        image = Image.open(BytesIO(base64.b64decode(data)))
        self.assertLessEqual(max(image.size), 2000)

    def test_fichier_qui_n_est_pas_une_image(self):
        with self.assertRaises(AssistantIAError):
            preparer_image(SimpleUploadedFile('a.png', b'pas une image', content_type='image/png'))

    def test_ressemblance_ordre_et_accents(self):
        self.assertGreater(ressemblance('BAH Mamadou', 'Mamadou Bah'), 0.95)
        self.assertGreater(ressemblance('Aissatou Camara', 'Aïssatou Camara'), 0.95)
        self.assertLess(ressemblance('Mamadou Bah', 'Fatoumata Sow'), 0.6)
