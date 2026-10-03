# Contribuer

Les contributions sont les bienvenues : signalement d'anomalie, cas de gestion comptable ou fiscal non couvert,
correctif. Ouvrez d'abord une issue pour discuter d'une évolution importante.

- Une modification s'accompagne de ses tests ; `python manage.py test accounting_fr facturation_fr` doit passer sous SQLite et
  PostgreSQL (la CI le vérifie).
- Le code, les messages et la documentation sont en français.
- Les migrations sont générées (`python manage.py makemigrations`) et ne modifient pas les migrations déjà publiées.

En proposant une contribution, vous acceptez qu'elle soit distribuée sous la licence AGPL-3.0-or-later du projet et
que l'auteur du projet puisse aussi la distribuer sous une licence commerciale (double licence).
