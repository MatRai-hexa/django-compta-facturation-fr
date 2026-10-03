"""
Liquidation de la TVA d'une période : l'écriture de déclaration (journal OD, validée).

Elle solde les comptes de TVA collectée (4457…), autoliquidée (4452…) et déductible (4456…) à la fin de la période,
impute le crédit de TVA reporté de la déclaration précédente (44567), puis porte le solde :

- en TVA à décaisser (44551) si la TVA collectée l'emporte ;
- en crédit de TVA à reporter (44567) sinon.

Le paiement de la TVA (44551 / 512) se saisit ensuite dans le journal de banque. Les soldes sont
ceux des écritures validées jusqu'à la fin de la période : une liquidation tient compte de tout
ce qui précède, régularisations comprises.
"""
from __future__ import annotations

from datetime import date

from django.db import transaction
from django.db.models import Sum

from .models import ZERO, Account, AccountingSettings, LedgerEntry, Transaction, to_cents
from .posting import AccountingError, Line, post_entry

COLLECTED, DEDUCTIBLE = ("4457", "4452"), "4456"  # TVA collectée et TVA autoliquidée ; TVA déductible


def _key(start: date, end: date) -> str:
    return f"vat:{start.isoformat()}:{end.isoformat()}"


def _account(conf, field, code):
    account = getattr(conf, field) or Account.objects.filter(code=code).first()
    if account is None:
        raise AccountingError(f"Compte {code} absent du plan comptable (Paramètres comptables).")
    return account


def _balances(prefix: str, end: date, exclude=()):
    rows = (LedgerEntry.objects.filter(account__code__startswith=prefix, transaction__is_validated=True,
                                       transaction__date__lte=end)
            .exclude(account__in=exclude)
            .values("account_id").annotate(debit=Sum("debit"), credit=Sum("credit")))
    accounts = Account.objects.in_bulk([r["account_id"] for r in rows])
    return [{"account": accounts[r["account_id"]], "balance": to_cents(r["debit"]) - to_cents(r["credit"])}
            for r in sorted(rows, key=lambda r: accounts[r["account_id"]].code)]


def preview(start: date, end: date) -> dict:
    """Ce que passerait la liquidation de la période : TVA collectée, déductible, crédit reporté, solde."""
    conf = AccountingSettings.get()
    credit_account = _account(conf, "vat_credit_account", "445671")
    payable_account = _account(conf, "vat_payable_account", "445510")
    collected = [{**r, "amount": -r["balance"]} for prefix in COLLECTED for r in _balances(prefix, end) if r["balance"]]
    deductible = [{**r, "amount": r["balance"]} for r in _balances(DEDUCTIBLE, end, exclude=[credit_account]) if r["balance"]]
    carried = next((r["balance"] for r in _balances(credit_account.code, end) if r["account"] == credit_account), ZERO)
    total_collected = sum((r["amount"] for r in collected), ZERO)
    total_deductible = sum((r["amount"] for r in deductible), ZERO)
    due = total_collected - total_deductible - carried
    drafts = (Transaction.objects.filter(is_validated=False, date__lte=end)
              .filter(entries__account__code__regex=r"^445(2|6|7)").distinct().count())
    return {
        "start": start, "end": end, "collected": collected, "deductible": deductible,
        "total_collected": total_collected, "total_deductible": total_deductible, "carried_credit": carried,
        "due": due if due > 0 else ZERO, "credit": -due if due < 0 else ZERO,
        "payable_account": payable_account, "credit_account": credit_account, "drafts": drafts,
        "settlement": Transaction.objects.filter(source_key=_key(start, end)).first(),
        "later": Transaction.objects.filter(type="vat", date__gt=end).order_by("date").first(),
    }


@transaction.atomic
def settle(start: date, end: date, user=None) -> Transaction:
    """Passe et valide l'écriture de liquidation de la TVA de la période. Idempotent par période."""
    data = preview(start, end)
    if data["settlement"]:
        return data["settlement"]
    if data["later"]:
        raise AccountingError(f"Une liquidation postérieure existe déjà ({data['later'].number}) : "
                              "régulariser sur la prochaine déclaration.")
    if data["drafts"]:
        raise AccountingError(f"{data['drafts']} brouillon(s) mouvementent la TVA jusqu'au {end:%d/%m/%Y} : "
                              "à valider ou supprimer avant la liquidation.")
    label = f"Liquidation de la TVA du {start:%d/%m/%Y} au {end:%d/%m/%Y}"
    lines = []
    for row in data["collected"]:  # solde créditeur -> débit (et inversement pour une régularisation)
        amount = row["amount"]
        lines.append(Line(row["account"], debit=amount if amount > 0 else ZERO, credit=-amount if amount < 0 else ZERO,
                          label=f"TVA {'autoliquidée' if row['account'].code.startswith('4452') else 'collectée'} {row['account'].code}"))
    for row in data["deductible"]:
        amount = row["amount"]
        lines.append(Line(row["account"], credit=amount if amount > 0 else ZERO, debit=-amount if amount < 0 else ZERO,
                          label=f"TVA déductible {row['account'].code}"))
    # Le crédit reporté est imputé, puis remplacé par le nouveau crédit éventuel : une seule ligne nette
    credit_move = data["credit"] - data["carried_credit"]
    if credit_move:
        lines.append(Line(data["credit_account"], debit=credit_move if credit_move > 0 else ZERO,
                          credit=-credit_move if credit_move < 0 else ZERO, label="Crédit de TVA à reporter"))
    if data["due"]:
        lines.append(Line(data["payable_account"], credit=data["due"], label="TVA à décaisser"))
    if not lines:
        raise AccountingError("Aucune TVA à liquider sur la période.")
    return post_entry("OD", end, label, lines, entry_type="vat", reference=f"TVA {start:%m/%Y}",
                      source_key=_key(start, end), user=user, validate=True)
