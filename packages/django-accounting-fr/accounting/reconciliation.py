"""
Lettrage des comptes de tiers (clients, fournisseurs) : rapprocher les factures de leurs règlements.

Un lettrage regroupe des lignes validées d'un même compte et d'un même compte auxiliaire dont les
débits égalent les crédits. Elles reçoivent un code propre au compte (A, B… Z, AA, AB…) et la date
du lettrage, repris dans les zones EcritureLet et DateLet du FEC.

Seules les lignes des exercices ouverts se lettrent : un exercice clôturé ne change plus, ses soldes
de tiers sont repris par les à-nouveaux (un par compte auxiliaire), qui se lettrent à leur tour.

- `auto_reconcile()` lettre ce qui se rapproche sans ambiguïté (même pièce, même référence,
  montant unique, solde nul du tiers) ;
- `reconcile(ids)` lettre les lignes choisies, `write_off(ids)` solde d'abord un petit écart
  (règlement arrondi, frais retenus) par une écriture d'OD ;
- `unreconcile(account, code)` défait un lettrage.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date as date_type
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Max, Min, Q, Sum
from django.utils import timezone

from . import conf
from .models import ZERO, Account, LedgerEntry, to_cents
from .posting import AccountingError, Line, audit, post_entry

GAP_LIMIT = Decimal("5.00")  # écart maximal soldé en OD lors d'un lettrage manuel
GAP_ACCOUNTS = {"loss": ("658000", "Charges diverses de gestion courante", "expense"),
                "gain": ("758000", "Produits divers de gestion courante", "revenue"),
                "exchange_loss": ("666000", "Pertes de change financières", "expense"),
                "exchange_gain": ("766000", "Gains de change financiers", "revenue")}
AGE_BUCKETS = (30, 60, 90)


class ReconciliationError(AccountingError):
    """Lettrage impossible (lignes déséquilibrées, de tiers différents, d'un exercice clôturé…)."""


# === Codes de lettrage : A … Z, AA … ZZ, AAA … ===

def code_to_int(code: str) -> int:
    number = 0
    for char in code:
        number = number * 26 + (ord(char) - 64)
    return number


def int_to_code(number: int) -> str:
    code = ""
    while number:
        number, rest = divmod(number - 1, 26)
        code = chr(65 + rest) + code
    return code


def _is_code(ref: str) -> bool:
    return ref.isascii() and ref.isalpha() and ref.isupper()


def _codes(account: Account):
    """Codes libres du compte, à la suite du plus grand déjà attribué."""
    used = (LedgerEntry.objects.filter(account=account).exclude(reconciliation_ref="")
            .values_list("reconciliation_ref", flat=True).distinct())
    number = max((code_to_int(ref) for ref in used if _is_code(ref)), default=0)
    while True:
        number += 1
        yield int_to_code(number)


# === Comptes et lignes lettrables ===

def reconcilable_accounts():
    """Comptes de tiers : préfixes du réglage ACCOUNTING["RECONCILABLE_PREFIXES"] (40 et 41 par défaut)."""
    condition = Q()
    for prefix in conf.get("RECONCILABLE_PREFIXES"):
        condition |= Q(code__startswith=prefix)
    return Account.objects.filter(condition) if condition else Account.objects.none()


def is_reconcilable(account: Account) -> bool:
    return any(account.code.startswith(prefix) for prefix in conf.get("RECONCILABLE_PREFIXES"))


def is_payable(account: Account) -> bool:
    """Compte fournisseur (40x) : son solde se lit au crédit."""
    return account.code.startswith("40")


def lines(account: Account, auxiliary: str | None = None):
    """Lignes lettrables d'un compte : validées, d'un exercice ouvert."""
    qs = LedgerEntry.objects.filter(account=account, transaction__is_validated=True,
                                    transaction__fiscal_period__is_closed=False)
    if auxiliary is not None:
        qs = qs.filter(auxiliary_code=auxiliary)
    return qs


def open_lines(account: Account, auxiliary: str | None = None):
    return lines(account, auxiliary).filter(reconciliation_ref="")


# === Lettrage ===

def _check(entries: list[LedgerEntry], minimum=2):
    if not entries or len(entries) < minimum:
        raise ReconciliationError("Choisir au moins deux lignes à lettrer.")
    first = entries[0]
    if any((e.account_id, e.auxiliary_code) != (first.account_id, first.auxiliary_code) for e in entries):
        raise ReconciliationError("Les lignes lettrées ensemble doivent être du même compte et du même tiers.")
    for e in entries:
        if not is_reconcilable(e.account):
            raise ReconciliationError(f"Le compte {e.account.code} n'est pas un compte de tiers lettrable.")
        if not e.transaction.is_validated:
            raise ReconciliationError("Seules les écritures validées se lettrent.")
        if e.transaction.fiscal_period.is_closed:
            raise ReconciliationError(f"L'exercice {e.transaction.fiscal_period.name} est clôturé : "
                                      "lettrer ses à-nouveaux dans l'exercice suivant.")
        if e.reconciliation_ref:
            raise ReconciliationError(f"Une ligne est déjà lettrée ({e.reconciliation_ref}).")


def _gap(entries) -> Decimal:
    """Débits moins crédits."""
    return sum((e.debit - e.credit for e in entries), ZERO)


def _locked(ids) -> list[LedgerEntry]:
    entries = list(LedgerEntry.objects.select_for_update()
                   .filter(pk__in=ids).select_related("account", "transaction", "transaction__fiscal_period"))
    if len(entries) != len(set(ids)):
        raise ReconciliationError("Ligne d'écriture introuvable.")
    return entries


def _apply(account: Account, groups: list[list[int]], day: date_type, user=None) -> list[str]:
    """Attribue un code à chaque groupe (le compte est verrouillé : codes uniques)."""
    Account.objects.select_for_update().filter(pk=account.pk).first()
    codes, applied = _codes(account), []
    for ids in groups:
        code = next(codes)
        # Les lignes d'écritures validées sont verrouillées : seuls les champs du lettrage changent
        LedgerEntry.objects.filter(pk__in=ids).update(reconciliation_ref=code, is_reconciled=True, reconciled_at=day)
        applied.append(code)
    return applied


@transaction.atomic
def reconcile(ids, user=None, day: date_type | None = None) -> str:
    """Lettre les lignes `ids` (débits = crédits). Renvoie le code attribué."""
    entries = _locked(ids)
    _check(entries)
    gap = _gap(entries)
    if gap:
        raise ReconciliationError(f"Lignes déséquilibrées : écart de {abs(gap)} € "
                                  f"({'débit' if gap > 0 else 'crédit'} en trop).")
    account = entries[0].account
    code = _apply(account, [[e.pk for e in entries]], day or timezone.localdate(), user)[0]
    audit(user, "reconciled", account, f"{account.code} {entries[0].auxiliary_code} : {code} ({len(entries)} lignes)")
    return code


def _gap_account(kind):
    code, name, nature = GAP_ACCOUNTS[kind]
    account, _ = Account.objects.get_or_create(code=code, defaults={"name": name, "account_type": nature})
    return account


@transaction.atomic
def write_off(ids, user=None, day: date_type | None = None, limit: Decimal = GAP_LIMIT, exchange: bool = False) -> str:
    """
    Solde l'écart des lignes `ids` par une écriture d'OD (charge 658 ou produit 758), puis les lettre
    avec la ligne d'écart. Pour un règlement arrondi ou amputé de petits frais. `exchange` : écart de
    change d'une créance ou d'une dette en devise (perte 666, gain 766), sans plafond.
    """
    entries = _locked(ids)
    _check(entries, minimum=1)
    gap = _gap(entries)
    if not gap:
        return reconcile(ids, user, day)
    if not exchange and abs(gap) > limit:
        raise ReconciliationError(f"Écart de {abs(gap)} € : au-delà de {limit} €, passer une écriture.")
    day = day or timezone.localdate()
    first = entries[0]
    tiers = {"auxiliary_code": first.auxiliary_code, "auxiliary_label": first.auxiliary_label}
    if exchange and not any(e.currency for e in entries):
        raise ReconciliationError("Écart de change : au moins une des lignes doit être en devise.")
    kind = "Écart de change" if exchange else "Écart de règlement"
    label = f"{kind} {first.auxiliary_label or first.auxiliary_code or first.account.name}"[:200]
    loss, gain = ("exchange_loss", "exchange_gain") if exchange else ("loss", "gain")
    if gap > 0:  # reste dû par le tiers (client) ou trop payé au fournisseur : charge
        lines_ = [Line(_gap_account(loss), debit=gap, label=label), Line(first.account, credit=gap, label=label, **tiers)]
    else:
        lines_ = [Line(first.account, debit=-gap, label=label, **tiers), Line(_gap_account(gain), credit=-gap, label=label)]
    entry = post_entry("OD", day, label, lines_, entry_type="other", reference=f"Lettrage {first.account.code}",
                       user=user, validate=True)
    line = entry.entries.get(account=first.account)
    return reconcile([e.pk for e in entries] + [line.pk], user, day)


@transaction.atomic
def unreconcile(account: Account, code: str, user=None) -> int:
    """Défait le lettrage `code` du compte. Renvoie le nombre de lignes délettrées."""
    entries = LedgerEntry.objects.select_for_update().filter(account=account, reconciliation_ref=code)
    if not entries.exists():
        raise ReconciliationError(f"Lettrage {code} introuvable sur le compte {account.code}.")
    if entries.filter(transaction__fiscal_period__is_closed=True).exists():
        raise ReconciliationError(f"Le lettrage {code} porte sur un exercice clôturé : il ne peut plus être défait.")
    count = entries.update(reconciliation_ref="", is_reconciled=False, reconciled_at=None)
    audit(user, "unreconciled", account, f"{account.code} : {code} ({count} lignes)")
    return count


# === Lettrage automatique ===

def _amount(e) -> Decimal:
    return e["debit"] - e["credit"]


def _pairs(items, unique=False):
    """Paires débit / crédit de même montant (dans l'ordre chronologique). `unique` : montant sans ambiguïté."""
    debits, credits = defaultdict(list), defaultdict(list)
    for e in items:
        (debits if e["debit"] else credits)[abs(_amount(e))].append(e)
    pairs = []
    for amount, side in debits.items():
        other = credits.get(amount, [])
        if unique and (len(side) != 1 or len(other) != 1):
            continue
        pairs.extend([a, b] for a, b in zip(side, other))
    return pairs


def _match(items) -> list[list[dict]]:
    """Groupes équilibrés d'un tiers, des plus sûrs aux plus larges."""
    groups, left = [], list(items)

    def take(group):
        groups.append(group)
        used = {e["pk"] for e in group}
        left[:] = [e for e in left if e["pk"] not in used]

    for key in ("document", "reference"):  # même pièce d'origine, puis même référence
        buckets = defaultdict(list)
        for e in left:
            if e[key]:
                buckets[e[key]].append(e)
        for bucket in buckets.values():
            if len(bucket) < 2:
                continue
            if not sum((_amount(e) for e in bucket), ZERO):
                take(bucket)
            else:
                for pair in _pairs(bucket):
                    take(pair)
    for pair in _pairs(left, unique=True):  # même montant, sans ambiguïté
        take(pair)
    if len(left) > 1 and not sum((_amount(e) for e in left), ZERO):  # le reste du tiers est soldé
        take(list(left))
    return groups


@transaction.atomic
def auto_reconcile(account: Account | None = None, user=None, day: date_type | None = None) -> int:
    """Lettre automatiquement les comptes de tiers (ou `account`). Renvoie le nombre de lettrages."""
    day = day or timezone.localdate()
    total = 0
    for acc in ([account] if account else reconcilable_accounts()):
        rows = (open_lines(acc).order_by("transaction__date", "pk")
                .values("pk", "auxiliary_code", "debit", "credit", "transaction__document_type",
                        "transaction__document_id", "transaction__reference"))
        by_tiers = defaultdict(list)
        for r in rows:
            document = (f"{r['transaction__document_type']}:{r['transaction__document_id']}"
                        if r["transaction__document_type"] else "")
            by_tiers[r["auxiliary_code"]].append({"pk": r["pk"], "debit": r["debit"], "credit": r["credit"],
                                                  "document": document, "reference": r["transaction__reference"]})
        groups = [[e["pk"] for e in group] for items in by_tiers.values() for group in _match(items)]
        if groups:
            _apply(acc, groups, day, user)
            audit(user, "auto_reconciled", acc, f"{acc.code} : {len(groups)} lettrage(s)")
            total += len(groups)
    return total


# === États : tiers ouverts, balance âgée ===

def tiers(account: Account, search: str = ""):
    """Comptes auxiliaires du compte avec leurs lignes non lettrées (exercices ouverts)."""
    qs = open_lines(account)
    if search:
        qs = qs.filter(Q(auxiliary_code__icontains=search) | Q(auxiliary_label__icontains=search))
    rows = (qs.values("auxiliary_code")
            .annotate(debit=Sum("debit"), credit=Sum("credit"), count=Count("pk"), label=Max("auxiliary_label"),
                      oldest=Min("transaction__date"))
            .order_by("auxiliary_code"))
    sign = -1 if is_payable(account) else 1
    result = []
    for r in rows:
        debit, credit = to_cents(r["debit"]), to_cents(r["credit"])
        result.append({"code": r["auxiliary_code"], "label": r["label"] or r["auxiliary_code"], "debit": debit,
                       "credit": credit, "balance": sign * (debit - credit), "count": r["count"], "oldest": r["oldest"]})
    return result


def aged_balance(account: Account, as_of: date_type | None = None, buckets=AGE_BUCKETS) -> dict:
    """
    Balance âgée : soldes non lettrés par tiers, répartis selon l'ancienneté des pièces à la date
    `as_of` (lignes datées jusqu'à `as_of` et non lettrées à cette date).
    Montants positifs : dus par le client (compte 41) ou au fournisseur (compte 40).
    """
    as_of = as_of or timezone.localdate()
    labels = [f"0 à {buckets[0]} j"] + [f"{a + 1} à {b} j" for a, b in zip(buckets, buckets[1:])] + [f"plus de {buckets[-1]} j"]
    sign = -1 if is_payable(account) else 1
    qs = (lines(account).filter(transaction__date__lte=as_of)
          .filter(Q(reconciliation_ref="") | Q(reconciled_at__gt=as_of))
          .values_list("auxiliary_code", "auxiliary_label", "transaction__date", "debit", "credit"))
    rows = {}
    for code, label, day, debit, credit in qs:
        row = rows.setdefault(code, {"code": code, "label": label or code, "buckets": [ZERO] * len(labels), "total": ZERO})
        age = (as_of - day).days
        index = next((i for i, limit in enumerate(buckets) if age <= limit), len(buckets))
        amount = sign * (debit - credit)
        row["buckets"][index] += amount
        row["total"] += amount
        if label and row["label"] == code:
            row["label"] = label
    result = sorted((r for r in rows.values() if r["total"] or any(r["buckets"])), key=lambda r: (-r["total"], r["code"]))
    totals = [sum((r["buckets"][i] for r in result), ZERO) for i in range(len(labels))]
    return {"account": account, "as_of": as_of, "labels": labels, "rows": result, "totals": totals,
            "total": sum(totals, ZERO)}
