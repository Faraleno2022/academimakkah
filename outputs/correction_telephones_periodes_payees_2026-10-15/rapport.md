# Deuxième téléphone et périodes effectivement couvertes

Projet : GS_hadja_kanfing_dian--main. Date : 15 octobre 2026.

## Liste des élèves

Le crayon ouvre deux champs. Le premier numéro reste obligatoire ; le deuxième est facultatif et peut être effacé indépendamment. Les numéros locaux de huit ou neuf chiffres et les numéros avec indicatif sont acceptés, avec espaces ou tirets, puis normalisés en `+224…`.

Les deux numéros sont validés avant toute écriture et enregistrés ensemble. Après un enregistrement, les lignes visibles des enfants ayant le même responsable sont actualisées. Annuler puis rouvrir l'édition restaure les valeurs enregistrées. La recherche et la pagination dynamiques conservent les événements d'édition.

Le deuxième numéro appartient au responsable principal ; aucun responsable fictif n'est créé. La fiche de modification permet également de le modifier. Les formulaires existants et anciens messages de synchronisation qui omettent ce champ conservent sa valeur. Le contrôle d'accès est limité à l'école de l'utilisateur, y compris pour son administrateur ; le superadministrateur conserve son accès habituel.

Migration : `eleves.0023_responsable_telephone_secondaire`.

## Bus et cantine

La périodicité affichée dans les listes, PDF et Excel comprend désormais « 3 mois sur 9 mois », « 6 mois sur 9 mois » ou « 9 mois sur 9 mois », ainsi que le type de paiement d'origine. Les montants payés, les dates et le reste à payer restent visibles.

Pour le bus, le calcul des tranches avec la grille et les remises est conservé. Les reçus successifs prolongent le cycle depuis le premier paiement ; chaque reçu conserve la couverture obtenue à sa date.

Pour la cantine, selon la confirmation de l'utilisateur, le tarif annuel net est réparti en trois tranches égales, arrondies au franc ; la dernière absorbe l'éventuel écart d'arrondi. Un tarif couvrant deux trimestres est réparti en deux tranches. Les autres périodicités restent journalières, hebdomadaires ou mensuelles.

Exemple : tarif annuel de 900 000 GNF, remise de 10 %, soit 810 000 GNF nets.

| Payé | Période couverte | Début | Fin incluse | Reste à payer |
| --- | --- | --- | --- | --- |
| 270 000 GNF | 3 mois sur 9 mois | 15/10/2026 | 14/01/2027 | 540 000 GNF |
| 540 000 GNF | 6 mois sur 9 mois | 15/10/2026 | 14/04/2027 | 270 000 GNF |
| 810 000 GNF | 9 mois sur 9 mois | 15/10/2026 | 14/07/2027 | 0 GNF |

Le premier acompte ouvre le premier trimestre, comme pour le bus. Le deuxième puis le troisième trimestre nécessitent le règlement de leur seuil cumulé. Sans versement et avec un tarif positif, la couverture est nulle ; une remise intégrale conserve la période gratuite. Sans tarif renseigné, le montant saisi reste le montant encaissé et aucune dette supplémentaire n'est inventée.

Le formulaire, le modèle, les listes, les reçus privés/publics, l'historique, les exports et les aperçus utilisent la couverture calculée. Le changement du montant ou de la remise recalcule la fin sans déplacer le début de l'abonnement cantine.

Migration : `bus.0014_cantine_periode_payee`, après `bus.0013_validite_tranches`. Elle régularise les dates existantes par lots sans modifier les montants, les tarifs ni les remises. Son recalcul est idempotent.

## Validation et livraison

Les résultats détaillés sont enregistrés dans `verification.json` dans ce dossier. Les tests utilisent une base et des fichiers temporaires ; le réseau externe est désactivé. Le parcours navigateur est réalisé dans Edge avec un serveur local isolé.

Ces ajouts sont présents dans les sources locales. L'installateur 1.3.20 existant a été compilé avant ces ajouts et ne les contient pas. Cette correction n'a pas produit de nouvel installateur, de commit ou de publication GitHub. Le serveur et les postes devront appliquer les migrations lors de leur mise à jour.
