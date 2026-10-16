# Validité des abonnements bus et cantine — 15 octobre 2026

## Règle confirmée

Les durées sont calculées depuis le début de l'abonnement, avec une date de fin incluse.

| Couverture | Durée | Début | Fin incluse |
| --- | --- | --- | --- |
| Un trimestre | 3 mois | 15/10/2026 | 14/01/2027 |
| Deux trimestres | 6 mois | 15/10/2026 | 14/04/2027 |
| Trois trimestres | 9 mois | 15/10/2026 | 14/07/2027 |

Pour le bus, un premier versement le 15/10/2026 puis une deuxième tranche le 20/12/2026 conservent le début au 15/10/2026 et portent la fin au 14/04/2027. La date de chaque paiement reste enregistrée séparément, pour les reçus et rapports journaliers.

## Corrections

- Bus : calcul selon les montants des tranches effectivement couverts, remises déduites. Un choix « Annuel » payé à hauteur de la première tranche donne trois mois. Un complément de première tranche ne donne pas automatiquement un trimestre supplémentaire. Un premier acompte ouvre le premier trimestre ; les tranches suivantes doivent être soldées pour prolonger la durée.
- Les reçus suivants prolongent le même cycle. Correction, suppression ou réception hors ordre d'un versement : les périodes suivantes sont recalculées. L'année et la grille isolent les cycles.
- L'ordre des reçus du même jour utilise leur numéro puis leur UUID de synchronisation, pour être identique sur les postes. L'aperçu d'une modification conserve cette identité.
- Cantine : trimestriel = 3 mois, deux trimestres = 6 mois, trois trimestres et annuel scolaire = 9 mois. Le formulaire, le modèle et les renouvellements utilisent ces durées. Les montants, tarifs, remises et dettes ne sont pas modifiés par cette correction des dates.
- Début et fin affichés dans la liste bus, l'historique et ses exports, les exports de la liste, les reçus privés et publics et les aperçus WhatsApp. Les relances utilisent également le début de validité.
- Formulaire bus : une réponse de calcul devenue ancienne ne peut plus écraser le montant saisi ; l'aperçu suit la date et le montant.

## Migration

`bus.0013_validite_tranches` ajoute le début de validité bus et régularise les dates existantes par lots, sans changer les paiements ni les tarifs. Une seconde exécution du recalcul est sans effet supplémentaire.

La migration a été appliquée à une base temporaire. La base du client n'a pas été utilisée par les vérifications. Les installations devront appliquer cette migration lors de leur mise à jour.

## Vérifications

| Vérification | Résultat | Journal |
| --- | --- | --- |
| Suites bus, paiements, rapports, synchronisation et nouvelle année, avant les derniers ajustements d'affichage | 476 tests réussis, 221,528 s | `tmp/tests_validite_tranches_finances_sync_20261015.log` |
| Vérification ciblée après correction du formulaire, de l'ordre des reçus et des aperçus WhatsApp | 64 tests réussis, 45,600 s | `tmp/tests_validite_tranches_final_20261015.log` |
| Dernière vérification des périodes et des affichages, incluant le reçu public | 18 tests réussis, 3,780 s | `tmp/tests_validite_tranches_affichages_20261015.log` |
| Navigateur Edge réel, isolé : nouvelle année, filtres, remises, aperçus bus et cantine 3/6/9 mois | Réussi ; aucune erreur JavaScript | `tmp/navigateur_validite_tranches_20261015.log` |
| Cohérence des modèles et application des migrations sur une base temporaire | Réussi | `tmp/schema_validite_tranches_20261015.log` |
| `git diff --check` | Réussi | Vérification locale |

Les suites se recouvrent ; leurs nombres ne s'additionnent pas. Le test de synchronisation simule trois postes en local, sans réseau externe ; il ne mesure pas les délais de production.

## État de livraison

Corrections présentes sur `codex/validite-tranches-bus-cantine-2026-10-15`, base `6ba4bd4`. Aucun commit, push ou nouvel installateur n'a été produit pour cette correction. Le dossier utilisateur `video_reconstruction/` a été conservé.
