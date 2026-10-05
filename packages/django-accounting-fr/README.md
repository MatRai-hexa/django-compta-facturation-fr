# django-accounting-fr

Application Django de comptabilité en partie double, conforme aux usages français :
plan comptable général (PCG), journaux VT/BQ/AC/OD/AN, numérotation chronologique
à la validation, écritures validées intangibles (correction par contre-passation),
TVA collectée par taux, lettrage des comptes de tiers, rapprochement bancaire, clôture d'exercice et à-nouveaux, export FEC
(art. A47 A-1 du LPF), journal d'audit.

Elle ne dépend d'aucun autre module : le projet hôte lui décrit ses pièces
(ventes, encaissements, avoirs, remboursements) via une API Python.

> Ce module produit une comptabilité tenue selon les règles du PCG et un FEC
> au format attendu ; il ne remplace pas l'expert-comptable ni un logiciel
> certifié lorsque la loi l'impose (caisse, facturation électronique).

## Installation

```bash
pip install django-accounting-fr      # ou depuis un dépôt Git / un index privé
```

```python
INSTALLED_APPS = [..., "accounting_fr"]  # libellé d'application : « accounting »
urlpatterns = [..., path("comptabilite/", include("accounting_fr.urls"))]
```

```bash
python manage.py migrate        # installe le plan comptable, les journaux et les taux de TVA
python manage.py init_accounts  # (ré)installe le plan par défaut, sans doublon
```

Les pages sont réservées aux utilisateurs ayant les permissions `accounting.*`
(`view_transaction`, `add_transaction`, `change_fiscalperiod`…).

## API

```python
from accounting_fr import api

customer = api.Party("C042", "Dupont SARL")
api.post_sale("invoice:F-001", day, "F-001", "Facture F-001", customer,
              [api.SaleLine(Decimal("120.00"), Decimal("0.20")),
               api.SaleLine(Decimal("6.00"), Decimal("0.20"), kind="shipping")],
              prices_include_tax=True, document=("invoice", "F-001"))
api.post_payment("invoice:F-001:payment", day, "VIR-1", "Règlement F-001",
                 Decimal("126.00"), "transfer", customer, document=("invoice", "F-001"))
api.post_credit_note("invoice:F-001:refund", "invoice:F-001", day, "AV-1", "Avoir F-001")
api.post_refund("invoice:F-001:refund_payment", day, "AV-1", "Remboursement", Decimal("126.00"), "transfer", customer)
api.post_fee("invoice:F-001:fee", day, "ch_123", "Frais Stripe F-001", Decimal("2.14"), "stripe")   # 627 / 467
api.post_transfer("stripe:payout:po_1", day, "po_1", "Virement Stripe", Decimal("123.86"), "stripe")  # ST 580/467, BQ 512/580

api.is_posted("invoice:F-001")          # True
api.entries_for("invoice", "F-001")     # écritures liées à la pièce
```

- Chaque appel porte une **clé unique** : le rejouer ne crée jamais de doublon.
- `kind` d'une ligne : `goods` et `services` (compte de ventes paramétré, 707 par défaut) ou `shipping` (7085).
- Le journal d'encaissement dépend du moyen (`method`) : table *Journaux des moyens de paiement*,
  à défaut le journal de la banque ; le compte est le compte de trésorerie du journal.
- Une erreur de paramétrage (taux de TVA sans compte, exercice clôturé…) lève
  `api.AccountingError`.

Pour les écritures libres : `accounting_fr.posting.post_entry(journal_code, day, description, lines, ...)`.
`posting.with_treasury_counterpart(journal, lines)` complète une saisie dans un journal de banque
par sa contrepartie sur le compte du journal.

## Intégration au projet hôte

Tout est facultatif ; sans réglage, l'application fonctionne seule avec son propre gabarit.

```python
ACCOUNTING = {
    "BASE_TEMPLATE": "monprojet/base.html",               # doit définir un bloc « content »
    "DOCUMENT_URL": "monprojet.accounting.document_url",  # f(type, id) -> URL de la pièce ou None
    "COMPANY": "monprojet.accounting.company",            # f() -> {"name": ..., "siren": ...}
    "DASHBOARD_PANELS": ["monprojet.accounting.panels"],  # f(request) -> [{"label", "value", "url", "alert"}]
}
```

La raison sociale et le SIREN saisis dans les paramètres comptables priment sur `COMPANY`.

## Journaux

Édition de chaque journal et du livre-journal (`/journaux/<code>/`, `/journaux/livre-journal/`),
journal centralisateur (`/journaux/`), création de journaux.

Chaque journal de banque a un **compte de trésorerie** (`Journal.account`) : BQ 512000, ST (Stripe)
467100, PP (PayPal) 467200, CA (caisse) 530000 par défaut. Ce compte est la contrepartie de toutes
les écritures du journal et ne se mouvemente que dans ce journal (à-nouveaux et contre-passations
exceptés) ; un virement entre deux trésoreries passe par le compte de virements internes (580).
`PaymentAccount.journal` dirige les encaissements, frais et virements d'un moyen de paiement vers
son journal. Un journal mixte hérité d'une version précédente est signalé au tableau de bord.

## TVA et bilan

`/tva/` : récapitulatif de la période (bases et TVA collectée par taux, TVA déductible) et **liquidation** :
`vat.settle(début, fin)` passe et valide, au dernier jour de la période, l'écriture de déclaration (journal OD,
type `vat`) qui solde les 4457 et 4456 (écritures validées jusqu'à cette date), impute le crédit de TVA reporté
(44567) et porte le solde en TVA à décaisser (44551) ou en nouveau crédit à reporter. Elle est refusée s'il reste
des brouillons touchant la TVA ou si une période postérieure est déjà liquidée ; `vat.preview` montre ce qu'elle
passerait. Le paiement (44551 / 512) se saisit dans le journal de banque. Comptes : paramètres comptables.

`/bilan/?as_of=` (`reports.balance_sheet`) : actif et passif par rubrique simplifiée du PCG ; comptes de tiers et
de trésorerie selon le sens de leur solde, amortissements en déduction, résultat de l'exercice en capitaux
propres ; depuis les à-nouveaux de l'exercice s'ils existent, sinon depuis l'origine (résultats antérieurs non
reportés à part). Export CSV. Document de pilotage, pas les comptes annuels.

## TVA : encaissements, autoliquidation, CA3

- **Prestations de services** : sauf option pour les débits (`AccountingSettings.vat_on_debits`), leur TVA reste en
  attente (445800) jusqu'au paiement ; chaque encaissement (`post_payment`) en fait passer la part correspondante en
  TVA collectée, un remboursement l'inverse. Les lignes de TVA portent leur base HT (`vat_base`).
- **Autoliquidation** : `post_purchase(..., reverse_charge="eu_goods" | "eu_services" | "foreign")`, facture hors taxe,
  TVA au taux de chaque ligne due en 445200 et déductible en 445662 ; TVA sur immobilisations en 445620.
- **CA3** : `reports.ca3(début, fin)` et la page TVA donnent les lignes 01, 03, 2A, 3B, 05, la TVA brute par taux,
  16, 17, 19, 20, 22, 23 et 25 ou 28 (CSV). Aide à la déclaration, à contrôler avant télédéclaration.

## Devises et analytique

- Une ligne peut porter un montant d'origine en devise (`Line(currency="USD", currency_amount=...)`, `currency=("USD", montant)`
  dans l'API) : la comptabilité reste en euros, le FEC renseigne Montantdevise / Idevise. Au lettrage, l'écart de change se
  solde en 666 / 766 (`reconciliation.write_off(..., exchange=True)`).
- Sections analytiques (`AnalyticSection`) sur les lignes de charges et de produits : à la saisie, par l'API
  (`SaleLine(analytic="WEB")`, `PurchaseLine(analytic=...)`) ou après validation depuis l'écriture (hors empreinte).
  `/analytique/` : produits, charges et résultat par section.

## Comptes annuels

`/comptes-annuels/` (`annual.annual_accounts`) : bilan détaillé (brut, amortissements et dépréciations, net) et compte de
résultat (exploitation, financier, exceptionnel, impôt) selon la présentation du PCG, avec l'exercice précédent ; CSV.
Document de travail : la liasse fiscale et sa transmission EDI-TDFC restent du ressort de l'expert-comptable.

## Inaltérabilité : empreinte chaînée

À sa validation, chaque écriture reçoit un rang (`seal_index`) et une empreinte SHA-256 (`seal`) de son contenu
(numéro, journal, dates, pièce, libellé, montant, lignes) et de l'empreinte précédente (`accounting_fr.seal`).
Une écriture validée modifiée, supprimée ou insérée après coup, même directement en base, casse la chaîne.
La clôture d'un exercice enregistre le dernier maillon comme sceau de l'exercice (`FiscalPeriod.closing_seal`,
affiché et journalisé) : conservé hors du logiciel, il prouve que rien n'a été réécrit, même par quelqu'un qui
recalculerait toute la chaîne. Lettrage, pointage bancaire et lien de contre-passation ne sont pas scellés.

```bash
python manage.py verifier_integrite   # code de sortie 1 en cas d'altération ; aussi page /integrite/
python manage.py ancrer_sceau         # sceau du jour par e-mail (destinataires des sceaux) et dans SEAL_ARCHIVE_DIR
```

Le sceau part aussi à chaque clôture. Conservé hors du logiciel, il se contrôle sur la page Intégrité (maillon et
empreinte) ; la vérification compare la chaîne à chaque envoi enregistré.

La migration scelle les écritures déjà validées, dans l'ordre de validation.

## Immobilisations

`/immobilisations/` : registre (valeur d'origine, mise en service, durée, comptes), plan d'amortissement linéaire
prorata temporis en jours, sur une année de 360 jours (`assets.schedule`), et écarts entre le registre et les comptes
de la classe 2 (`assets.register_gaps`).

```python
from accounting_fr import assets

assets.post_depreciations(exercice)        # dotations au dernier jour : 6811 / 28, ce qui reste dû (rattrapage compris)
assets.dispose(immo, date, prix=None)      # dotation complémentaire, puis sortie : 28 + 675 / 2 ; prix en 462 / 775
```

Comptes d'amortissement et de dotation déduits du compte d'immobilisation (218300 → 281830, 6811x), modifiables.
L'acquisition se comptabilise par la facture du fournisseur. Amortissement linéaire ou dégressif fiscal (coefficients
1,25 / 1,75 / 2,25, bascule en linéaire), dépréciations (`assets.impair`, 6816 / 29, reprises en 7816), TVA sur le prix
de cession. Non traités : amortissement dérogatoire, révision du plan après dépréciation, régularisations de TVA.

## Lettrage

Comptes de tiers (préfixes `ACCOUNTING["RECONCILABLE_PREFIXES"]`, 40 et 41 par défaut), par compte
auxiliaire : `/lettrage/` (soldes par tiers), `/lettrage/<compte>/` (lettrage manuel), `/lettrage/balance-agee/`.

```python
from accounting_fr import reconciliation

reconciliation.auto_reconcile()               # même pièce, même référence, montant unique, tiers soldé
code = reconciliation.reconcile([id1, id2])   # lignes équilibrées d'un même tiers -> "A", "B"… "AA"
reconciliation.write_off([id1, id2])          # solde un écart ≤ 5 € en OD (658 / 758), puis lettre
reconciliation.unreconcile(account, "A")
reconciliation.aged_balance(account, as_of)   # 0–30, 31–60, 61–90, plus de 90 jours
```

```bash
python manage.py lettrage_auto   # à planifier (par exemple chaque heure)
```

Seules les lignes validées des exercices ouverts se lettrent ; le code et la date de lettrage
alimentent EcritureLet et DateLet du FEC. Permission : `accounting.reconcile_entries` (solder un
écart demande aussi `validate_transaction`). Dans la saisie manuelle, la colonne « Tiers » renseigne
le compte auxiliaire d'une ligne.

## Rapprochement bancaire

Par journal de banque : `/banque/` (journaux, import, relevés), `/banque/<journal>/` (pointage),
`/banque/<journal>/etat/` (état de rapprochement). Les fonctions travaillent sur le compte de
trésorerie du journal (`journal.account`, et `bank.journal_of(compte)` en sens inverse).

```python
from accounting_fr import bank

result = bank.import_statement(account, contenu, "releve.ofx")  # CSV, OFX, CAMT.053, CFONB 120
bank.auto_match(account)                     # même montant à 7 jours près, sans ambiguïté
bank.match(account, [ligne_releve], [ligne_ecriture, ...])       # totaux égaux
bank.create_entry(ligne_releve, compte_627)  # écriture validée au journal de la banque, pointée
bank.mark_prior(account, date)               # écritures antérieures au premier relevé
bank.state(account, date)                    # solde comptable, en-cours, solde du relevé, écart
```

```bash
python manage.py rapprochement_auto   # à planifier avec lettrage_auto
```

Un CSV aux colonnes inconnues demande la correspondance (date, libellé, montant ou débit / crédit).
Une opération déjà importée est ignorée (identifiant de la banque, sinon date, montant et libellé).
Permission : `accounting.reconcile_bank` (passer une écriture depuis le relevé demande aussi
`add_transaction` et `validate_transaction`).

## Contrôle du FEC

```bash
python manage.py check_fec 123456789FEC20261231.txt   # n'importe quel FEC
python manage.py check_fec --year 2026 --output fec/  # FEC de l'exercice, généré puis contrôlé
```

Règles de structure de l'article A47 A-1 du LPF (zones, formats, couples de zones, équilibre,
numérotation continue et chronologique, exercice, nom de fichier) ; aussi depuis la page des exports.
`accounting_fr.fec_check.check_fec(contenu, nom)` renvoie erreurs, avertissements et statistiques.

## Tests

```bash
python manage.py test accounting
```

## Licence

AGPL-3.0-or-later : voir [LICENSE](LICENSE). Une licence commerciale (sans l'obligation de publier les modifications) peut être accordée par l'auteur : ouvrez une issue.
