# Journal des versions

## Non publié

- E-reporting 10.1 : les factures aux professionnels établis hors de France transmettent leurs remises (TG-20)
  et leurs lignes (TG-24 : quantité et unité, prix unitaire net HT, désignation), obligatoires à partir du 01/09/2027.

## 0.2.0

- Modules renommés `accounting_fr` et `facturation_fr` (au lieu de `accounting` et `invoicing`, déjà pris par d'autres
  paquets sur PyPI) ; les libellés d'application restent `accounting` et `invoicing` : tables, migrations, droits et
  espaces d'URL inchangés, aucune migration à rejouer. Mettre à jour `INSTALLED_APPS`, `include()` et les imports.
- Première publication sur PyPI.

## 0.1.0

- Première version publique.
