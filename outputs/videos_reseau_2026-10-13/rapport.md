# Lecture des vidéos publiques sur connexion lente

Date : 13 octobre 2026.

## Résultat

Les deux vidéos de la page d'accueil et de `/demo/` disposent de versions
360p, 480p et 720p. Le lecteur adapte la qualité au débit et propose les choix
Automatique, Économie de données et Haute qualité.

| Vidéo | Original | MP4 économique | Réduction | Débit moyen HLS 360p |
| --- | ---: | ---: | ---: | ---: |
| Présentation | 44,34 Mo | 3,96 Mo | 91,1 % | 264 kbit/s |
| Gestion des notes | 45,43 Mo | 4,21 Mo | 90,7 % | 253 kbit/s |

Les Mo sont décimaux. Les durées et les empreintes des originaux sont préservées.
Le débit de la version économique inclut le son.

## Fonctionnement

- HLS VOD avec segments indépendants d'environ deux secondes, H.264/AAC.
- Démarrage HLS.js en 360p ; réserve cible de 30 secondes, limitée à 8 Mo.
  Le lecteur HLS natif utilise la gestion de réserve du navigateur.
- Aucun transfert vidéo avant le clic. La bibliothèque locale HLS.js est
  chargée uniquement si le navigateur ne dispose pas de lecture HLS native.
- Respect du réglage navigateur d'économie de données.
- Une seule vidéo active ; les téléchargements HLS.js s'arrêtent à la pause.
- Reprise des erreurs temporaires et secours MP4 léger si HLS échoue.
  La position est conservée lors du passage au secours.
- Retour arrière dans le navigateur : le lecteur conservé en cache reste
  utilisable et reprend sur une action de l'utilisateur.
- Lecture MP4 économique disponible même sans JavaScript.
- Politique CSP : médias locaux et `blob:` pour MediaSource, connexions locales.
- Types MIME HLS explicites dans WhiteNoise ; les segments déjà compressés ne
  sont pas recompressés pendant `collectstatic`.

Les petits segments peuvent être servis par le stockage statique existant,
même si l'hébergement ne gère pas les requêtes Range sur les gros MP4.
Une connexion durablement inférieure au débit vidéo ou une coupure plus longue
que la réserve peut encore interrompre la lecture.

## Vérifications effectuées

- 15 tests JavaScript passent (`node --test scripts/test_public_videos.cjs`).
- 61 tests Django de sécurité passent :
  `python scripts/run_project_tests.py ecole_moderne.test_security_middleware ecole_moderne.test_vulnerabilites`.
- Décodage FFmpeg complet des six versions HLS, sans erreur.
- Collecte des fichiers avec les paramètres de production dans un dossier
  temporaire : pages `/`, `/index/`, `/demo/` en HTTP 200, playlists et segments
  présents, durées cohérentes, types MIME et réponses partielles validés avec
  WhiteNoise. Base de test isolée ; aucune modification de la base client.
- Navigateur Edge isolé : lecture HLS.js réelle à 524 288 bit/s, latence 100 ms,
  absence de téléchargement vidéo avant clic, passage 720p, seconde vidéo,
  affichage mobile 390 × 844 sans débordement, aucune erreur JavaScript/HTTP.
- Après constitution d'une réserve de plus de huit secondes, une coupure
  simulée de quatre secondes a été absorbée et la lecture a continué.
- Manifeste rendu inaccessible : le secours MP4 léger a été lu dans le navigateur.
- Contrôle visuel de la capture mobile jointe.

Le premier essai limité à 512 kbit/s a atteint deux secondes de lecture
8,834 secondes après le clic, téléchargement initial de la bibliothèque inclus.
Cette mesure n'est pas le délai d'apparition de la première image.
Ces essais sont locaux avec réseau simulé ; le résultat sur l'hébergement
devra être confirmé après déploiement.

## Déploiement PythonAnywhere

Dans le dépôt du serveur :

```bash
git pull
python manage.py collectstatic --noinput
```

Puis utiliser **Reload** dans l'onglet **Web** et rafraîchir la page avec
Ctrl+F5. Cette modification ne nécessite aucune migration ni installation
de FFmpeg sur le serveur : les fichiers encodés sont inclus dans le dépôt.

## Préparation et dépendance

Le script `scripts/prepare_public_videos.py` permet de régénérer les versions
avec FFmpeg/FFprobe sur la machine de préparation. Les paramètres et métriques
de chaque vidéo figurent dans son fichier `encodage.json`.

HLS.js 1.7.3 light est distribué localement avec sa licence Apache-2.0.
L'intégrité SHA-512 de l'archive NPM officielle a été vérifiée avant extraction.
La provenance et l'empreinte SHA-256 figurent dans `static/vendor/hls/README.md`.
