# Compilation MySchoolGN 1.3.20 — 15/10/2026

## Livraison locale

- Installateur : `Output/MySchoolGN_Setup_v1.3.20.exe` (454.7 Mio).
- Empreinte : `Output/MySchoolGN_Setup_v1.3.20.exe.sha256`.
- Application : `dist/MySchoolGN/MySchoolGN.exe`.
- Distribution précédente conservée : `dist/MySchoolGN_avant_recompilation_20261015`.

## Contenu

Construction depuis le dépôt local, base Git `6ba4bd4a17f6369a12db746d8a4ea13dabb0bf43`, incluant les modifications non commitées de validité bus/cantine et la version portée à 1.3.20.

Inclus : création de nouvelle année, séparation des finances par année, correction du cache des notes, validité bus/cantine de 3/6/9 mois, prolongation du cycle bus depuis son premier paiement, dates visibles dans les formulaires, listes, reçus et exports. Les optimisations de synchronisation, le transfert des images, les remises et les vidéos publiques adaptatives des versions précédentes sont conservés.

Migrations vérifiées dans le paquet : `bus.0012_abonnements_annee_scolaire`, `bus.0013_validite_tranches`, `synchronisation.0009_syncmediatransfer` et `eleves.0022_ecole_autoriser_remise_inscription`.

## Vérifications

- Construction PyInstaller et Inno Setup réussie.
- 38 modules extraits de l'exécutable et comparés au code source compilé avec le même niveau d'optimisation.
- 30 ressources et migrations comparées aux sources ; 399 fichiers des vidéos adaptatives vérifiés dans leurs emplacements de distribution.
- Version embarquée et `version.json` : 1.3.20.
- Manifeste d'intégrité valide et fichier de garde présent.
- Contrôle de démarrage `--check-updates --no-update` réussi en 2.39 s ; aucune base ouverte. Ce contrôle quitte avant le démarrage complet du serveur.
- Sept tests du mécanisme de mise à jour réussis après changement de version.
- Avant compilation : 476 tests sur les suites financières/synchronisation, puis 64 tests ciblés et 18 dernières vérifications d'affichage, ainsi qu'un parcours navigateur réussi. Ces suites se recouvrent et ne s'additionnent pas.
- Absence de base, licence, configuration personnelle et photos client dans la distribution.
- Empreintes des bases locales et fichiers privés inchangées après construction.

SHA-256 : `bf7ecff3715edfb4326dc66f189618207fb83b933a15a786ac682cdad3325a63`.

## Installation

L'application des postes applique les migrations au démarrage, après sa sauvegarde automatique. Le serveur doit également recevoir les corrections et appliquer les migrations avant rechargement de l'application web.

Cette étape livre un installateur local ; aucune installation sur les données du client ni publication de Release GitHub n'a été exécutée.
