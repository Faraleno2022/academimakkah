# Compilation MySchoolGN 1.3.18 — 12/10/2026

## Fichiers livrés

- Installateur : `Output/MySchoolGN_Setup_v1.3.18.exe` (74.6 Mio).
- Empreinte : `Output/MySchoolGN_Setup_v1.3.18.exe.sha256`.
- Application : `dist/MySchoolGN/MySchoolGN.exe`.
- Ancienne compilation conservée : `dist/MySchoolGN_avant_recompilation_20261012`.

## Contenu

Code fusionné sur `main` au commit `82608e953275ca23b6a297d0ded643cfc2e9539e`, avec la version portée à 1.3.18 dans `app_version.py` et `installer_myschool.iss`.

La distribution inclut la synchronisation rapide et l'actualisation des postes, ainsi que les remises d'inscription/réinscription par école. La migration `eleves.0022_ecole_autoriser_remise_inscription`, les formulaires et le JavaScript de synchronisation embarqués correspondent aux sources.

## Vérifications

- Construction PyInstaller et Inno Setup réussies.
- Version compilée et version de distribution : 1.3.18.
- 20 modules applicatifs vérifiés dans l'archive de l'exécutable.
- Manifeste d'intégrité valide et fichier de garde présents.
- Contrôle de démarrage réussi en 2.81 s avec `--check-updates --no-update`, avant ouverture de la base et sans accès réseau.
- Sept tests du mécanisme de mise à jour réussis après changement de version.
- Les 326 tests paiements/élèves étaient validés avant compilation ; aucun calcul métier n'a été modifié pour cette recompilation.
- Aucune base, licence, configuration personnelle ni photo du client dans la distribution.
- Empreintes de la base locale et de ses fichiers SQLite annexes inchangées avant/après compilation.

SHA-256 de l'installateur : `4a3ca45c5e4f3bfe676e3862b2d98c070417e02dba70e20d14aca4d16acb13f4`.

La compilation a été effectuée dans une copie du code isolée des données client. L'installateur est livré localement ; il n'a pas été exécuté sur une installation client pendant cette vérification. Les migrations sont incluses pour le démarrage de l'application mise à jour.
