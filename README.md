# Comptabilité et facturation françaises pour Django

Deux applications Django indépendantes, prêtes à brancher dans n'importe quel projet (boutique en ligne,
logiciel de gestion, SaaS) :

- **[django-accounting-fr](packages/django-accounting-fr)** (`accounting`) : comptabilité en partie double selon le plan
  comptable général — journaux et journaux de trésorerie, numérotation continue, empreinte chaînée et sceaux de
  clôture (inaltérabilité), lettrage et balance âgée, rapprochement bancaire (CSV, OFX, CAMT.053, CFONB 120), TVA
  (sur les débits ou les encaissements, autoliquidation, aide à la CA3, liquidation), immobilisations et amortissements,
  bilan, compte de résultat et comptes annuels, analytique, devises, FEC et son contrôle.
- **[django-facturation-fr](packages/django-facturation-fr)** (`invoicing`) : factures et avoirs numérotés aux mentions
  légales françaises, PDF **Factur-X** (EN 16931), facturation électronique (plateforme agréée, cycle de vie, factures
  reçues) et **e-reporting** au format officiel (flux 10 des spécifications externes de la DGFiP).

Chaque application s'utilise seule ; le projet hôte leur décrit ses ventes, encaissements et achats par une API Python
(voir le README de chacune).

## Installation

```bash
pip install "django-accounting-fr @ git+https://github.com/MatRai-hexa/django-compta-facturation-fr.git#subdirectory=packages/django-accounting-fr"
pip install "django-facturation-fr @ git+https://github.com/MatRai-hexa/django-compta-facturation-fr.git#subdirectory=packages/django-facturation-fr"
```

Python 3.11 et plus, Django 5.2. SQLite ou PostgreSQL.

## Développement

```bash
python -m venv .venv && . .venv/bin/activate          # Windows : .venv\Scripts\activate
pip install -e packages/django-accounting-fr -e packages/django-facturation-fr dj-database-url
python manage.py test accounting invoicing
DATABASE_URL=postgres://utilisateur:mdp@localhost:5432/test python manage.py test accounting invoicing
```

`testproject/` est un projet Django minimal qui installe les deux applications.

## Avertissement

Ces outils aident à tenir une comptabilité et à facturer conformément aux règles françaises, mais ne remplacent pas un
expert-comptable : déclarations (CA3, liasse fiscale) et comptes annuels sont à faire valider avant dépôt. Le FEC produit
est contrôlé par l'application ; il n'a pas été certifié par l'outil Test Compta Demat de l'administration.

## Licence

Copyright © 2026 MatRai-hexa.

Distribué sous **GNU Affero General Public License v3.0 ou ultérieure** ([LICENSE](LICENSE)) : utilisation, modification
et redistribution libres, à condition de publier sous la même licence toute version modifiée, y compris lorsqu'elle est
seulement utilisée à travers un réseau. Une **licence commerciale**, sans cette obligation, peut être accordée par
l'auteur : ouvrez une issue.
