"""
Passation des écritures : point d'entrée unique pour écrire en comptabilité.

- `post_entry` crée une écriture équilibrée (ou lève AccountingError) ;
- `validate_entry` la numérote et la verrouille ;
- `reverse_entry` corrige une écriture validée par contre-passation ;
- `close_period` / `generate_opening_entries` clôturent un exercice.

Les pièces métier (ventes, encaissements, avoirs) passent par `accounting.api`.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_type
from decimal import ROUND_HALF_UP, Decimal
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    ZERO, Account, AccountingSettings, AuditLog, EntrySequence, FiscalPeriod, Journal, LedgerEntry, Transaction,
)

from .seal import anchor, seal_period, seal_transaction

logger = logging.getLogger(__name__)
CENT = Decimal("0.01")


class AccountingError(Exception):
    """Écriture impossible (déséquilibrée, exercice clôturé, paramétrage manquant...)."""


@dataclass
class Line:
    account: Account
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    label: str = ""
    vat_rate: Decimal | None = None
    auxiliary_code: str = ""
    auxiliary_label: str = ""
    vat_base: Decimal | None = None  # ligne de TVA : base HT correspondante
    currency: str = ""                 # opération en devise : code ISO 4217…
    currency_amount: Decimal | None = None  # … et montant d'origine (positif)
    analytic: object = None            # section analytique (AnalyticSection), lignes de charges et de produits


def q(amount) -> Decimal:
    return Decimal(amount).quantize(CENT, rounding=ROUND_HALF_UP)


def percent(rate) -> str:
    """0.0550 -> "5,5" ; 0.2000 -> "20"."""
    return f"{(Decimal(rate) * 100).normalize():f}".replace(".", ",")


def audit(user, action, obj=None, details=""):
    AuditLog.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None, action=action,
        model_name=obj.__class__.__name__ if obj is not None else "", object_id=getattr(obj, "pk", None),
        details=details[:2000],
    )


# === Exercices ===

def get_period(day: date_type, create: bool = True) -> FiscalPeriod:
    """Exercice ouvert contenant `day` ; crée l'exercice civil s'il n'existe pas."""
    period = FiscalPeriod.objects.filter(date_start__lte=day, date_end__gte=day).first()
    if period is None:
        if not create:
            raise AccountingError(f"Aucun exercice ne couvre le {day:%d/%m/%Y}.")
        start, end = date_type(day.year, 1, 1), date_type(day.year, 12, 31)
        if FiscalPeriod.objects.filter(date_start__lte=end, date_end__gte=start).exists():
            raise AccountingError(f"Aucun exercice ne couvre le {day:%d/%m/%Y} (exercices non civils : à créer).")
        period = FiscalPeriod.objects.create(name=str(day.year), date_start=start, date_end=end)
    if period.is_closed:
        raise AccountingError(f"L'exercice {period.name} est clôturé : aucune écriture au {day:%d/%m/%Y}.")
    return period


# === Écritures ===

def check_treasury(journal: Journal, lines: list[Line]) -> None:
    """
    Un compte de trésorerie ne se mouvemente que dans son journal (hors à-nouveaux), et toute écriture
    d'un journal de banque a pour contrepartie le compte de ce journal : c'est ce qui permet de
    rapprocher le journal avec les relevés.
    """
    owners = dict(Journal.objects.filter(kind="bank", account__isnull=False).values_list("account_id", "code"))
    if journal.kind == "bank":
        if journal.account_id is None:
            raise AccountingError(f"Le journal {journal.code} n'a pas de compte de trésorerie (Comptabilité › Journaux).")
        if not any(line.account.pk == journal.account_id for line in lines):
            raise AccountingError(f"Une écriture du journal {journal.code} mouvemente son compte de trésorerie "
                                  f"{journal.account.code}.")
    elif journal.kind == "opening":
        return
    for line in lines:
        owner = owners.get(line.account.pk)
        if owner and owner != journal.code:
            raise AccountingError(f"Le compte {line.account.code} se mouvemente dans le journal {owner}, pas dans "
                                  f"{journal.code} (virement entre trésoreries : passer par le compte 580).")


def with_treasury_counterpart(journal: Journal, lines: list[Line], label: str = "") -> list[Line]:
    """Saisie dans un journal de banque : ajoute la contrepartie sur son compte de trésorerie pour équilibrer."""
    if not journal.is_treasury:
        return lines
    gap = sum((q(line.debit) - q(line.credit) for line in lines), ZERO)
    if not gap:
        return lines
    counterpart = Line(journal.account, debit=-gap if gap < 0 else ZERO, credit=gap if gap > 0 else ZERO, label=label)
    return [*lines, counterpart]


@transaction.atomic
def post_entry(journal_code: str, day: date_type, description: str, lines: list[Line], entry_type: str = "other",
               reference: str = "", piece_date=None, source_key: str | None = None, user=None,
               validate: bool = False, document_type: str = "", document_id: str = "", tags: str = "") -> Transaction:
    """Crée une écriture équilibrée. Si `source_key` existe déjà, renvoie l'écriture existante."""
    if source_key:
        existing = Transaction.objects.filter(source_key=source_key).first()
        if existing:
            return existing

    lines = [line for line in lines if q(line.debit) or q(line.credit)]
    for line in lines:
        line.currency = (line.currency or "").strip().upper()
        if bool(line.currency) != (line.currency_amount is not None) or (line.currency and len(line.currency) != 3):
            raise AccountingError("Une ligne en devise porte un code ISO à 3 lettres et son montant d'origine.")
        if line.currency_amount is not None:
            line.currency_amount = abs(q(line.currency_amount))
        line.debit, line.credit = q(line.debit), q(line.credit)
        if line.debit < 0 or line.credit < 0 or (line.debit > 0 and line.credit > 0):
            raise AccountingError("Chaque ligne porte un montant positif, au débit ou au crédit.")
    total_debit = sum((line.debit for line in lines), ZERO)
    total_credit = sum((line.credit for line in lines), ZERO)
    if not lines or total_debit != total_credit:
        raise AccountingError(f"Écriture déséquilibrée : débit {total_debit} ≠ crédit {total_credit}.")

    journal = Journal.objects.filter(code=journal_code).first()
    if journal is None:
        raise AccountingError(f"Journal {journal_code} inexistant.")
    if entry_type != "reversal":  # une contre-passation reprend l'écriture d'origine telle quelle
        check_treasury(journal, lines)
    period = get_period(day)
    try:
        with transaction.atomic():
            txn = Transaction.objects.create(
                journal=journal, type=entry_type, date=day, reference=reference[:128], piece_date=piece_date or day,
                description=description[:255], amount=total_debit, fiscal_period=period,
                document_type=document_type, document_id=str(document_id or ""), tags=tags[:255],
                source_key=source_key, created_by=user if getattr(user, "is_authenticated", False) else None,
            )
    except IntegrityError:
        if source_key:  # créée en parallèle par un autre processus
            return Transaction.objects.get(source_key=source_key)
        raise
    LedgerEntry.objects.bulk_create([
        LedgerEntry(transaction=txn, account=line.account, debit=line.debit, credit=line.credit,
                    label=(line.label or description)[:200], vat_rate=line.vat_rate, vat_base=line.vat_base,
                    currency=line.currency, currency_amount=line.currency_amount, analytic=line.analytic,
                    auxiliary_code=line.auxiliary_code[:40], auxiliary_label=line.auxiliary_label[:120])
        for line in lines
    ])
    if validate:
        txn = validate_entry(txn, user)
    return txn


@transaction.atomic
def validate_entry(txn: Transaction, user=None) -> Transaction:
    """Numérote et verrouille une écriture. Idempotent."""
    txn = Transaction.objects.select_for_update().get(pk=txn.pk)
    if txn.is_validated:
        return txn
    if not txn.is_balanced():
        raise AccountingError("Écriture déséquilibrée : validation impossible.")
    if txn.type != "reversal":  # un brouillon saisi hors de post_entry (admin, import) passe aussi le contrôle
        check_treasury(txn.journal, [Line(e.account, debit=e.debit, credit=e.credit)
                                     for e in txn.entries.select_related("account")])
    period = FiscalPeriod.objects.get(pk=txn.fiscal_period_id)
    if period.is_closed:
        raise AccountingError(f"L'exercice {period.name} est clôturé.")
    if not period.contains(txn.date):
        raise AccountingError("La date de l'écriture est hors de son exercice.")

    sequence, _ = EntrySequence.objects.get_or_create(journal_id=txn.journal_id, fiscal_period=period)
    sequence = EntrySequence.objects.select_for_update().get(pk=sequence.pk)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number"])

    Transaction.objects.filter(pk=txn.pk).update(
        number=f"{txn.journal.code}{period.name}-{sequence.last_number:05d}", is_validated=True,
        validated_at=timezone.now(), validated_by=user if getattr(user, "is_authenticated", False) else None)
    seal_transaction(txn)
    txn.refresh_from_db()
    audit(user, "entry_validated", txn, txn.number)
    return txn


@transaction.atomic
def reverse_entry(txn: Transaction, day: date_type | None = None, user=None) -> Transaction:
    """Contre-passation d'une écriture validée (lignes inversées, validée immédiatement)."""
    if not txn.is_validated:
        raise AccountingError("Une écriture en brouillon se modifie ou se supprime : pas de contre-passation.")
    lines = [Line(e.account, debit=e.credit, credit=e.debit, label=f"Annulation {e.label}"[:200], vat_rate=e.vat_rate,
                  vat_base=e.vat_base, currency=e.currency, currency_amount=e.currency_amount, analytic=e.analytic,
                  auxiliary_code=e.auxiliary_code, auxiliary_label=e.auxiliary_label) for e in txn.entries.all()]
    reversal = post_entry(
        txn.journal.code, day or timezone.localdate(), f"Contre-passation de {txn.number}", lines,
        entry_type="reversal", reference=txn.reference, source_key=f"reversal:{txn.pk}",
        user=user, validate=True, document_type=txn.document_type, document_id=txn.document_id)
    if reversal.reversal_of_id is None:
        Transaction.objects.filter(pk=reversal.pk).update(reversal_of=txn)
        reversal.reversal_of = txn
    audit(user, "entry_reversed", txn, reversal.number)
    return reversal


def delete_draft(txn: Transaction, user=None):
    if txn.is_validated:
        raise AccountingError("Écriture validée : suppression interdite.")
    audit(user, "draft_deleted", txn, f"{txn.description} ({txn.amount} €)")
    txn.delete()


# === Clôture et à-nouveaux ===

@transaction.atomic
def close_period(period: FiscalPeriod, user=None) -> FiscalPeriod:
    period = FiscalPeriod.objects.select_for_update().get(pk=period.pk)
    if period.is_closed:
        raise AccountingError("Exercice déjà clôturé.")
    drafts = Transaction.objects.filter(fiscal_period=period, is_validated=False).count()
    if drafts:
        raise AccountingError(f"{drafts} écriture(s) en brouillon : à valider ou supprimer avant la clôture.")
    period.is_closed = True
    period.closed_at = timezone.now()
    period.closed_by = user if getattr(user, "is_authenticated", False) else None
    period.save(update_fields=["is_closed", "closed_at", "closed_by"])
    seal_period(period)
    period.refresh_from_db()
    transaction.on_commit(lambda: anchor(f"clôture de l'exercice {period.name}", force=True))
    audit(user, "period_closed", period, f"{period.name} · sceau {period.closing_seal}")
    return period


@transaction.atomic
def generate_opening_entries(closed: FiscalPeriod, new_period: FiscalPeriod, user=None) -> Transaction | None:
    """
    Reprend en à-nouveaux (journal AN) les soldes des comptes de bilan (classes 1 à 5)
    de l'exercice clôturé ; le résultat (classes 6 et 7) est porté en 120 ou 129.
    """
    closed.refresh_from_db()
    if not closed.is_closed:
        raise AccountingError("L'exercice doit être clôturé avant de générer les à-nouveaux.")
    conf = AccountingSettings.get()
    # Soldes par compte et, pour les comptes de tiers, par compte auxiliaire (client, fournisseur)
    balances = defaultdict(lambda: ZERO)
    labels = {}
    # Mouvements de l'exercice clôturé uniquement (ses propres à-nouveaux inclus)
    entries = LedgerEntry.objects.filter(transaction__fiscal_period=closed, transaction__is_validated=True)
    for e in entries.select_related("account"):
        balances[(e.account, e.auxiliary_code)] += e.debit - e.credit
        if e.auxiliary_code:
            labels[e.auxiliary_code] = e.auxiliary_label

    lines, result = [], ZERO
    for (account, auxiliary), balance in sorted(balances.items(), key=lambda item: (item[0][0].code, item[0][1])):
        if not balance:
            continue
        if account.pcg_class in "67":
            result += balance  # débiteur = perte
        else:
            label = f"À-nouveau {labels.get(auxiliary) or account.name}"[:200]
            lines.append(Line(account, debit=balance if balance > 0 else ZERO, credit=-balance if balance < 0 else ZERO,
                              label=label, auxiliary_code=auxiliary, auxiliary_label=labels.get(auxiliary, "")))
    if result > 0:
        lines.append(Line(conf.loss_account, debit=result, label=f"Résultat {closed.name} (perte)"))
    elif result < 0:
        lines.append(Line(conf.profit_account, credit=-result, label=f"Résultat {closed.name} (bénéfice)"))
    if not lines:
        return None
    entry = post_entry("AN", new_period.date_start, f"À-nouveaux de l'exercice {closed.name}", lines,
                       entry_type="opening", source_key=f"opening:{new_period.pk}", user=user, validate=True)
    audit(user, "opening_generated", new_period, entry.number)
    return entry
