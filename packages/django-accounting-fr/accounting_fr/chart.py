"""Plan comptable, journaux et taux de TVA par défaut (PCG). Idempotent."""
from decimal import Decimal

# (numéro, intitulé, nature)
ACCOUNTS = [
    ("101000", "Capital", "equity"),
    ("108000", "Compte de l'exploitant", "equity"),
    ("110000", "Report à nouveau (solde créditeur)", "equity"),
    ("119000", "Report à nouveau (solde débiteur)", "equity"),
    ("120000", "Résultat de l'exercice (bénéfice)", "equity"),
    ("129000", "Résultat de l'exercice (perte)", "equity"),
    ("205000", "Concessions, brevets, licences, logiciels", "asset"),
    ("215400", "Matériel industriel", "asset"),
    ("218200", "Matériel de transport", "asset"),
    ("218300", "Matériel de bureau et informatique", "asset"),
    ("218400", "Mobilier", "asset"),
    ("280500", "Amortissements des concessions, brevets, licences, logiciels", "asset"),
    ("281540", "Amortissements du matériel industriel", "asset"),
    ("281820", "Amortissements du matériel de transport", "asset"),
    ("281830", "Amortissements du matériel de bureau et informatique", "asset"),
    ("281840", "Amortissements du mobilier", "asset"),
    ("370000", "Stocks de marchandises", "asset"),
    ("401000", "Fournisseurs", "liability"),
    ("404000", "Fournisseurs d'immobilisations", "liability"),
    ("411000", "Clients", "asset"),
    ("419100", "Clients - avances et acomptes reçus", "liability"),
    ("445510", "TVA à décaisser", "liability"),
    ("462000", "Créances sur cessions d'immobilisations", "asset"),
    ("445200", "TVA due intracommunautaire et autoliquidée", "liability"),
    ("445620", "TVA déductible sur immobilisations", "asset"),
    ("445660", "TVA déductible sur autres biens et services", "asset"),
    ("445662", "TVA déductible intracommunautaire et autoliquidée", "asset"),
    ("445671", "Crédit de TVA à reporter", "asset"),
    ("445711", "TVA collectée 20 %", "liability"),
    ("445712", "TVA collectée 10 %", "liability"),
    ("445713", "TVA collectée 5,5 %", "liability"),
    ("445714", "TVA collectée 2,1 %", "liability"),
    ("445800", "TVA collectée en attente d'encaissement", "liability"),
    ("467100", "Stripe - fonds à recevoir", "asset"),
    ("467200", "PayPal - fonds à recevoir", "asset"),
    ("512000", "Banque", "asset"),
    ("530000", "Caisse", "asset"),
    ("580000", "Virements internes", "asset"),
    ("607000", "Achats de marchandises", "expense"),
    ("603700", "Variation des stocks de marchandises", "expense"),
    ("606000", "Achats non stockés de matières et fournitures", "expense"),
    ("613200", "Locations immobilières", "expense"),
    ("622600", "Honoraires", "expense"),
    ("623000", "Publicité, publications", "expense"),
    ("624100", "Transports sur achats", "expense"),
    ("624200", "Transports sur ventes", "expense"),
    ("626000", "Frais postaux et de télécommunications", "expense"),
    ("627000", "Services bancaires et frais de paiement", "expense"),
    ("651000", "Redevances pour logiciels", "expense"),
    ("658000", "Charges diverses de gestion courante", "expense"),
    ("666000", "Pertes de change financières", "expense"),
    ("675000", "Valeurs comptables des éléments d'actif cédés", "expense"),
    ("681110", "Dotations aux amortissements des immobilisations incorporelles", "expense"),
    ("681120", "Dotations aux amortissements des immobilisations corporelles", "expense"),
    ("681600", "Dotations aux dépréciations des immobilisations", "expense"),
    ("707000", "Ventes de marchandises", "revenue"),
    ("708500", "Ports et frais accessoires facturés", "revenue"),
    ("709700", "Rabais, remises et ristournes accordés", "revenue"),
    ("758000", "Produits divers de gestion courante", "revenue"),
    ("766000", "Gains de change financiers", "revenue"),
    ("775000", "Produits des cessions d'éléments d'actif", "revenue"),
    ("781600", "Reprises sur dépréciations des immobilisations", "revenue"),
]

# (code, libellé, type, compte de trésorerie des journaux de banque)
JOURNALS = [
    ("VT", "Ventes", "sales", None),
    ("BQ", "Banque", "bank", "512000"),
    ("ST", "Stripe", "bank", "467100"),
    ("PP", "PayPal", "bank", "467200"),
    ("CA", "Caisse", "bank", "530000"),
    ("AC", "Achats", "purchases", None),
    ("OD", "Opérations diverses", "misc", None),
    ("AN", "À-nouveaux", "opening", None),
]

# (taux, libellé, compte de TVA collectée)
VAT_RATES = [
    (Decimal("0.2000"), "TVA 20 %", "445711"),
    (Decimal("0.1000"), "TVA 10 %", "445712"),
    (Decimal("0.0550"), "TVA 5,5 %", "445713"),
    (Decimal("0.0210"), "TVA 2,1 %", "445714"),
    (Decimal("0.0000"), "Exonéré / non soumis", None),
]

SETTINGS_ACCOUNTS = {
    "customer_account": "411000",
    "sales_account": "707000",
    "shipping_account": "708500",
    "bank_account": "512000",
    "fees_account": "627000",
    "profit_account": "120000",
    "loss_account": "129000",
    "supplier_account": "401000",
    "purchases_account": "607000",
    "deductible_vat_account": "445660",
    "transfer_account": "580000",
    "vat_payable_account": "445510",
    "vat_credit_account": "445671",
    "pending_vat_account": "445800",
}


# (code du moyen de paiement, libellé, journal de trésorerie) : modifiables ensuite dans les réglages
PAYMENT_ACCOUNTS = [
    ("cod", "Paiement à la livraison", "CA"),
    ("stripe", "Stripe", "ST"),
    ("paypal", "PayPal", "PP"),
]


def install_chart(Account, Journal, VATRate, AccountingSettings, PaymentAccount=None):
    """Crée ce qui manque, sans modifier l'existant."""
    accounts = {}
    for code, name, kind in ACCOUNTS:
        accounts[code], _ = Account.objects.get_or_create(code=code, defaults={"name": name, "account_type": kind})
    journals = {}
    for code, label, kind, account_code in JOURNALS:
        account = accounts.get(account_code)
        if account is not None and Journal.objects.filter(account=account).exclude(code=code).exists():
            account = None  # compte déjà rattaché à un autre journal
        journals[code], _ = Journal.objects.get_or_create(code=code, defaults={"label": label, "kind": kind,
                                                                               "account": account})
    for rate, label, account_code in VAT_RATES:
        VATRate.objects.get_or_create(rate=rate, defaults={"label": label, "collected_account": accounts.get(account_code)})
    if PaymentAccount is not None:
        for method, label, code in PAYMENT_ACCOUNTS:
            if journals[code].account_id:
                PaymentAccount.objects.get_or_create(method=method, defaults={"label": label, "journal": journals[code]})
    settings_obj = AccountingSettings.objects.filter(pk=1).first()
    if settings_obj is None:
        settings_obj = AccountingSettings.objects.create(
            pk=1, **{field: accounts[code] for field, code in SETTINGS_ACCOUNTS.items()})
    return settings_obj


def ensure_chart_of_accounts():
    from .models import Account, AccountingSettings, Journal, PaymentAccount, VATRate
    return install_chart(Account, Journal, VATRate, AccountingSettings, PaymentAccount)
