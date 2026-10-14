# Compilation MySchoolGN 1.3.19 — 14/10/2026

## Livraison locale

- Installateur : `Output/MySchoolGN_Setup_v1.3.19.exe` (454.6 Mio).
- Empreinte : `Output/MySchoolGN_Setup_v1.3.19.exe.sha256`.
- Application : `dist/MySchoolGN/MySchoolGN.exe`.
- Compilation précédente conservée : `dist/MySchoolGN_avant_recompilation_20261014`.

## Sources et contenu

Code fusionné sur `main` au commit `81a62d894e26158253bb4a24f36a2aed57b8b3f5`, avec
la version portée à 1.3.19 dans `app_version.py` et `installer_myschool.iss`.

Cette version inclut le transfert des logos, photos et fichiers entre serveur
et postes, leur rattrapage et leurs réessais dans une tâche séparée, ainsi que
les optimisations de synchronisation et de performance, les remises
d'inscription/réinscription et les vidéos publiques avec lecture adaptative.

Les migrations `synchronisation.0008_syncchange_reception_indexes`,
`synchronisation.0009_syncmediatransfer` et
`eleves.0022_ecole_autoriser_remise_inscription` sont incluses.

## Vérifications

- PyInstaller et Inno Setup : construction réussie.
- 24 modules extraits de l'exécutable et comparés au code source compilé avec
  le même niveau d'optimisation, dont tous les nouveaux modules de transfert.
- Version embarquée et version de distribution : 1.3.19.
- Migrations, interfaces et scripts comparés aux sources.
- Les 399 fichiers des vidéos adaptatives sont vérifiés dans les emplacements
  de distribution, avec la bibliothèque HLS.js et les vidéos originales.
- Manifeste d'intégrité valide et fichier de garde présents.
- Contrôle de démarrage avec `--check-updates --no-update` réussi en
  1.66 s, sans ouverture de base.
- Les sept tests du mécanisme de mise à jour passent après changement de version.
- La correction source avait été validée par 885 tests complets, puis 60 tests
  ciblés après les derniers ajustements. La migration était validée sur une
  base temporaire. Aucun calcul métier n'a été modifié pour cette compilation.
- Aucune base, licence, configuration personnelle ni image client dans le paquet.
- Empreintes de la base locale, de ses annexes et des fichiers privés inchangées.

SHA-256 de l'installateur : `7e72da15011bc441f04004ab0ae86d5d88dd6a823de597a49bf8a84d8cf9351d`.

## Activation du transfert d'images

Le serveur doit également recevoir le code fusionné et la migration
`0009_syncmediatransfer` (`python manage.py migrate`, puis rechargement web).
L'application des postes applique ses migrations au démarrage. Le transfert
des images nécessite la mise à jour du serveur et des postes.

L'installateur est livré localement. Le contrôle n'a pas exécuté d'installation
sur les données client, et aucune Release GitHub n'a été publiée dans cette étape.
