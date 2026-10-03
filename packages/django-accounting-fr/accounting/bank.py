"""
Rapprochement bancaire : relevés importés, pointage avec le journal de banque, état de rapprochement.

Un rapprochement se fait toujours par journal de trésorerie (BQ, ST, CA…) : chacun a un compte de
contrepartie unique (`Journal.account`), qui ne se mouvemente que dans ce journal. Les relevés, opérations
et pointages sont rattachés à ce compte ; `journal.account` et `journal_of(compte)` passent de l'un à l'autre.

- `import_statement(account, contenu, nom)` lit le relevé (CSV, OFX, CAMT.053, CFONB 120) et
  enregistre ses opérations ; une opération déjà importée (relevés qui se chevauchent) est ignorée ;
- `auto_match(account)` pointe chaque opération avec la ligne d'écriture du même montant, à quelques
  jours près, quand il n'y a pas d'ambiguïté (la référence de la pièce citée dans le libellé départage) ;
- `match(lignes, écritures)` pointe à la main, à montant total égal (une remise de chèques, un virement
  groupé…) ; `create_entry(opération, compte)` comptabilise une opération absente de la comptabilité
  (frais bancaires, prélèvement, règlement fournisseur) et la pointe ;
- `state(account, jour)` : solde comptable, opérations non pointées de part et d'autre, solde du relevé.

Le pointage n'altère pas les écritures (elles restent validées et intangibles).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_type, timedelta
from decimal import Decimal
import hashlib
import re

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from . import bank_formats, reconciliation
from .models import ZERO, Account, AccountingSettings, BankLine, BankMatch, BankStatement, Journal, LedgerEntry, to_cents
from .posting import AccountingError, Line, audit, post_entry

WINDOW = 7  # jours d'écart admis entre l'écriture et l'opération du relevé (pointage automatique)


class BankError(AccountingError):
    """Import ou pointage impossible."""


def treasury_journals():
    """Journaux de banque dotés d'un compte de trésorerie : ceux que l'on rapproche."""
    return (Journal.objects.filter(kind="bank", account__isnull=False, account__is_active=True)
            .select_related("account").order_by("code"))


def bank_accounts():
    return Account.objects.filter(treasury_journals__in=treasury_journals()).order_by("code")


def journal_of(account: Account) -> Journal | None:
    return Journal.objects.filter(kind="bank", account=account).first()


def is_bank(account: Account) -> bool:
    return journal_of(account) is not None


def _signed(entry) -> Decimal:
    """Montant d'une ligne d'écriture vu du relevé : débit du 512 = crédit sur le relevé."""
    return entry.debit - entry.credit


def entries(account: Account):
    """Lignes validées du compte de banque, hors à-nouveaux (un solde repris n'est pas une opération du relevé)."""
    return LedgerEntry.objects.filter(account=account, transaction__is_validated=True).exclude(transaction__type="opening")


def book_balance(account: Account, day: date_type) -> Decimal:
    """
    Solde comptable au `day` (écritures validées) : depuis les derniers à-nouveaux du compte s'il y en a,
    pour ne pas compter deux fois les exercices précédents.
    """
    lines = LedgerEntry.objects.filter(account=account, transaction__is_validated=True, transaction__date__lte=day)
    opening = (lines.filter(transaction__type="opening").order_by("-transaction__date")
               .values_list("transaction__date", flat=True).first())
    if opening:
        lines = lines.filter(transaction__date__gte=opening)
    totals = lines.aggregate(debit=Sum("debit"), credit=Sum("credit"))
    return to_cents(totals["debit"]) - to_cents(totals["credit"])


# === Import ===

def _fingerprint(line: bank_formats.Line, occurrence: int) -> str:
    if line.uid:
        key = f"id|{line.uid}"
    else:
        key = f"{line.date:%Y%m%d}|{line.amount}|{bank_formats._norm(line.label)}|{occurrence}"
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass
class ImportResult:
    statement: BankStatement
    created: int
    duplicates: int
    matched: int


@transaction.atomic
def import_statement(account: Account, raw: bytes, filename: str, user=None, mapping=None,
                     closing_balance: Decimal | None = None) -> ImportResult:
    """Importe un relevé ; lève bank_formats.ColumnsError pour un CSV aux colonnes inconnues."""
    if not is_bank(account):
        raise BankError(f"Le compte {account.code} n'est le compte de trésorerie d'aucun journal de banque.")
    digest = hashlib.sha256(raw).hexdigest()
    if BankStatement.objects.filter(account=account, file_sha256=digest).exists():
        raise BankError("Ce fichier a déjà été importé pour ce compte.")
    try:
        parsed = bank_formats.parse(raw, filename, mapping)
    except bank_formats.ColumnsError:
        raise
    except bank_formats.StatementError as exc:
        raise BankError(str(exc)) from exc
    statement = BankStatement.objects.create(
        account=account, filename=filename[:255], file_format=parsed.file_format, file_sha256=digest,
        date_start=parsed.date_start, date_end=parsed.date_end, opening_balance=parsed.opening_balance,
        closing_balance=closing_balance if closing_balance is not None else parsed.closing_balance,
        imported_by=user if getattr(user, "is_authenticated", False) else None)
    seen, created, duplicates = defaultdict(int), 0, 0
    existing = set(BankLine.objects.filter(account=account, date__gte=parsed.date_start, date__lte=parsed.date_end)
                   .values_list("fingerprint", flat=True))
    new = []
    for line in parsed.lines:
        key = (line.date, line.amount, bank_formats._norm(line.label))
        fingerprint = _fingerprint(line, seen[key])
        seen[key] += 1
        if fingerprint in existing:
            duplicates += 1
            continue
        existing.add(fingerprint)
        new.append(BankLine(statement=statement, account=account, date=line.date, value_date=line.value_date,
                            label=line.label[:255], reference=line.reference[:120], amount=line.amount,
                            fingerprint=fingerprint))
    try:
        with transaction.atomic():
            BankLine.objects.bulk_create(new)
    except IntegrityError as exc:  # import concurrent du même relevé
        raise BankError("Opérations déjà importées par ailleurs : réessayez.") from exc
    created = len(new)
    audit(user, "bank_statement_imported", statement, f"{account.code} {filename} : {created} opération(s), "
                                                       f"{duplicates} déjà importée(s)")
    matched = auto_match(account, user) if created else 0
    return ImportResult(statement, created, duplicates, matched)


@transaction.atomic
def delete_statement(statement: BankStatement, user=None):
    if statement.lines.filter(match__isnull=False).exists():
        raise BankError("Des opérations de ce relevé sont pointées : dépointez-les avant de supprimer le relevé.")
    audit(user, "bank_statement_deleted", statement, f"{statement.account.code} {statement.filename}")
    statement.delete()


# === Pointage ===

def _words(text: str) -> set:
    return {w for w in re.split(r"[^0-9A-Za-z]+", (text or "").upper()) if len(w) >= 4}


def _mentions(bank_line, entry) -> bool:
    """La référence ou le numéro de la pièce comptable figure dans le libellé de l'opération."""
    text = f"{bank_line.label} {bank_line.reference}".upper()
    reference = (entry.transaction.reference or "").upper()
    if len(reference) < 4:
        return False
    return reference in text or bool(_words(reference) & _words(text))


def _create_match(account, bank_ids, entry_ids, method, user=None) -> BankMatch:
    match_ = BankMatch.objects.create(account=account, method=method,
                                      created_by=user if getattr(user, "is_authenticated", False) else None)
    BankLine.objects.filter(pk__in=bank_ids).update(match=match_)
    # Seul le lien de pointage change : la ligne d'une écriture validée reste intangible
    LedgerEntry.objects.filter(pk__in=entry_ids).update(bank_match=match_)
    return match_


@transaction.atomic
def auto_match(account: Account, user=None) -> int:
    """Pointe les opérations dont la contrepartie est sans ambiguïté. Renvoie le nombre de pointages."""
    Account.objects.select_for_update().filter(pk=account.pk).first()
    lines = list(BankLine.objects.filter(account=account, match__isnull=True).order_by("date", "pk"))
    if not lines:
        return 0
    start, end = min(l.date for l in lines) - timedelta(days=WINDOW), max(l.date for l in lines) + timedelta(days=WINDOW)
    candidates = list(entries(account).filter(bank_match__isnull=True, transaction__date__gte=start,
                                              transaction__date__lte=end).select_related("transaction"))
    by_amount = defaultdict(list)
    for entry in candidates:
        by_amount[_signed(entry)].append(entry)
    lines_by_amount = defaultdict(list)
    for line in lines:
        lines_by_amount[line.amount].append(line)

    def near(line, entry):
        return abs((entry.transaction.date - line.date).days) <= WINDOW

    pairs, used = [], set()
    for amount, group in lines_by_amount.items():
        for line in group:
            options = [e for e in by_amount.get(amount, []) if e.pk not in used and near(line, e)]
            rivals = [other for other in group if other is not line and other.match_id is None
                      and any(near(other, e) for e in options)]
            if len(options) > 1 or rivals:
                mentioned = [e for e in options if _mentions(line, e)]
                options = mentioned if len(mentioned) == 1 else []
                if options and any(_mentions(other, options[0]) for other in rivals):
                    options = []
            if len(options) == 1:
                pairs.append((line, options[0]))
                used.add(options[0].pk)
                line.match_id = -1  # réservée dans cette passe
    for line, entry in pairs:
        _create_match(account, [line.pk], [entry.pk], "auto", user)
    if pairs:
        audit(user, "bank_auto_matched", account, f"{account.code} : {len(pairs)} pointage(s)")
    return len(pairs)


@transaction.atomic
def match(account: Account, bank_ids, entry_ids, user=None) -> BankMatch:
    """Pointage manuel : opérations et lignes d'écriture du compte, de même montant total."""
    lines = list(BankLine.objects.select_for_update().filter(pk__in=bank_ids, account=account))
    ledger = list(entries(account).select_for_update().filter(pk__in=entry_ids))
    if not lines or not ledger:
        raise BankError("Choisir au moins une opération du relevé et une ligne d'écriture.")
    if len(lines) != len(set(bank_ids)) or len(ledger) != len(set(entry_ids)):
        raise BankError("Opération ou écriture introuvable sur ce compte.")
    if any(l.match_id for l in lines) or any(e.bank_match_id for e in ledger):
        raise BankError("Une des lignes est déjà pointée.")
    bank_total = sum((l.amount for l in lines), ZERO)
    ledger_total = sum((_signed(e) for e in ledger), ZERO)
    if bank_total != ledger_total:
        raise BankError(f"Montants différents : relevé {bank_total} €, comptabilité {ledger_total} €.")
    result = _create_match(account, [l.pk for l in lines], [e.pk for e in ledger], "manual", user)
    audit(user, "bank_matched", account, f"{account.code} : {len(lines)} opération(s), {len(ledger)} ligne(s), {bank_total} €")
    return result


@transaction.atomic
def mark_prior(account: Account, until: date_type, user=None) -> int:
    """
    Point de départ : les écritures non pointées jusqu'au `until` (inclus) sont déjà dans le solde
    initial du premier relevé importé et ne figureront sur aucune opération. Elles sont pointées sans
    opération du relevé. Renvoie le nombre de lignes.
    """
    ids = list(entries(account).filter(bank_match__isnull=True, transaction__date__lte=until).values_list("pk", flat=True))
    if ids:
        _create_match(account, [], ids, "prior", user)
        audit(user, "bank_marked_prior", account, f"{account.code} : {len(ids)} ligne(s) jusqu'au {until}")
    return len(ids)


@transaction.atomic
def unmatch(account: Account, match_id: int, user=None) -> None:
    found = BankMatch.objects.select_for_update().filter(pk=match_id, account=account).first()
    if found is None:
        raise BankError("Pointage introuvable.")
    audit(user, "bank_unmatched", account, f"{account.code} : pointage {found.pk}")
    BankLine.objects.filter(match=found).update(match=None)
    LedgerEntry.objects.filter(bank_match=found).update(bank_match=None)
    found.delete()


@transaction.atomic
def create_entry(line: BankLine, counterpart: Account, label: str = "", auxiliary_code: str = "",
                 auxiliary_label: str = "", user=None):
    """Comptabilise une opération du relevé dans le journal de la banque (validée) et la pointe."""
    line = BankLine.objects.select_for_update().select_related("account").get(pk=line.pk)
    if line.match_id:
        raise BankError("Opération déjà pointée.")
    if counterpart == line.account:
        raise BankError("La contrepartie doit être un autre compte que la banque.")
    label = (label or line.label)[:200]
    tiers = {}
    if auxiliary_code:
        tiers = {"auxiliary_code": auxiliary_code[:40], "auxiliary_label": (auxiliary_label or auxiliary_code)[:120]}
    amount = abs(line.amount)
    if line.amount > 0:
        lines = [Line(line.account, debit=amount, label=label), Line(counterpart, credit=amount, label=label, **tiers)]
    else:
        lines = [Line(counterpart, debit=amount, label=label, **tiers), Line(line.account, credit=amount, label=label)]
    txn = post_entry(journal_of(line.account).code, line.date, label, lines, entry_type="other",
                     reference=(line.reference or f"Relevé {line.date:%d/%m/%Y}")[:128],
                     source_key=f"bankline:{line.pk}", user=user, validate=True,
                     document_type="bank_line", document_id=str(line.pk))
    bank_entry = txn.entries.get(account=line.account)
    _create_match(line.account, [line.pk], [bank_entry.pk], "entry", user)
    audit(user, "bank_entry_created", txn, f"{line.account.code} {line.date} {line.amount} -> {counterpart.code}")
    if tiers and reconciliation.is_reconcilable(counterpart):
        reconciliation.auto_reconcile(counterpart, user)
    return txn


# === État de rapprochement ===

def bank_balance(account: Account, day: date_type) -> Decimal | None:
    """Solde du relevé au `day` : dernier solde final connu, ajusté des opérations postérieures ou antérieures."""
    anchor = (BankStatement.objects.filter(account=account, closing_balance__isnull=False, date_end__lte=day)
              .order_by("-date_end", "-pk").first())
    if anchor:
        after = BankLine.objects.filter(account=account, date__gt=anchor.date_end, date__lte=day).aggregate(s=Sum("amount"))
        return anchor.closing_balance + to_cents(after["s"])
    anchor = (BankStatement.objects.filter(account=account, opening_balance__isnull=False, date_start__lte=day)
              .order_by("date_start", "pk").first())
    if anchor:
        moves = BankLine.objects.filter(account=account, date__gte=anchor.date_start, date__lte=day).aggregate(s=Sum("amount"))
        return anchor.opening_balance + to_cents(moves["s"])
    anchor = (BankStatement.objects.filter(account=account, closing_balance__isnull=False, date_end__gt=day)
              .order_by("date_end", "pk").first())
    if anchor:  # solde final postérieur : on retranche les opérations après `day`
        moves = BankLine.objects.filter(account=account, date__gt=day, date__lte=anchor.date_end).aggregate(s=Sum("amount"))
        return anchor.closing_balance - to_cents(moves["s"])
    return None


def state(account: Account, day: date_type) -> dict:
    """
    État de rapprochement au `day` :
    solde comptable − écritures non pointées + opérations du relevé non comptabilisées = solde du relevé.
    Une ligne pointée avec une contrepartie datée après `day` compte comme non pointée à cette date.
    Les écritures non pointées d'un exercice clôturé restent en attente : elles sont dans les à-nouveaux.
    """
    ledger = list(entries(account).filter(transaction__date__lte=day).select_related("transaction", "bank_match"))
    lines = list(BankLine.objects.filter(account=account, date__lte=day))
    match_ids = {e.bank_match_id for e in ledger if e.bank_match_id} | {l.match_id for l in lines if l.match_id}
    bank_dates = defaultdict(list)
    for line_ in BankLine.objects.filter(match_id__in=match_ids):
        bank_dates[line_.match_id].append(line_.date)
    ledger_dates = defaultdict(list)
    for entry in LedgerEntry.objects.filter(bank_match_id__in=match_ids).select_related("transaction"):
        ledger_dates[entry.bank_match_id].append(entry.transaction.date)

    # Pointée à `day` si sa contrepartie existe à cette date (un pointage sans opération du relevé :
    # écriture antérieure au premier relevé)
    def ledger_open(entry):
        return not entry.bank_match_id or any(d > day for d in bank_dates.get(entry.bank_match_id, []))

    def line_open(line_):
        return not line_.match_id or any(d > day for d in ledger_dates.get(line_.match_id, []))

    unmatched_entries = [e for e in ledger if ledger_open(e)]
    unmatched_lines = [l for l in lines if line_open(l)]
    book = book_balance(account, day)
    pending_entries = sum((_signed(e) for e in unmatched_entries), ZERO)
    pending_lines = sum((l.amount for l in unmatched_lines), ZERO)
    expected = book - pending_entries + pending_lines
    statement_balance = bank_balance(account, day)
    return {
        "account": account, "journal": journal_of(account), "day": day, "book_balance": book,
        "unmatched_entries": sorted(unmatched_entries, key=lambda e: (e.transaction.date, e.pk)),
        "unmatched_lines": unmatched_lines, "pending_entries": pending_entries, "pending_lines": pending_lines,
        "expected_bank_balance": expected, "bank_balance": statement_balance,
        "gap": None if statement_balance is None else statement_balance - expected,
    }


def summary(account: Account) -> dict:
    last = BankStatement.objects.filter(account=account).order_by("-date_end", "-pk").first()
    return {"account": account, "journal": journal_of(account), "last": last,
            "lines": BankLine.objects.filter(account=account, match__isnull=True).count(),
            "entries": entries(account).filter(bank_match__isnull=True).count(),
            "balance": book_balance(account, timezone.localdate())}


def default_counterpart():
    return AccountingSettings.get().fees_account
