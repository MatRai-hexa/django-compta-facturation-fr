"""États comptables : journaux, centralisateur, balance, grand livre, compte de résultat, TVA, tableau de bord."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, timedelta
from decimal import Decimal

from django.db import models
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .models import ZERO, Account, AccountingSettings, Journal, LedgerEntry, Transaction, VATRate, to_cents as _d

PCG_CLASSES = OrderedDict([
    ("1", "Comptes de capitaux"), ("2", "Comptes d'immobilisations"), ("3", "Comptes de stocks"),
    ("4", "Comptes de tiers"), ("5", "Comptes financiers"), ("6", "Comptes de charges"), ("7", "Comptes de produits"),
])


def _lines(start=None, end=None, validated_only=False):
    qs = LedgerEntry.objects.all()
    if start:
        qs = qs.filter(transaction__date__gte=start)
    if end:
        qs = qs.filter(transaction__date__lte=end)
    if validated_only:
        qs = qs.filter(transaction__is_validated=True)
    return qs


def journal_entries(journal: Journal | None, start=None, end=None, validated_only=False):
    """Écritures d'un journal (ou de tous) sur la période, dans l'ordre du livre-journal."""
    qs = Transaction.objects.filter(entries__isnull=False).distinct()
    if journal is not None:
        qs = qs.filter(journal=journal)
    if start:
        qs = qs.filter(date__gte=start)
    if end:
        qs = qs.filter(date__lte=end)
    if validated_only:
        qs = qs.filter(is_validated=True)
    return qs.select_related("journal").order_by("date", "journal__code", "number", "pk")


def journal_book(transactions) -> list[dict]:
    """Lignes de chaque écriture, avec ses totaux (pour l'édition du journal)."""
    transactions = list(transactions)
    lines = {}
    for e in (LedgerEntry.objects.filter(transaction__in=transactions).select_related("account")
              .order_by("transaction_id", "-debit", "id")):
        lines.setdefault(e.transaction_id, []).append(e)
    book = []
    for txn in transactions:
        entries = lines.get(txn.pk, [])
        book.append({"txn": txn, "lines": entries, "debit": sum((e.debit for e in entries), ZERO),
                     "credit": sum((e.credit for e in entries), ZERO)})
    return book


def journal_totals(journal: Journal | None, start=None, end=None, validated_only=False) -> dict:
    lines = _lines(start, end, validated_only)
    if journal is not None:
        lines = lines.filter(transaction__journal=journal)
    totals = lines.aggregate(debit=Sum("debit"), credit=Sum("credit"), entries=Count("transaction", distinct=True))
    return {"debit": _d(totals["debit"]), "credit": _d(totals["credit"]), "entries": totals["entries"] or 0,
            "drafts": journal_entries(journal, start, end).filter(is_validated=False).count() if not validated_only else 0}


def centralizer(start=None, end=None, validated_only=False) -> dict:
    """
    Journal centralisateur : totaux débit / crédit de chaque journal, mois par mois.
    Son total général doit être égal à celui de la balance sur la même période.
    """
    rows = (_lines(start, end, validated_only)
            .annotate(month=TruncMonth("transaction__date"))
            .values("transaction__journal_id", "month")
            .annotate(debit=Sum("debit"), credit=Sum("credit"), entries=Count("transaction", distinct=True))
            .order_by("transaction__journal_id", "month"))
    journals = Journal.objects.in_bulk([r["transaction__journal_id"] for r in rows])
    by_journal = OrderedDict()
    months = OrderedDict()
    total = {"debit": ZERO, "credit": ZERO, "entries": 0}
    for r in sorted(rows, key=lambda r: (journals[r["transaction__journal_id"]].code, r["month"])):
        journal = journals[r["transaction__journal_id"]]
        debit, credit = _d(r["debit"]), _d(r["credit"])
        group = by_journal.setdefault(journal.pk, {"journal": journal, "months": [], "debit": ZERO, "credit": ZERO,
                                                   "entries": 0})
        group["months"].append({"month": r["month"], "debit": debit, "credit": credit, "entries": r["entries"]})
        group["debit"] += debit
        group["credit"] += credit
        group["entries"] += r["entries"]
        month = months.setdefault(r["month"], {"month": r["month"], "debit": ZERO, "credit": ZERO})
        month["debit"] += debit
        month["credit"] += credit
        total["debit"] += debit
        total["credit"] += credit
        total["entries"] += r["entries"]
    balance = trial_balance(start, end, validated_only)["total"]
    return {"journals": list(by_journal.values()), "months": sorted(months.values(), key=lambda m: m["month"]),
            "total": total, "is_balanced": total["debit"] == total["credit"],
            "matches_trial_balance": balance["debit"] == total["debit"] and balance["credit"] == total["credit"]}


def trial_balance(start=None, end=None, validated_only=False) -> dict:
    """Balance générale : mouvements et soldes par compte, sous-totaux par classe."""
    rows = (_lines(start, end, validated_only).values("account_id")
            .annotate(debit=Sum("debit"), credit=Sum("credit")))
    accounts = Account.objects.in_bulk([r["account_id"] for r in rows])
    classes = OrderedDict()
    total = {"debit": ZERO, "credit": ZERO}
    for r in sorted(rows, key=lambda r: accounts[r["account_id"]].code):
        account = accounts[r["account_id"]]
        debit, credit = _d(r["debit"]), _d(r["credit"])
        balance = debit - credit
        group = classes.setdefault(account.pcg_class, {
            "code": account.pcg_class, "label": PCG_CLASSES.get(account.pcg_class, "Autres"),
            "accounts": [], "debit": ZERO, "credit": ZERO})
        group["accounts"].append({
            "account": account, "debit": debit, "credit": credit,
            "balance_debit": balance if balance > 0 else ZERO, "balance_credit": -balance if balance < 0 else ZERO,
        })
        group["debit"] += debit
        group["credit"] += credit
        total["debit"] += debit
        total["credit"] += credit
    return {"classes": list(classes.values()), "total": total, "is_balanced": total["debit"] == total["credit"]}


def general_ledger(account: Account, start=None, end=None, validated_only=False) -> dict:
    """Grand livre d'un compte : solde d'ouverture, lignes avec solde progressif."""
    opening = ZERO
    if start:
        before = _lines(None, None, validated_only).filter(account=account, transaction__date__lt=start)
        totals = before.aggregate(d=Sum("debit"), c=Sum("credit"))
        opening = _d(totals["d"]) - _d(totals["c"])
    balance = opening
    rows = []
    entries = (_lines(start, end, validated_only).filter(account=account)
               .select_related("transaction", "transaction__journal")
               .order_by("transaction__date", "transaction_id", "id"))
    for entry in entries:
        balance += entry.debit - entry.credit
        rows.append({"entry": entry, "txn": entry.transaction, "balance": balance})
    totals = entries.aggregate(d=Sum("debit"), c=Sum("credit"))
    return {"account": account, "opening": opening, "rows": rows, "closing": balance,
            "debit": _d(totals["d"]), "credit": _d(totals["c"])}


def profit_and_loss(start, end, validated_only=False) -> dict:
    """Compte de résultat : produits (classe 7, crédit - débit) et charges (classe 6, débit - crédit)."""
    def by_account(prefix, sign):
        rows = (_lines(start, end, validated_only).filter(account__code__startswith=prefix)
                .values("account__code", "account__name").annotate(debit=Sum("debit"), credit=Sum("credit"))
                .order_by("account__code"))
        result = []
        for r in rows:
            amount = sign * (_d(r["debit"]) - _d(r["credit"]))
            if amount:
                result.append({"code": r["account__code"], "name": r["account__name"], "amount": amount})
        return result

    revenues = by_account("7", -1)
    expenses = by_account("6", 1)
    total_revenues = sum((r["amount"] for r in revenues), ZERO)
    total_expenses = sum((r["amount"] for r in expenses), ZERO)
    profit = total_revenues - total_expenses
    return {
        "start": start, "end": end, "revenues": revenues, "expenses": expenses,
        "total_revenues": total_revenues, "total_expenses": total_expenses, "profit": profit,
        "margin": (profit / total_revenues * 100) if total_revenues else ZERO,
    }


def vat_summary(start, end, validated_only=False) -> dict:
    """
    Récapitulatif de TVA pour la déclaration (CA3) : bases HT et TVA collectée par taux, TVA déductible.

    Toute la TVA collectée (4457) est comptée, y compris celle des écritures saisies à la main sans
    taux sur les lignes : le taux est alors celui du compte (paramétrage des taux de TVA). La base HT
    est celle portée par les lignes de TVA (écritures automatiques : biens à la facturation, prestations
    à l'encaissement) ; à défaut, celle des comptes de produits de l'écriture, rattachée à son taux
    quand elle n'en a qu'un. Ce qui reste sans taux figure sur une ligne à part.
    """
    lines = _lines(start, end, validated_only).exclude(transaction__type="vat")  # hors écritures de liquidation
    account_rates = dict(VATRate.objects.filter(collected_account__isnull=False)
                         .values_list("collected_account_id", "rate"))
    rates = {}

    def add(rate, field, amount):
        rates.setdefault(rate, {"rate": rate, "base": ZERO, "vat": ZERO})[field] += amount

    txn_rates, based = {}, set(lines.filter(account__code__startswith="445", vat_base__isnull=False)
                               .values_list("transaction_id", flat=True))
    for r in (lines.filter(account__code__startswith="4457").values("vat_rate", "account_id", "transaction_id")
              .annotate(total_debit=Sum("debit"), total_credit=Sum("credit"),
                        base_credit=Sum("vat_base", filter=Q(credit__gt=0)), base_debit=Sum("vat_base", filter=Q(debit__gt=0)))):
        rate = r["vat_rate"] if r["vat_rate"] is not None else account_rates.get(r["account_id"])
        add(rate, "vat", _d(r["total_credit"]) - _d(r["total_debit"]))
        if r["transaction_id"] in based:
            add(rate, "base", _d(r["base_credit"]) - _d(r["base_debit"]))
        txn_rates.setdefault(r["transaction_id"], set()).add(rate)
    for r in (lines.filter(account__code__startswith="7").exclude(transaction_id__in=based)
              .values("vat_rate", "transaction_id").annotate(debit=Sum("debit"), credit=Sum("credit"))):
        rate = r["vat_rate"]
        if rate is None:
            found = txn_rates.get(r["transaction_id"])
            if not found:
                continue  # produit sans TVA collectée dans l'écriture : hors champ du récapitulatif
            rate = next(iter(found)) if len(found) == 1 else None
        add(rate, "base", _d(r["credit"]) - _d(r["debit"]))
    conf = AccountingSettings.get()
    deductible = (lines.filter(account__code__startswith="4456").exclude(account=conf.vat_credit_account)
                  .aggregate(d=Sum("debit"), c=Sum("credit")))
    deductible = _d(deductible["d"]) - _d(deductible["c"])
    pending = _lines(None, end, validated_only).filter(account__code__startswith="4458").aggregate(d=Sum("debit"), c=Sum("credit"))
    reverse = lines.filter(account__code__startswith="4452").aggregate(d=Sum("debit"), c=Sum("credit"))
    autoliquidated = _d(reverse["c"]) - _d(reverse["d"])
    rows = sorted(rates.values(), key=lambda r: (r["rate"] is None, -(r["rate"] or 0)))
    collected = sum((r["vat"] for r in rows), ZERO)
    return {"start": start, "end": end, "rows": rows, "collected": collected, "deductible": deductible,
            "base": sum((r["base"] for r in rows), ZERO), "autoliquidated": autoliquidated,
            "net": collected + autoliquidated - deductible, "pending": _d(pending["c"]) - _d(pending["d"])}


# Lignes du formulaire 3310-CA3 (TVA brute : ligne du taux ; 2,1 % en métropole via l'annexe 3310 A, ligne 14)
CA3_RATE_LINES = {Decimal("0.2000"): ("08", "Taux normal 20 %"), Decimal("0.1000"): ("9B", "Taux réduit 10 %"),
                  Decimal("0.0550"): ("09", "Taux réduit 5,5 %"), Decimal("0.0210"): ("14", "Taux particulier 2,1 % (annexe 3310 A)")}
CA3_REVERSE_LINES = {"eu_goods": ("03", "Acquisitions intracommunautaires"),
                     "eu_services": ("2A", "Achats de prestations de services intracommunautaires (art. 283-2 du CGI)"),
                     "foreign": ("3B", "Achats auprès d'un assujetti non établi en France (art. 283-1 du CGI)")}


def ca3(start, end, validated_only=False) -> dict:
    """
    Aide à la déclaration 3310-CA3 de la période, ligne par ligne, depuis la comptabilité : opérations
    imposables et non imposables (bases HT), TVA brute par taux (ventes et autoliquidation), TVA
    déductible (immobilisations, autres biens et services, crédit reporté), solde. À contrôler avant
    télédéclaration ; les opérations non imposables restent à ventiler entre les lignes 04 à 06.
    """
    summary = vat_summary(start, end, validated_only)
    lines = _lines(start, end, validated_only).exclude(transaction__type="vat")
    conf = AccountingSettings.get()
    operations, gross = OrderedDict(), OrderedDict()

    def put(table, code, label, base=ZERO, tax=ZERO):
        row = table.setdefault(code, {"code": code, "label": label, "base": ZERO, "tax": ZERO})
        row["base"] += base
        row["tax"] += tax

    put(operations, "01", "Ventes, prestations de services (opérations imposables)",
        sum((r["base"] for r in summary["rows"] if r["rate"]), ZERO))
    for r in summary["rows"]:
        if r["rate"] is not None and not r["rate"]:
            put(operations, "05", "Opérations non imposables (à ventiler entre les lignes 04 à 06)", r["base"])
    reverse_rows = (lines.filter(account__code__startswith="4452")
                    .values("transaction__tags", "vat_rate")
                    .annotate(total_debit=Sum("debit"), total_credit=Sum("credit"),
                              base_credit=Sum("vat_base", filter=Q(credit__gt=0)), base_debit=Sum("vat_base", filter=Q(debit__gt=0))))
    intracom_vat = ZERO
    reverse_by_rate = {}
    for r in reverse_rows:
        scheme = (r["transaction__tags"] or "").partition("autoliquidation:")[2] or "foreign"
        base = _d(r["base_credit"]) - _d(r["base_debit"])
        tax = _d(r["total_credit"]) - _d(r["total_debit"])
        put(operations, *CA3_REVERSE_LINES.get(scheme, CA3_REVERSE_LINES["foreign"]), base)
        if scheme == "eu_goods":
            intracom_vat += tax
        totals = reverse_by_rate.setdefault(r["vat_rate"], [ZERO, ZERO])
        totals[0] += base
        totals[1] += tax
    for r in summary["rows"]:
        if r["rate"]:
            put(gross, *CA3_RATE_LINES.get(r["rate"], ("14", f"Autre taux ({(r['rate'] * 100).normalize()} %)")), r["base"], r["vat"])
        elif r["rate"] is None:
            put(gross, "14", "TVA sans taux identifié (à contrôler)", r["base"], r["vat"])
    for rate, (base, tax) in reverse_by_rate.items():
        put(gross, *CA3_RATE_LINES.get(rate, ("14", "Autre taux")), base, tax)
    total_gross = sum((r["tax"] for r in gross.values()), ZERO)
    deductible = lines.filter(account__code__startswith="4456").exclude(account=conf.vat_credit_account)
    on_assets = deductible.filter(account__code__startswith="44562").aggregate(d=Sum("debit"), c=Sum("credit"))
    others = deductible.exclude(account__code__startswith="44562").aggregate(d=Sum("debit"), c=Sum("credit"))
    line19 = _d(on_assets["d"]) - _d(on_assets["c"])
    line20 = _d(others["d"]) - _d(others["c"])
    carried = ZERO
    if conf.vat_credit_account_id:
        before = _lines(None, start - timedelta(days=1), True).filter(account=conf.vat_credit_account) \
            .aggregate(d=Sum("debit"), c=Sum("credit"))
        carried = _d(before["d"]) - _d(before["c"])
    total_deductible = line19 + line20 + carried
    balance = total_gross - total_deductible
    return {
        "start": start, "end": end, "operations": list(operations.values()), "gross": list(gross.values()),
        "total_gross": total_gross, "intracom_vat": intracom_vat, "line19": line19, "line20": line20, "line22": carried,
        "total_deductible": total_deductible, "due": balance if balance > 0 else ZERO,
        "credit": -balance if balance < 0 else ZERO, "pending": summary["pending"],
    }


# Rubriques du bilan (présentation simplifiée du PCG), dans l'ordre d'affichage
ASSET_SECTIONS = [("fixed", "Actif immobilisé"), ("stocks", "Stocks et en-cours"), ("receivables", "Créances clients"),
                  ("other_receivables", "Autres créances"), ("cash", "Disponibilités"), ("asset_accruals", "Charges constatées d'avance")]
LIABILITY_SECTIONS = [("equity", "Capitaux propres"), ("provisions", "Provisions"), ("financial_debts", "Dettes financières"),
                      ("suppliers", "Dettes fournisseurs"), ("tax_social", "Dettes fiscales et sociales"),
                      ("other_debts", "Autres dettes"), ("liability_accruals", "Produits constatés d'avance")]


def _balance_sheet_section(code: str, balance) -> str:
    """Rubrique d'un compte de bilan selon son numéro et, pour les classes 4 et 5, le sens de son solde."""
    if code[0] == "1":
        return {"15": "provisions", "16": "financial_debts", "17": "financial_debts"}.get(code[:2], "equity")
    if code[0] == "2":
        return "fixed"  # amortissements et dépréciations (28, 29) en moins
    if code[0] == "3":
        return "stocks"
    if code[0] == "5":
        return "cash" if balance >= 0 else "financial_debts"  # solde créditeur : concours bancaires
    if code[:3] == "486":
        return "asset_accruals"
    if code[:3] == "487":
        return "liability_accruals"
    if balance >= 0:
        return "receivables" if code[:2] in ("41", "49") else "other_receivables"
    return {"40": "suppliers", "42": "tax_social", "43": "tax_social", "44": "tax_social"}.get(code[:2], "other_debts")


def balance_sheet(as_of: date, validated_only=False) -> dict:
    """
    Bilan au `as_of` : comptes de bilan (classes 1 à 5) par rubrique, résultat de l'exercice en cours
    (classes 6 et 7) en capitaux propres. Si l'exercice a ses à-nouveaux, les soldes partent de son
    ouverture ; sinon de l'origine, les résultats des exercices précédents non reportés apparaissant à part.
    """
    from .models import FiscalPeriod

    period = (FiscalPeriod.objects.filter(date_start__lte=as_of, date_end__gte=as_of).first()
              or FiscalPeriod.objects.filter(date_start__lte=as_of).order_by("-date_start").first())
    has_opening = period is not None and Transaction.objects.filter(
        journal__kind="opening", type="opening", date__gte=period.date_start, date__lte=as_of).exists()
    start = period.date_start if has_opening else None
    rows = (_lines(start, as_of, validated_only).values("account_id")
            .annotate(debit=Sum("debit"), credit=Sum("credit")))
    accounts = Account.objects.in_bulk([r["account_id"] for r in rows])
    sections = {key: {"key": key, "label": label, "accounts": [], "total": ZERO}
                for key, label in ASSET_SECTIONS + LIABILITY_SECTIONS}
    assets = {key for key, _ in ASSET_SECTIONS}
    result_current, result_prior = ZERO, ZERO
    for r in sorted(rows, key=lambda r: accounts[r["account_id"]].code):
        account = accounts[r["account_id"]]
        balance = _d(r["debit"]) - _d(r["credit"])
        if not balance:
            continue
        if account.pcg_class in "67":
            continue  # résultat calculé ci-dessous
        key = _balance_sheet_section(account.code, balance)
        amount = balance if key in assets else -balance  # actif : solde débiteur ; passif : solde créditeur
        sections[key]["accounts"].append({"account": account, "amount": amount})
        sections[key]["total"] += amount
    results = _lines(start, as_of, validated_only).filter(account__code__regex=r"^(6|7)")
    if period is not None and not has_opening:
        before = results.filter(transaction__date__lt=period.date_start).aggregate(d=Sum("debit"), c=Sum("credit"))
        result_prior = _d(before["c"]) - _d(before["d"])
        results = results.filter(transaction__date__gte=period.date_start)
    current = results.aggregate(d=Sum("debit"), c=Sum("credit"))
    result_current = _d(current["c"]) - _d(current["d"])
    equity = sections["equity"]
    if result_prior:
        equity["accounts"].append({"account": None, "label": "Résultats des exercices antérieurs (non reportés)",
                                   "amount": result_prior})
    equity["accounts"].append({"account": None, "label": "Résultat de l'exercice" + (" (bénéfice)" if result_current >= 0 else " (perte)"),
                               "amount": result_current})
    equity["total"] += result_prior + result_current
    asset_sections = [sections[key] for key, _ in ASSET_SECTIONS if sections[key]["accounts"]]
    liability_sections = [sections[key] for key, _ in LIABILITY_SECTIONS if sections[key]["accounts"]]
    total_assets = sum((sec["total"] for sec in asset_sections), ZERO)
    total_liabilities = sum((sec["total"] for sec in liability_sections), ZERO)
    return {"as_of": as_of, "period": period, "from_opening": has_opening, "assets": asset_sections,
            "liabilities": liability_sections, "total_assets": total_assets, "total_liabilities": total_liabilities,
            "result": result_current, "is_balanced": total_assets == total_liabilities}


def analytic_summary(start, end, validated_only=False) -> dict:
    """Produits (7), charges (6) et résultat par section analytique ; lignes non affectées à part."""
    from .models import AnalyticSection

    rows = (_lines(start, end, validated_only).filter(account__code__regex=r"^(6|7)")
            .values("analytic_id", "account__code").annotate(debit=Sum("debit"), credit=Sum("credit")))
    sections = OrderedDict()
    known = AnalyticSection.objects.in_bulk()
    for r in rows:
        key = r["analytic_id"]
        row = sections.setdefault(key, {"section": known.get(key), "revenues": ZERO, "expenses": ZERO})
        amount = _d(r["credit"]) - _d(r["debit"])
        if r["account__code"].startswith("7"):
            row["revenues"] += amount
        else:
            row["expenses"] -= amount
    result = []
    for key, row in sorted(sections.items(), key=lambda item: (item[0] is None, item[1]["section"].code if item[1]["section"] else "")):
        row["result"] = row["revenues"] - row["expenses"]
        result.append(row)
    return {"rows": result, "revenues": sum((r["revenues"] for r in result), ZERO),
            "expenses": sum((r["expenses"] for r in result), ZERO), "result": sum((r["result"] for r in result), ZERO)}


def treasury_anomalies() -> list[dict]:
    """
    Comptes de trésorerie mouvementés hors de leur journal (journaux de banque mixtes d'avant les
    journaux de trésorerie, ou saisies antérieures) : à-nouveaux et contre-passations exceptés.
    Les écritures validées restent telles quelles ; le rapprochement du journal en tient compte.
    """
    rows = (LedgerEntry.objects.filter(account__treasury_journals__kind="bank")
            .exclude(transaction__journal__kind="opening").exclude(transaction__type="reversal")
            .exclude(transaction__journal=models.F("account__treasury_journals"))
            .values("account__code", "account__treasury_journals__code", "transaction__journal__code")
            .annotate(lines=Count("id")).order_by("account__code", "transaction__journal__code"))
    return [{"account": r["account__code"], "owner": r["account__treasury_journals__code"],
             "journal": r["transaction__journal__code"], "lines": r["lines"]} for r in rows]


def dashboard(today: date | None = None) -> dict:
    today = today or timezone.localdate()
    month_start, year_start = today.replace(day=1), today.replace(month=1, day=1)
    month = profit_and_loss(month_start, today)
    year = profit_and_loss(year_start, today)
    drafts = Transaction.objects.filter(is_validated=False)
    return {
        "today": today,
        "revenue_month": month["total_revenues"], "revenue_year": year["total_revenues"],
        "profit_year": year["profit"],
        "vat_month": vat_summary(month_start, today)["net"],
        "drafts": drafts.count(),
        "treasury_anomalies": treasury_anomalies(),
        "unbalanced": [t for t in drafts.annotate(d=Sum("entries__debit"), c=Sum("entries__credit")) if t.d != t.c][:10],
        "recent": Transaction.objects.select_related("journal").order_by("-created_at")[:8],
    }
