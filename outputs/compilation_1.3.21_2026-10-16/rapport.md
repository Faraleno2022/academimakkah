# Compilation MySchoolGN 1.3.21 — 16/10/2026

## Livraison locale

- Installateur : `Output/MySchoolGN_Setup_v1.3.21.exe` (453.1 Mio).
- Empreinte : `Output/MySchoolGN_Setup_v1.3.21.exe.sha256`.
- Application : `dist/MySchoolGN/MySchoolGN.exe`.
- Distribution précédente conservée : `dist/MySchoolGN_avant_recompilation_20261016`.

## Contenu

Construction depuis `main`, commit `036fce0` (« Prepare MySchoolGN Desktop 1.3.21 »), sans modification locale non commitée.

La 1.3.21 remplace la 1.3.20 compilée le 15/10, qui ne contenait pas ces corrections. Le numéro change pour que les postes ayant déjà installé la 1.3.20 reçoivent la mise à jour.

Nouveautés par rapport à la 1.3.20 :

- Bus et cantine : mois couverts (3, 6 ou 9) selon le montant payé après remise, tarif annuel de la cantine réparti en trois tranches égales, avec dates et reste à payer dans les listes, PDF et Excel.
- Élèves : deuxième téléphone facultatif du responsable.
- Enseignants : une même classe peut être affectée plusieurs fois pour des matières différentes (ex. Biologie et Chimie en 7ème). Le message « Corrigez les données en double dans classe et date_debut… » est remplacé par un message clair.

Migrations vérifiées dans le paquet, identiques aux sources : `bus.0013_validite_tranches`, `bus.0014_cantine_periode_payee`, `eleves.0023_responsable_telephone_secondaire`, `salaires.0014_affectation_unicite_par_matiere`.

## Vérifications

- Avant compilation : 323 tests des modules bus, élèves et salaires réussis.
- Construction PyInstaller et Inno Setup réussie.
- Version embarquée et `version.json` : 1.3.21.
- Contrôle de démarrage `--check-updates --no-update` réussi en 3.4 s (code 0), intégrité vérifiée, aucune base créée ; ses fichiers temporaires (`.secret_key`, `myschool.log`) ont été supprimés avant Inno Setup.
- Sept tests du mécanisme de mise à jour réussis.
- Aucune base, licence, configuration ou photo utilisateur dans la distribution.
- Empreintes de la base locale, de `.env` et des licences inchangées après construction.
- `gobject-2.0-0.dll` n'est plus présent (il venait d'un dossier absent du PATH de cette construction). WeasyPrint charge `libgobject-2.0-0.dll` en premier ; ce fichier est présent, comme dans les versions précédentes.

SHA-256 : `35c9bb9286c865bb3f5d2205946781bc5138efc62be5978838428ce498ec4c0b`.

## Installation

L'application des postes applique les migrations au démarrage, après sa sauvegarde automatique. Le serveur en ligne doit aussi recevoir ces corrections et appliquer les migrations.

Aucune installation sur les données du client ni publication de Release GitHub n'a été exécutée.
