# django-facturation-fr

Application Django de facturation : factures et avoirs numérotés, mentions légales françaises,
PDF et **Factur-X** (PDF/A-3 avec le XML UN/CEFACT CII au profil **EN 16931**, le format socle de
la facturation électronique). Indépendante de tout autre module : le projet hôte lui décrit ses
ventes via une API Python.

- **Numérotation continue** par série et par année (`F2026-00001`, `AV2026-00001`), attribuée sous verrou.
- **Intangibilité** : une facture émise ne se modifie ni ne se supprime ; elle s'annule par un avoir
  qui en reprend exactement les montants.
- **Montants conformes à la norme** : TVA par taux ; prix TTC (particuliers) ou HT (professionnels) ;
  remises de pied de facture par taux ; total à payer égal au prix payé, au centime.
- **Mentions** : identité complète du vendeur (figée à l'émission), SIREN et n° de TVA du client
  professionnel, adresse de livraison, nature des opérations, option pour les débits, exonération,
  pénalités de retard et indemnité de 40 € pour les professionnels, mention « acquittée ».
- **Archivage** : PDF enregistré à l'émission avec son empreinte SHA-256.
- **Factur-X** : XML vérifié par le schéma XSD officiel à chaque émission, polices intégrées,
  profil de couleurs sRGB et métadonnées PDF/A-3.

## Installation

```bash
pip install django-facturation-fr
```

```python
INSTALLED_APPS = [..., "facturation_fr"]  # libellé d'application : « invoicing »
urlpatterns = [..., path("facturation/", include("facturation_fr.urls"))]
```

Pages réservées aux permissions `invoicing.*` ; le lien de téléchargement client
(`/facturation/telecharger/<jeton>/`) est public et doit être exempté d'une éventuelle connexion obligatoire.

## API

```python
from facturation_fr import api

invoice = api.issue_invoice(
    "order:42",                                     # clé unique : rejouer renvoie la même facture
    api.Buyer("Martin SARL", "5 avenue Foch", "75016", "Paris", siren="732829320", vat_number="FR40732829320"),
    [api.Item("T-shirt", 2, Decimal("25.00")),       # prix TTC par défaut (prices_include_tax=False pour du HT)
     api.Item("Livre", 1, Decimal("18.00"), Decimal("0.055")),
     api.Item("Atelier", 1, Decimal("60.00"), nature="services")],   # biens par défaut (nature des opérations)
    [api.Allowance("Code promo DIX", Decimal("5.00"))],
    paid_at=day, payment_method="Carte bancaire", document=("order", 42))
api.credit_invoice("order:42:credit", invoice, reason="Retour du colis")
api.invoices_for("order", 42)
```

Une identité de vendeur incomplète (raison sociale, adresse, SIREN, n° de TVA si la facture
comporte de la TVA) lève `api.InvoicingError` : rien n'est émis.

## Intégration au projet hôte

```python
INVOICING = {
    "BASE_TEMPLATE": "monprojet/base.html",            # bloc « content »
    "SELLER": "monprojet.invoicing.seller",            # f() -> {"name", "address", "postal_code", "city", "siret", ...}
    "BRANDING": "monprojet.invoicing.branding",        # f() -> {"logo_path", "color"}
    "DOCUMENT_URL": "monprojet.invoicing.document_url",
    "ON_ISSUED": ["monprojet.invoicing.on_issued"],    # f(invoice) après chaque émission
}
```

Les paramètres saisis dans **Paramètres de facturation** priment sur `SELLER`.

## Facturation électronique (plateforme agréée)

`invoicing.einvoicing` et `invoicing.platforms` :

- **aiguillage** : facture à un professionnel établi en France (SIREN de l'acheteur) → facture électronique,
  déposée sur la plateforme ; vente à un particulier ou à un professionnel étranger → **e-reporting** ;
- **cycle de vie** : les 14 statuts officiels (200 Déposée à 213 Rejetée ; obligatoires : 200, 210, 212, 213),
  historisés ; le statut **212 Encaissée** est transmis dès que la facture est payée ;
- **factures reçues** : import ou réception d'un PDF Factur-X, d'un XML CII ou UBL, lecture des données EN 16931
  (fournisseur, montants, TVA par taux, échéance), contrôle du destinataire, sans doublon ; statuts de l'acheteur
  (prise en charge, approuvée, en litige, refusée avec motif…) ; XML lu sans entités externes ni accès réseau ;
- **e-reporting** au format officiel (`invoicing.ereporting`, flux 10 des spécifications externes DGFiP v3.2,
  validé contre les XSD officiels fournis dans `invoicing/xsd/ereporting`) — deux transmissions par période :
  - *transactions* : ventes aux particuliers par jour, devise et catégorie (TLB1 biens, TPS1 services, TNT1 hors
    champ de la TVA française), ventilées par taux, avoirs déduits (10.3) ; factures aux professionnels établis
    hors de France, une à une, avec leurs remises et leurs lignes (10.1) ;
  - *encaissements* des prestations de services (sauf option pour la TVA sur les débits) : par jour pour les
    particuliers (10.4), par facture pour les professionnels étrangers (10.2), remboursements en négatif ;
  - périodes selon le **régime de TVA** (réel mensuel : transactions par décade, encaissements par mois ; réel
    trimestriel et simplifié : par mois ; franchise : par bimestre civil) ;
  - une période transmise dont les données changent est retransmise en **rectificative** (RE, version suivante),
    qui annule et remplace la précédente ; l'historique des transmissions est conservé ;
  - émetteur : la plateforme agréée (matricule à 4 caractères, fourni par le connecteur ou saisi dans les
    paramètres) ; déclarant : le vendeur (SIREN) ;
- `python manage.py sync_einvoicing` (planificateur) : statuts, dépôts, encaissements, factures reçues, e-reporting.

Plateformes fournies : **Dépôt manuel** (fichiers à déposer sur le portail de n'importe quelle plateforme agréée,
statuts reportés à la main) et **Plateforme simulée** (tests). Une plateforme réelle s'ajoute en sous-classant
`invoicing.platforms.Platform` (`send_invoice`, `fetch_events`, `send_payment`, `fetch_incoming`,
`send_buyer_status`, `send_ereport`) et en la déclarant dans `INVOICING["PLATFORMS"]`, ses identifiants dans
`INVOICING["PLATFORM_OPTIONS"]`. `INVOICING["ON_RECEIVED_STATUS"]` permet de comptabiliser les factures reçues.

## Limites

- Le contrôle **schematron** EN 16931 (règles métier) demande un moteur XSLT 2 (Saxon, Java) : non
  exécuté ici ; les règles principales (BR-CO-10/13/15/17, BR-S-02, BR-CO-25, mentions) sont garanties
  par le code et les tests. La conformité PDF/A n'a pas été vérifiée avec veraPDF.
- Aucun connecteur vers une plateforme agréée réelle n'est encore fourni (choix du prestataire à faire) ;
  en dépôt manuel, le flux 10 (XML) se télécharge pour être déposé sur le portail de la plateforme.
- E-reporting : les acomptes (TVA exigible à l'encaissement sur les livraisons de biens) et le régime de la marge
  (TMA1) ne sont pas gérés.

## Tests

```bash
python manage.py test invoicing
```

## Licence

AGPL-3.0-or-later : voir [LICENSE](LICENSE). Une licence commerciale (sans l'obligation de publier les modifications) peut être accordée par l'auteur : ouvrez une issue.
