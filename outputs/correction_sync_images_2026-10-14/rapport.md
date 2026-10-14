# Correction de la synchronisation des images

Date : 14 octobre 2026.

## Cause confirmée

`serialize_instance()` échangeait uniquement `FieldFile.name` pour les champs
fichiers. `build_change_instance()` enregistrait ensuite ce chemin dans la base
du poste, sans copier les octets dans son dossier média. Le nom de l'école
pouvait donc être à jour tandis que son logo pointait vers un fichier absent.
Aucun transport binaire ni rattrapage des anciens fichiers n'existait.

## Correction

- API privée `/api/v1/sync/media/` authentifiée avec les identifiants du poste,
  limitée à l'objet et à l'école autorisés. Les chemins arbitraires sont refusés.
- Transfert des logos, photos d'écoles, portraits d'élèves et autres champs
  fichiers des modèles déjà synchronisés.
- Troisième tâche de synchronisation, distincte de l'envoi et de la réception
  des données métier. Les octets ne sont pas ajoutés aux lots JSON.
- File durable `SyncMediaTransfer`, créée par la migration
  `0009_syncmediatransfer`. Acquittement des données sortantes et mise en file
  des fichiers dans la même transaction.
- Contrôle SHA-256, validation des images, limite de 25 Mio par fichier,
  transfert par blocs de 64 Kio et stockage temporaire au-delà de 1 Mio.
- Écriture atomique : un fichier incomplet ne remplace pas une copie existante.
  Après une coupure, le fichier entier est réessayé avec un délai croissant,
  plafonné à 300 secondes. La file survit au redémarrage du poste.
- Protection contre un transfert devenu ancien pendant une nouvelle saisie.
- Noms des fichiers envoyés séparés par école et dérivés de leur contenu.
  Un envoi répété après perte de réponse reste idempotent.
- Rattrapage des anciens chemins, par pages de 50 objets : récupération des
  fichiers absents en local, vérification des fichiers locaux pour compléter
  ceux jamais envoyés au serveur. Les curseurs métier sont conservés.
- Priorité aux images de l'école devant le rattrapage des portraits.
- Réutilisation locale des octets déjà envoyés ; pas de téléchargement
  systématique d'une image présente à chaque changement métier.
- Changement de la révision d'affichage une fois le fichier disponible,
  permettant l'actualisation des pages ouvertes selon les protections de saisie
  existantes. La restauration d'un fichier manquant actualise aussi le serveur.
- Compteurs des fichiers en attente et à réessayer sur la page de connexion.
  La commande `sync_offline --etat` donne aussi les erreurs des fichiers.

## Vérification

Toutes les vérifications ont utilisé des bases et dossiers temporaires,
avec le réseau externe désactivé.

| Vérification | Résultat |
| --- | --- |
| Suite complète du projet | 885 tests passent, 309,403 s |
| Validation ciblée après les derniers ajustements | 60 tests passent, 26,079 s |
| Cohérence des migrations | `makemigrations --check` : aucun changement manquant |
| Application des migrations sur SQLite temporaire | `0009_syncmediatransfer` appliquée avec succès |
| Réception de 200 élèves | 7 requêtes SQL |
| Instantané de 200 élèves | 5 requêtes SQL |

La suite complète a précédé les derniers ajustements de priorité, d'affichage
et de restauration des octets. La validation ciblée a ensuite couvert ces
ajustements et tous les tests du nouveau transfert.

L'essai d'intégration utilise un serveur et deux postes ayant chacun une
base SQLite et un dossier média distincts. Seul le transport HTTP est remplacé
par un adaptateur vers les vraies vues Django. Il vérifie :

1. Logo et photo du bâtiment modifiés sur le serveur, récupérés par les deux postes.
2. Fichier historiquement absent du serveur, restauré depuis un poste sans
   réinitialiser sa synchronisation, puis récupéré par l'autre poste.
3. Nouveau logo et portrait créés sur le poste A, transférés au serveur puis au poste B.
4. Révision d'affichage modifiée après réception et absence de boucle de renvoi.

Les tests couvrent également les jetons, les appareils désactivés, les accès
entre écoles, les chemins interdits, les fichiers invalides/incomplets, les
réessais, la priorité du logo, les transferts obsolètes et le mode `--pull-only`.

Commandes de vérification :

```text
python scripts/run_project_tests.py
python scripts/run_project_tests.py synchronisation.test_media synchronisation.test_liaison synchronisation.test_realtime ecole_moderne.test_security_middleware
python scripts/run_project_tests.py --schema
```

## Activation

Cette correction est préparée dans le dépôt local. Elle doit être livrée sur
le serveur et dans l'application des postes pour activer le transfert binaire.
Un ancien exécutable continue à n'échanger que les noms des fichiers.

Après livraison du code, appliquer `python manage.py migrate` sur le serveur,
recharger l'application web, puis installer la version des postes contenant
ces modules et cette migration. Le rattrapage des images démarre automatiquement.
Les fichiers originaux présents sur une autre machine doivent rester disponibles
pour pouvoir être récupérés.

L'essai n'a pas déployé de changement sur l'hébergement de production et
n'a pas utilisé la base du client. Les fichiers supérieurs à 25 Mio restent
signalés en attente avec leur erreur ; la base métier continue à se synchroniser.

Le transfert utilise le stockage local `FileSystemStorage`, configuré par le
projet. Les limites de l'hébergeur sur la taille des requêtes restent applicables.

Références techniques : transfert binaire via
[FileResponse de Django](https://docs.djangoproject.com/en/5.2/ref/request-response/#fileresponse-objects)
et validation des images avec les protections
[Pillow](https://pillow.readthedocs.io/en/stable/handbook/security.html).
