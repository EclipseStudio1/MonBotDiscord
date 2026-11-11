# ECLIPSE RP — Bot Discord

Bot Discord Python basé sur `discord.py`, avec stockage SQLite.

## Fonctions incluses
- Tickets SESSION ECLIPSE RP et ASSISTANCE
- Prise en charge d'un ticket par un seul membre du staff
- Formulaire de candidature et envoi vers un salon configuré
- Modération : avertir, timeout, expulser, bannir, purger
- Logs : messages supprimés/modifiés, arrivées/départs, rôles, tickets et annonces
- Statistiques du serveur
- Suivi des invitations et classement Top 10
- Annonces
- Commandes personnalisées réservées au staff
- Vérification des membres par bouton
- Configuration des salons et rôles par commandes slash

## Installation sur Render
1. Ajoute les fichiers du projet dans ton dépôt GitHub.
2. Sur Render, garde ton service Web Service existant.
3. Dans **Environment**, ajoute `DISCORD_TOKEN` avec le token du bot.
4. Mets la commande de démarrage à `python bot.py`.
5. Mets l'environnement Python et laisse Render installer `requirements.txt`.
6. Dans le portail Discord Developer, active les intents **Server Members Intent**, **Message Content Intent** et **Presence Intent** si demandés (Presence n'est pas utilisé ici).
7. Vérifie que le bot a les permissions nécessaires : gérer les salons, gérer les rôles, voir les salons, envoyer des messages, lire l'historique, gérer les messages, modérer les membres, expulser et bannir.

## Première configuration dans Discord
Avec un compte administrateur :
- `/configurer_role` → Staff
- `/configurer_role` → Membre vérifié
- `/configurer_role` → Non vérifié (facultatif)
- `/configurer_categorie_tickets` → Catégorie des tickets
- `/configurer_salon` → Logs
- `/configurer_salon` → Candidatures reçues
- `/configurer_salon` → Bienvenue
- `/publier_tickets`
- `/publier_candidatures`
- `/publier_verification`

## Important
- Ne publie jamais ton token Discord dans GitHub, un message ou une capture.
- Active les intents privilégiés dans le portail développeur Discord.
- Pour le suivi des invitations, le bot doit avoir la permission **Gérer le serveur**. Les invitations par lien personnalisé/vanity et certaines suppressions d'invites peuvent empêcher une attribution fiable.
- SQLite convient à un petit/moyen serveur. Pour une disponibilité durable sur Render, configure un disque persistant et place la base sur ce disque, sinon les données locales peuvent être perdues lors de certains redéploiements.
- Les commandes de vérification donnent le rôle configuré, mais il faut aussi régler les permissions des salons pour que le rôle non vérifié ne voie que le salon de vérification.
- Ce projet est un point de départ complet à tester sur un serveur privé avant de l'utiliser en production.
