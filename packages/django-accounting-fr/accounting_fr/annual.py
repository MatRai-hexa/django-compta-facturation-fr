"""
Comptes annuels, présentation du plan comptable général (modèle en tableau) : bilan détaillé (actif
brut, amortissements et dépréciations, net) et compte de résultat (exploitation, financier,
exceptionnel, impôt), exercice N et exercice précédent.

Chaque compte est rattaché au poste dont le préfixe est le plus long ; les comptes de tiers et de
trésorerie le sont selon le sens de leur solde (débiteur à l'actif, créditeur au passif). Document
de travail : la liasse fiscale (formulaires 2050 et suivants ou 2033, transmission EDI-TDFC) reste
établie par l'expert-comptable, qui valide aussi les écritures d'inventaire.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, timedelta

from django.db.models import Sum

from .models import ZERO, Account, FiscalPeriod, LedgerEntry, Transaction, to_cents

# (clé, poste, préfixes du brut, préfixes des amortissements et dépréciations)
ASSETS = [
    ("intangible", "Immobilisations incorporelles", ["20"], ["280", "290"]),
    ("tangible", "Immobilisations corporelles", ["21", "22", "23"], ["281", "282", "291", "292", "293"]),
    ("financial", "Immobilisations financières", ["26", "27"], ["296", "297"]),
    ("stocks", "Stocks et en-cours", ["31", "32", "33", "34", "35", "37"], ["39"]),
    ("advances_paid", "Avances et acomptes versés sur commandes", ["4091"], []),
    ("receivables", "Créances clients et comptes rattachés", ["41"], ["491"]),
    ("other_receivables", "Autres créances", ["40", "42", "43", "44", "45", "46", "47", "48"], ["495", "496"]),
    ("securities", "Valeurs mobilières de placement", ["50"], ["590"]),
    ("cash", "Disponibilités", ["51", "52", "53", "54", "58"], []),
    ("prepaid", "Charges constatées d'avance", ["486"], []),
]
ASSET_GROUPS = [("Actif immobilisé", ["intangible", "tangible", "financial"]),
                ("Actif circulant", ["stocks", "advances_paid", "receivables", "other_receivables", "securities", "cash", "prepaid"])]

LIABILITIES = [
    ("capital", "Capital social ou individuel", ["101", "108"]),
    ("premiums", "Primes d'émission, de fusion, d'apport", ["104"]),
    ("revaluation", "Écarts de réévaluation", ["105"]),
    ("legal_reserve", "Réserve légale", ["1061"]),
    ("reserves", "Réserves statutaires, réglementées et autres", ["106"]),
    ("retained", "Report à nouveau", ["11"]),
    ("result", "Résultat de l'exercice (bénéfice ou perte)", ["12"]),
    ("grants", "Subventions d'investissement", ["13"]),
    ("regulated", "Provisions réglementées", ["14"]),
    ("provisions", "Provisions pour risques et charges", ["15"]),
    ("bank_debts", "Emprunts et dettes auprès des établissements de crédit", ["164", "5"]),
    ("other_financial", "Emprunts et dettes financières divers", ["16", "17", "455"]),
    ("advances_received", "Avances et acomptes reçus sur commandes", ["4191"]),
    ("suppliers", "Dettes fournisseurs et comptes rattachés", ["401", "403", "408"]),
    ("tax_social", "Dettes fiscales et sociales", ["42", "43", "44"]),
    ("fixed_asset_debts", "Dettes sur immobilisations et comptes rattachés", ["404", "405"]),
    ("other_debts", "Autres dettes", ["4"]),
    ("deferred_income", "Produits constatés d'avance", ["487"]),
]
LIABILITY_GROUPS = [("Capitaux propres", ["capital", "premiums", "revaluation", "legal_reserve", "reserves", "retained", "result",
                                         "grants", "regulated"]),
                    ("Provisions", ["provisions"]),
                    ("Dettes", ["bank_debts", "other_financial", "advances_received", "suppliers", "tax_social",
                                "fixed_asset_debts", "other_debts", "deferred_income"])]

# Compte de résultat : (section, [(poste, préfixes)]) ; produits : crédit − débit, charges : débit − crédit
INCOME = OrderedDict([
    ("operating_income", ("Produits d'exploitation", [
        ("Ventes de marchandises", ["707", "7097"]),
        ("Production vendue de biens", ["701", "702", "703", "7091", "7092", "7093"]),
        ("Production vendue de services", ["704", "705", "706", "708", "7094", "7095", "7096", "7098"]),
        ("Production stockée", ["713"]),
        ("Production immobilisée", ["72"]),
        ("Subventions d'exploitation", ["74"]),
        ("Reprises sur amortissements, dépréciations et provisions, transferts de charges", ["781", "791"]),
        ("Autres produits", ["75"]),
    ])),
    ("operating_expenses", ("Charges d'exploitation", [
        ("Achats de marchandises (y compris droits de douane)", ["607", "6087", "6097"]),
        ("Variation de stock de marchandises", ["6037"]),
        ("Achats de matières premières et autres approvisionnements", ["601", "602", "6081", "6082", "6091", "6092"]),
        ("Variation de stock de matières premières et approvisionnements", ["6031", "6032"]),
        ("Autres achats et charges externes", ["604", "605", "606", "6084", "6085", "6086", "6094", "6095", "6096", "61", "62"]),
        ("Impôts, taxes et versements assimilés", ["63"]),
        ("Salaires et traitements", ["641", "644"]),
        ("Charges sociales", ["645", "646", "647", "648"]),
        ("Dotations aux amortissements sur immobilisations", ["6811", "6812"]),
        ("Dotations aux dépréciations sur immobilisations", ["6816"]),
        ("Dotations aux dépréciations sur actif circulant", ["6817"]),
        ("Dotations aux provisions", ["6815"]),
        ("Autres charges", ["65"]),
    ])),
    ("financial_income", ("Produits financiers", [
        ("Produits financiers de participations et d'autres valeurs", ["761", "762"]),
        ("Autres intérêts et produits assimilés", ["763", "764", "765", "768"]),
        ("Différences positives de change", ["766"]),
        ("Produits nets sur cessions de valeurs mobilières de placement", ["767"]),
        ("Reprises sur dépréciations et provisions, transferts de charges", ["786", "796"]),
    ])),
    ("financial_expenses", ("Charges financières", [
        ("Intérêts et charges assimilées", ["661", "664", "665", "668"]),
        ("Différences négatives de change", ["666"]),
        ("Charges nettes sur cessions de valeurs mobilières de placement", ["667"]),
        ("Dotations aux amortissements, dépréciations et provisions", ["686"]),
    ])),
    ("exceptional_income", ("Produits exceptionnels", [
        ("Sur opérations de gestion", ["771"]),
        ("Sur opérations en capital (dont produits des cessions d'éléments d'actif)", ["775", "777", "778"]),
        ("Reprises sur dépréciations et provisions, transferts de charges", ["787", "797"]),
    ])),
    ("exceptional_expenses", ("Charges exceptionnelles", [
        ("Sur opérations de gestion", ["671"]),
        ("Sur opérations en capital (dont valeur comptable des éléments d'actif cédés)", ["675", "678"]),
        ("Dotations aux amortissements, dépréciations et provisions", ["687"]),
    ])),
    ("tax", ("Participation et impôts", [
        ("Participation des salariés aux résultats", ["691"]),
        ("Impôts sur les bénéfices", ["695", "696", "698", "699"]),
    ])),
])
INCOME_SIDES = {"operating_income": 1, "financial_income": 1, "exceptional_income": 1}  # sinon charges


def _match(code: str, prefixes) -> int:
    """Longueur du plus long préfixe de `prefixes` correspondant à `code` (0 : aucun)."""
    return max((len(p) for p in prefixes if code.startswith(p)), default=0)


def _balances(start, end):
    rows = (LedgerEntry.objects.filter(transaction__is_validated=True, transaction__date__lte=end)
            .filter(**({"transaction__date__gte": start} if start else {}))
            .values("account_id").annotate(d=Sum("debit"), c=Sum("credit")))
    accounts = Account.objects.in_bulk([r["account_id"] for r in rows])
    return [(accounts[r["account_id"]], to_cents(r["d"]) - to_cents(r["c"])) for r in rows]


def _scope(period: FiscalPeriod):
    """Début des soldes de bilan : ouverture de l'exercice s'il a ses à-nouveaux, sinon l'origine."""
    opening = Transaction.objects.filter(type="opening", is_validated=True, date__gte=period.date_start,
                                         date__lte=period.date_end).exists()
    return period.date_start if opening else None


def income_statement(start: date, end: date) -> dict:
    """Compte de résultat de la période (écritures validées)."""
    sections = OrderedDict()
    unmapped = []
    for key, (label, items) in INCOME.items():
        sections[key] = {"key": key, "label": label, "lines": [{"label": text, "prefixes": prefixes, "amount": ZERO}
                                                               for text, prefixes in items], "total": ZERO}
    for account, balance in _balances(start, end):
        if account.pcg_class not in "67":
            continue
        best, target = 0, None
        for key, section in sections.items():
            for line in section["lines"]:
                length = _match(account.code, line["prefixes"])
                if length > best:
                    best, target = length, (key, line)
        if target is None:
            key = "operating_income" if account.pcg_class == "7" else "operating_expenses"
            target = (key, sections[key]["lines"][-1])  # autres produits / autres charges
            unmapped.append(account.code)
        key, line = target
        amount = -balance if INCOME_SIDES.get(key) else balance
        line["amount"] += amount
        sections[key]["total"] += amount
    total = {key: section["total"] for key, section in sections.items()}
    operating = total["operating_income"] - total["operating_expenses"]
    financial = total["financial_income"] - total["financial_expenses"]
    exceptional = total["exceptional_income"] - total["exceptional_expenses"]
    net = operating + financial + exceptional - total["tax"]
    return {"start": start, "end": end, "sections": sections, "operating": operating, "financial": financial,
            "current": operating + financial, "exceptional": exceptional, "net": net, "unmapped": unmapped}


def balance_sheet_detail(period: FiscalPeriod, as_of: date | None = None) -> dict:
    """Bilan détaillé au `as_of` (fin de l'exercice par défaut) : actif brut / amortissements / net, passif."""
    as_of = as_of or period.date_end
    start = _scope(period)
    assets = OrderedDict((key, {"key": key, "label": label, "gross": ZERO, "contra": ZERO})
                         for key, label, _, _ in ASSETS)
    liabilities = OrderedDict((key, {"key": key, "label": label, "amount": ZERO}) for key, label, _ in LIABILITIES)
    result_prior = ZERO
    for account, balance in _balances(start, as_of):
        code = account.code
        if account.pcg_class in "67" or not balance:
            continue
        contra = next((key for key, _, _, contras in ASSETS if _match(code, contras)), None)
        if contra:
            assets[contra]["contra"] -= balance  # amortissements et dépréciations : soldes créditeurs
            continue
        if code[0] in "23" or (code[0] in "45" and balance > 0) or code.startswith("486"):
            key = max(ASSETS, key=lambda item: _match(code, item[2]))[0] if any(_match(code, a[2]) for a in ASSETS) else "other_receivables"
            assets[key]["gross"] += balance
        else:
            key = max(LIABILITIES, key=lambda item: _match(code, item[2]))[0] \
                if any(_match(code, item[2]) for item in LIABILITIES) else "other_debts"
            liabilities[key]["amount"] -= balance
    if start is None:  # sans à-nouveaux : résultats antérieurs non reportés, à part
        result_prior = income_statement(date(1900, 1, 1), period.date_start - timedelta(days=1))["net"]
    current = income_statement(period.date_start, as_of)["net"]
    liabilities["result"]["amount"] += current
    if result_prior:
        liabilities["retained"]["amount"] += result_prior
    for row in assets.values():
        row["net"] = row["gross"] - row["contra"]
    asset_groups = [{"label": label, "rows": [assets[k] for k in keys if assets[k]["gross"] or assets[k]["contra"]],
                     "gross": sum((assets[k]["gross"] for k in keys), ZERO), "contra": sum((assets[k]["contra"] for k in keys), ZERO),
                     "net": sum((assets[k]["net"] for k in keys), ZERO)} for label, keys in ASSET_GROUPS]
    liability_groups = [{"label": label, "rows": [liabilities[k] for k in keys if liabilities[k]["amount"]],
                         "amount": sum((liabilities[k]["amount"] for k in keys), ZERO)} for label, keys in LIABILITY_GROUPS]
    total_assets = sum((g["net"] for g in asset_groups), ZERO)
    total_liabilities = sum((g["amount"] for g in liability_groups), ZERO)
    return {"as_of": as_of, "assets": asset_groups, "liabilities": liability_groups, "total_assets": total_assets,
            "total_liabilities": total_liabilities, "result": current, "prior_unreported": result_prior,
            "is_balanced": total_assets == total_liabilities, "from_opening": start is not None}


def annual_accounts(period: FiscalPeriod) -> dict:
    """Bilan et compte de résultat de l'exercice, avec l'exercice précédent pour comparaison."""
    previous = FiscalPeriod.objects.filter(date_end__lt=period.date_start).order_by("-date_end").first()
    return {
        "period": period, "previous": previous,
        "balance": balance_sheet_detail(period), "balance_previous": balance_sheet_detail(previous) if previous else None,
        "income": income_statement(period.date_start, period.date_end),
        "income_previous": income_statement(previous.date_start, previous.date_end) if previous else None,
    }
