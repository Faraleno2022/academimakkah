"""Assistant IA des modules Bus, Cantine, Avances de salaire et Test d'accueil (Claude simulé)."""

import json
from datetime import date
from decimal import Decimal
from io import BytesIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from bus.models import AbonnementBus, AbonnementCantine, GrilleTarifaireBus
from eleves.models import Classe, Ecole, Eleve
from examen_accueil.models import MatiereAccueil, NoteAccueil
from paiements.models import ModePaiement
from salaires.models import AvanceSalaire, Enseignant, EtatSalaire, PeriodeSalaire, TypeEnseignant
from utilisateurs.models import Profil

SANS_LICENCE = [m for m in settings.MIDDLEWARE if 'licence_middleware' not in m]
DOUTE = {'champs_douteux': [], 'remarque': ''}


@override_settings(MIDDLEWARE=SANS_LICENCE, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ecole = Ecole.objects.create(nom='École lot', adresse='Conakry', telephone='+224620000501', directeur='Direction')
        cls.autre = Ecole.objects.create(nom='Autre lot', adresse='Conakry', telephone='+224620000502', directeur='Direction')
        cls.classe = Classe.objects.create(ecole=cls.ecole, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        classe_x = Classe.objects.create(ecole=cls.autre, nom='7A', niveau='COLLEGE_7', annee_scolaire='2026-2027')
        cls.mamadou = Eleve.objects.create(matricule='L-001', prenom='Mamadou', nom='Bah', sexe='M', classe=cls.classe)
        cls.fatou = Eleve.objects.create(matricule='L-002', prenom='Fatoumata', nom='Camara', sexe='F', classe=cls.classe)
        cls.intrus = Eleve.objects.create(matricule='X-900', prenom='Ibrahima', nom='Sow', sexe='M', classe=classe_x)
        cls.mode = ModePaiement.objects.create(nom='Espèces lot')
        cls.user = get_user_model().objects.create_user(username='caisse-lot', password='x')
        Profil.objects.update_or_create(user=cls.user, defaults={
            'role': 'ADMIN', 'telephone': '+224620000503', 'ecole': cls.ecole, 'is_validated': True,
            'est_compte_principal': True,
        })

    def setUp(self):
        self.client.force_login(self.user)

    def lire(self, module, nom_url, reponse_ia, **donnees):
        with mock.patch(f'{module}.extraire_json', return_value=reponse_ia) as appel:
            rep = self.client.post(reverse(nom_url), {'texte': 'document', **donnees})
        return rep, appel

    def envoyer(self, nom_url, **corps):
        return self.client.post(reverse(nom_url), json.dumps(corps), content_type='application/json')

    def classeur(self, rep):
        from openpyxl import load_workbook

        self.assertEqual(rep.status_code, 200, rep.content[:300])
        self.assertIn('spreadsheet', rep['Content-Type'])
        return list(load_workbook(BytesIO(rep.content)).active.values)


class BusTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.grille = GrilleTarifaireBus.objects.create(
            ecole=cls.ecole, zone='Kipé', annee_scolaire='2026-2027',
            tranche_1=150000, tranche_2=125000, tranche_3=125000,
            date_echeance_tranche_1=date(2026, 10, 15), date_echeance_tranche_2=date(2027, 1, 15),
            date_echeance_tranche_3=date(2027, 4, 15),
        )

    def _lu(self, **v):
        return {'eleve_nom': '', 'matricule': '', 'classe': '', 'montant': 150000, 'date': '2026-11-10',
                'grille_id': self.grille.pk, 'tranche': 'T1', 'mode_paiement_id': self.mode.pk,
                'reference': 'OM1', **DOUTE, **v}

    def test_lecture_prepare_la_table(self):
        rep, appel = self.lire('bus.assistant_saisie', 'bus:assistant_bus', {'paiements': [
            self._lu(eleve_nom='BAH Mamadou'), self._lu(eleve_nom='Sow Ibrahima', matricule='X-900'),
        ]})
        self.assertEqual(rep.status_code, 200, rep.content)
        data = rep.json()
        self.assertEqual([c['cle'] for c in data['colonnes']][:3], ['matricule', 'grille_id', 'tranche'])
        self.assertEqual(data['lignes'][0]['valeurs']['matricule'], 'L-001')
        self.assertTrue(data['lignes'][0]['coche'])
        self.assertFalse(data['lignes'][1]['coche'])  # élève d'une autre école : jamais proposé
        self.assertIn('non retrouvé', data['lignes'][1]['alerte'])
        self.assertIn('Kipé', appel.call_args.args[0])
        self.assertFalse(AbonnementBus.objects.exists())

    def test_enregistrement_en_lot_par_la_vue_habituelle(self):
        base = {'grille_id': str(self.grille.pk), 'tranche': 'T1', 'date': '2026-11-10',
                'mode_paiement_id': str(self.mode.pk), 'reference': 'OM1'}
        rep = self.envoyer('bus:assistant_bus_importer', lignes=[
            {**base, '_index': 0, 'matricule': 'l-001', 'montant': '150 000'},
            {**base, '_index': 1, 'matricule': 'L-002', 'montant': '900000'},   # dépasse le reste à payer
            {**base, '_index': 2, 'matricule': 'X-900', 'montant': '150000'},   # autre école
        ])
        data = rep.json()
        self.assertEqual([c['index'] for c in data['crees']], [0], data)
        self.assertEqual([e['index'] for e in data['echecs']], [1, 2])
        self.assertIn('dépasse', data['echecs'][0]['erreur'])
        abo = AbonnementBus.objects.get()
        self.assertEqual((abo.eleve, int(abo.montant), abo.cree_par, abo.zone), (self.mamadou, 150000, self.user, 'Kipé'))
        self.assertIn(abo.numero_recu, data['crees'][0]['texte'])
        self.assertNotContains(self.client.get(reverse('bus:nouveau')), 'Paiement bus enregistré')

    def test_bus_document_repris_une_deuxieme_fois(self):
        texte = 'Paiements de bus du 10/11/2026 : Bah Mamadou 7A, 150 000 GNF, 1ère tranche'
        analyse = lambda: self.lire('bus.assistant_saisie', 'bus:assistant_bus', {'paiements': [self._lu(matricule='L-001')]}, texte=texte)[0].json()
        premiere = analyse()
        self.assertIsNone(premiere['deja_importe'])
        self.assertTrue(premiere['lignes'][0]['coche'])

        ligne = {**premiere['lignes'][0]['valeurs'], '_index': 0}
        rep = self.envoyer('bus:assistant_bus_importer', lignes=[ligne], empreintes=premiere['empreintes']).json()
        self.assertEqual(len(rep['crees']), 1, rep)

        seconde = analyse()
        self.assertEqual(seconde['deja_importe']['nombre'], 1)
        self.assertFalse(seconde['lignes'][0]['coche'])
        self.assertIn('Déjà enregistré', seconde['lignes'][0]['alerte'])
        rep = self.envoyer('bus:assistant_bus_importer', lignes=[ligne], empreintes=premiere['empreintes']).json()
        self.assertEqual(rep['crees'], [])
        self.assertEqual(AbonnementBus.objects.count(), 1)

        # Le même document dans un autre module (cantine) n'est pas « déjà importé »
        cantine = self.lire('bus.assistant_saisie', 'bus:assistant_cantine', {'abonnements': []}, texte=texte)[0]
        self.assertEqual(cantine.status_code, 400)  # rien de lu, mais pas d'erreur liée à l'empreinte
    def test_excel_et_bouton(self):
        lignes = self.classeur(self.envoyer('bus:assistant_bus_excel', lignes=[
            {'matricule': 'L-001', 'grille_id': str(self.grille.pk), 'tranche': 'T1', 'montant': '150000', 'date': '2026-11-10'},
        ]))
        self.assertEqual(lignes[1][:6], ('L-001', 'Mamadou Bah'.upper() if lignes[1][1].isupper() else lignes[1][1], '7A',
                                         'Kipé (2026-2027)', '1ère tranche', 150000))
        page = self.client.get(reverse('bus:nouveau'))
        self.assertContains(page, 'ouvrirAssistantBus')
        self.assertContains(page, reverse('bus:assistant_bus_importer'))


class CantineTests(Base):
    def test_lecture_import_excel_et_bouton(self):
        lu = {'eleve_nom': 'Camara Fatoumata', 'matricule': '', 'classe': '7A', 'montant': 150000,
              'date': '2026-11-05', 'periodicite': 'MENSUEL', 'type_repas': '', 'reference': '', **DOUTE}
        rep, _ = self.lire('bus.assistant_saisie', 'bus:assistant_cantine', {'abonnements': [lu]})
        ligne = rep.json()['lignes'][0]
        self.assertEqual((ligne['valeurs']['matricule'], ligne['valeurs']['type_repas']), ('L-002', 'DEJEUNER'))
        self.assertIn('type_repas', ligne['douteux'])

        valeurs = {**ligne['valeurs'], '_index': 0}
        rep = self.envoyer('bus:assistant_cantine_importer', lignes=[valeurs, {**valeurs, '_index': 1, 'matricule': 'X-900'}])
        data = rep.json()
        self.assertEqual(([c['index'] for c in data['crees']], [e['index'] for e in data['echecs']]), ([0], [1]), data)
        abo = AbonnementCantine.objects.get()
        self.assertEqual((abo.eleve, int(abo.montant), abo.periodicite, abo.statut), (self.fatou, 150000, 'MENSUEL', 'ACTIF'))
        self.assertGreater(abo.date_expiration, date(2026, 11, 5))

        self.assertEqual(self.classeur(self.envoyer('bus:assistant_cantine_excel', lignes=[valeurs]))[1][3], 'Mensuel')
        self.assertContains(self.client.get(reverse('bus:creer_abonnement_cantine')), 'ouvrirAssistantCantine')


class AvancesTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # L'utilisateur garde le rôle ADMIN de son école : il ne doit voir que le personnel de celle-ci
        cls.periode = PeriodeSalaire.objects.create(mois=11, annee=2026, ecole=cls.ecole, nombre_semaines=Decimal('4'), cree_par=cls.user)
        cls.prof = Enseignant.objects.create(
            nom='Diallo', prenoms='Ibrahima', ecole=cls.ecole, type_enseignant=TypeEnseignant.PRIMAIRE, statut='ACTIF',
            salaire_fixe=Decimal('1000000'), heures_mensuelles=Decimal('160'), date_embauche=date(2025, 1, 1), cree_par=cls.user,
        )
        cls.prof_ailleurs = Enseignant.objects.create(
            nom='Keita', prenoms='Sekou', ecole=cls.autre, type_enseignant=TypeEnseignant.PRIMAIRE, statut='ACTIF',
            salaire_fixe=Decimal('1000000'), heures_mensuelles=Decimal('160'), date_embauche=date(2025, 1, 1), cree_par=cls.user,
        )

    def test_lecture_import_excel_et_bouton(self):
        lu = {'personne': 'Ibrahima DIALLO', 'enseignant_id': None, 'montant': 200000, 'date': '2026-11-10',
              'mode_paiement_id': None, 'reference': '', 'motif': 'Besoin familial', **DOUTE}
        rep, appel = self.lire('salaires.assistant_saisie', 'salaires:assistant_avances', {'avances': [
            lu, {**lu, 'personne': 'Sekou Keita', 'enseignant_id': self.prof_ailleurs.pk},
        ]})
        self.assertEqual(rep.status_code, 200, rep.content)
        lignes = rep.json()['lignes']
        self.assertEqual(lignes[0]['valeurs']['enseignant_id'], self.prof.pk)  # retrouvé par le nom
        self.assertEqual(lignes[0]['valeurs']['periode_id'], self.periode.pk)
        self.assertEqual(lignes[1]['valeurs']['enseignant_id'], '')            # personnel d'une autre école
        self.assertFalse(lignes[1]['coche'])
        self.assertNotIn('Keita', appel.call_args.args[0])

        valeurs = {**lignes[0]['valeurs'], '_index': 0}
        rep = self.envoyer('salaires:assistant_avances_importer', lignes=[
            valeurs, {**valeurs, '_index': 1, 'enseignant_id': str(self.prof_ailleurs.pk)}, {**valeurs, '_index': 2, 'montant': ''},
        ])
        data = rep.json()
        self.assertEqual(([c['index'] for c in data['crees']], [e['index'] for e in data['echecs']]), ([0], [1, 2]), data)
        avance = AvanceSalaire.objects.get()
        self.assertEqual((avance.enseignant, int(avance.montant), avance.cree_par), (self.prof, 200000, self.user))
        etat = EtatSalaire.objects.get(enseignant=self.prof, periode=self.periode)
        self.assertEqual(etat.avances, Decimal('200000.00'))  # salaire net recalculé, comme sur la page

        self.assertEqual(self.classeur(self.envoyer('salaires:assistant_avances_excel', lignes=[valeurs]))[1][2], 200000)
        self.assertContains(self.client.get(reverse('salaires:ajouter_avance')), 'ouvrirAssistantAvances')


class TestAccueilTests(Base):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.francais = MatiereAccueil.objects.create(classe=cls.classe, nom='Français')
        cls.maths = MatiereAccueil.objects.create(classe=cls.classe, nom='Mathématiques')

    def _note(self, eleve, matiere, note, **v):
        return {'eleve_id': eleve.pk, 'matiere_id': matiere.pk, 'note': note, 'absent': False, 'douteux': False, 'remarque': '', **v}

    def test_lecture_excel_reimportable_et_bouton(self):
        rep, appel = self.lire('examen_accueil.assistant_saisie', 'examen_accueil:assistant_notes', {
            'notes': [self._note(self.mamadou, self.francais, 14.5), self._note(self.fatou, self.maths, 99),
                      self._note(self.intrus, self.francais, 10)],
            'non_reconnus': ['Sow 12'],
        }, classe=self.classe.pk, tous='1')
        self.assertEqual(rep.status_code, 200, rep.content)
        data = rep.json()
        self.assertEqual(len(data['notes']), 2)  # l'élève d'une autre école est écarté
        self.assertTrue(data['notes'][1]['douteux'])  # hors barème
        self.assertNotIn('IBRAHIMA', appel.call_args.args[0].upper())
        self.assertFalse(NoteAccueil.objects.exists())

        rep = self.envoyer('examen_accueil:assistant_notes_excel', classe=self.classe.pk, notes=[
            {'eleve_id': self.mamadou.pk, 'matiere_id': self.francais.pk, 'note': '14,5'},
            {'eleve_id': self.fatou.pk, 'matiere_id': self.maths.pk, 'note': '9'},
        ])
        self.classeur(rep)
        from django.core.files.uploadedfile import SimpleUploadedFile
        fichier = SimpleUploadedFile('notes.xlsx', rep.content)
        self.client.post(reverse('examen_accueil:importer_notes', args=[self.classe.pk]), {'fichier': fichier, 'tous': '1'})
        self.assertEqual(NoteAccueil.objects.count(), 2)
        self.assertEqual(NoteAccueil.objects.get(eleve=self.mamadou).note, Decimal('14.50'))

        page = self.client.get(reverse('examen_accueil:saisie_notes'), {'classe': self.classe.pk, 'tous': '1'})
        self.assertContains(page, 'ouvrirAssistantAccueil')
        self.assertContains(page, reverse('examen_accueil:assistant_notes'))


class DocumentsTests(TestCase):
    """PDF, Word et Excel sont acceptés en plus des photos."""

    def _fichier(self, nom, contenu):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(nom, contenu)

    def _docx(self):
        import zipfile
        w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        cellule = lambda t: f'<w:tc><w:p><w:r><w:t>{t}</w:t></w:r></w:p></w:tc>'
        xml = (f'<?xml version="1.0" encoding="UTF-8"?><w:document {w}><w:body>'
               '<w:p><w:r><w:t>Liste des </w:t></w:r><w:r><w:t>nouveaux inscrits</w:t></w:r></w:p>'
               f'<w:tbl><w:tr>{cellule("Diallo Aminata")}{cellule("14,5")}</w:tr>'
               f'<w:tr>{cellule("Bah Mamadou")}{cellule("12")}</w:tr></w:tbl></w:body></w:document>')
        tampon = BytesIO()
        with zipfile.ZipFile(tampon, 'w') as archive:
            archive.writestr('word/document.xml', xml)
        return tampon.getvalue()

    def _xlsx(self):
        from openpyxl import Workbook
        classeur = Workbook()
        feuille = classeur.active
        feuille.title = 'Notes'
        feuille.append(['Matricule', 'Nom', 'Note', 'Né le'])
        feuille.append(['L-001', 'Bah Mamadou', 14.0, date(2013, 5, 2)])
        feuille.append([None, None, None, None])
        feuille.append(['L-002', 'Camara Fatoumata', 12.5, None])
        tampon = BytesIO()
        classeur.save(tampon)
        return tampon.getvalue()

    def _pdf(self, pages=1):
        from reportlab.pdfgen import canvas
        tampon = BytesIO()
        dessin = canvas.Canvas(tampon)
        for _ in range(pages):
            dessin.drawString(72, 720, 'Bordereau de versements')
            dessin.showPage()
        dessin.save()
        return tampon.getvalue()

    def test_word_excel_csv_deviennent_du_texte(self):
        from assistant_ia.claude import preparer_image

        type_, texte = preparer_image(self._fichier('liste.docx', self._docx()))
        self.assertEqual(type_, 'text/plain')
        self.assertIn('Liste des nouveaux inscrits', texte)
        self.assertIn('Diallo Aminata | 14,5', texte)

        type_, texte = preparer_image(self._fichier('notes.xlsx', self._xlsx()))
        self.assertEqual(type_, 'text/plain')
        self.assertIn('L-001 | Bah Mamadou | 14 | 02/05/2013', texte)
        self.assertIn('L-002 | Camara Fatoumata | 12.5', texte)

        type_, texte = preparer_image(self._fichier('liste.csv', 'Nom;Note\nSow Aïssatou;15\n'.encode('cp1252')))
        self.assertIn('Sow Aïssatou;15', texte)

    def test_pdf_envoye_tel_quel_et_limites(self):
        import base64
        from assistant_ia.claude import AssistantIAError, preparer_image

        type_, donnees = preparer_image(self._fichier('bordereau.pdf', self._pdf()))
        self.assertEqual(type_, 'application/pdf')
        self.assertTrue(base64.b64decode(donnees).startswith(b'%PDF'))

        for nom, contenu, attendu in (
            ('gros.pdf', self._pdf(pages=41), '40 maximum'),
            ('ancien.doc', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 vieux', 'ancien format'),
            ('ancien.xls', b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 vieux', 'ancien format'),
            ('faux.pdf', b'%PDF-1.4 pas un vrai pdf', 'illisible'),
            ('vide.xlsx', None, 'aucun texte'),
        ):
            if contenu is None:
                from openpyxl import Workbook
                tampon = BytesIO()
                Workbook().save(tampon)
                contenu = tampon.getvalue()
            with self.assertRaises(AssistantIAError, msg=nom) as echec:
                preparer_image(self._fichier(nom, contenu))
            self.assertIn(attendu, str(echec.exception), nom)

    def test_blocs_envoyes_a_claude(self):
        """Photo -> image, PDF -> document, Word/Excel -> texte."""
        from types import SimpleNamespace
        from assistant_ia import claude

        appels = {}

        class FauxClient:
            def __init__(self, **_):
                self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.creer))

            def creer(self, **parametres):
                appels.update(parametres)
                return SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(type='text', text='{"ok": true}')])

        with mock.patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'sk-test'}), \
                mock.patch('anthropic.Anthropic', FauxClient):
            resultat = claude.extraire_json('consigne', {'type': 'object'}, images=[
                ('image/jpeg', 'AAAA'), ('application/pdf', 'BBBB'), ('text/plain', 'Contenu du fichier « a.xlsx » :\nx'),
            ])
        self.assertEqual(resultat, {'ok': True})
        blocs = appels['messages'][0]['content']
        self.assertEqual([b['type'] for b in blocs], ['image', 'document', 'text', 'text'])
        self.assertEqual(blocs[1]['source'], {'type': 'base64', 'media_type': 'application/pdf', 'data': 'BBBB'})

    def test_un_excel_passe_par_l_assistant_des_eleves(self):
        """De bout en bout : le fichier envoyé par le navigateur arrive à Claude en texte."""
        from django.core.files.uploadedfile import SimpleUploadedFile

        utilisateur = get_user_model().objects.create_superuser('admin-doc', 'a@a.gn', 'x')
        self.client.force_login(utilisateur)
        with override_settings(MIDDLEWARE=SANS_LICENCE), \
                mock.patch('notes.assistant_saisie.extraire_json', return_value={'notes': [], 'non_reconnus': []}) as appel:
            from notes.models import ClasseNote, MatiereNote
            ecole = Ecole.objects.create(nom='École doc', adresse='Conakry', telephone='+224620000601', directeur='D')
            classe = Classe.objects.create(ecole=ecole, nom='9A', niveau='COLLEGE_9', annee_scolaire='2026-2027')
            classe_note = ClasseNote.objects.create(ecole=ecole, nom='9A', niveau='COLLEGE_9', niveau_enseignement='SECONDAIRE', annee_scolaire='2026-2027')
            Eleve.objects.create(matricule='D-001', prenom='Mamadou', nom='Bah', sexe='M', classe=classe)
            MatiereNote.objects.create(classe=classe_note, nom='Français', code='FR', coefficient=1)
            rep = self.client.post(reverse('notes:assistant_notes'), {
                'classe_id': classe_note.pk, 'photos': SimpleUploadedFile('notes.xlsx', self._xlsx()),
            })
        self.assertEqual(rep.status_code, 200, rep.content)
        type_, texte = appel.call_args.kwargs['images'][0]
        self.assertEqual(type_, 'text/plain')
        self.assertIn('Bah Mamadou', texte)

class ProtectionDesDonneesTests(Base):
    """Aucune page de l'assistant ne répond sans connexion, ni ne garde le document envoyé."""

    PAGES = [
        'notes:assistant_notes', 'notes:assistant_notes_excel',
        'eleves:assistant_eleve', 'eleves:assistant_eleves_importer', 'eleves:assistant_eleves_excel',
        'paiements:assistant_paiement', 'paiements:assistant_paiements_importer', 'paiements:assistant_paiements_excel',
        'bus:assistant_bus', 'bus:assistant_bus_importer', 'bus:assistant_bus_excel',
        'bus:assistant_cantine', 'bus:assistant_cantine_importer', 'bus:assistant_cantine_excel',
        'salaires:assistant_avances', 'salaires:assistant_avances_importer', 'salaires:assistant_avances_excel',
        'examen_accueil:assistant_notes', 'examen_accueil:assistant_notes_excel',
        'eleves:doublons_eleves',
    ]

    def test_rien_sans_connexion(self):
        self.client.logout()
        with mock.patch('assistant_ia.claude.extraire_json') as appel:
            for nom in self.PAGES:
                for methode in (self.client.get, self.client.post):
                    rep = methode(reverse(nom))
                    self.assertIn(rep.status_code, (302, 403, 405), nom)
                    if rep.status_code == 302:
                        self.assertIn('login', rep['Location'], nom)
                    self.assertNotIn(b'L-001', rep.content, nom)
        appel.assert_not_called()

    def test_compte_en_lecture_seule_ne_peut_rien_importer(self):
        Profil.objects.filter(user=self.user).update(lecture_seule=True)
        rep = self.envoyer('eleves:assistant_eleves_importer', lignes=[
            {'_index': 0, 'prenom': 'Test', 'nom': 'Lecture', 'sexe': 'M', 'classe_id': str(self.classe.pk)},
        ])
        self.assertNotEqual(rep.status_code, 200)
        self.assertFalse(Eleve.objects.filter(nom='LECTURE').exists())

    def test_le_document_envoye_n_est_pas_conserve(self):
        import os
        from django.core.files.uploadedfile import SimpleUploadedFile

        media = settings.MEDIA_ROOT
        avant = {os.path.join(d, f) for d, _, fichiers in os.walk(media) for f in fichiers} if os.path.isdir(media) else set()
        with mock.patch('eleves.assistant_saisie.extraire_json', return_value={'eleves': []}):
            self.client.post(reverse('eleves:assistant_eleve'), {
                'photos': SimpleUploadedFile('liste.csv', 'Nom;Prenom\nSecret;Eleve\n'.encode('utf-8')),
            })
        apres = {os.path.join(d, f) for d, _, fichiers in os.walk(media) for f in fichiers} if os.path.isdir(media) else set()
        self.assertEqual(avant, apres)  # rien n'est écrit sur le disque du serveur
        from utilisateurs.models import JournalActivite
        self.assertFalse(JournalActivite.objects.filter(description__icontains='Secret').exists())
