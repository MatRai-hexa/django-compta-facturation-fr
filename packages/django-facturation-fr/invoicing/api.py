"""
API publique de la facturation.

    from invoicing import api

    invoice = api.issue_invoice(
        key="order:42", buyer=api.Buyer("Camille Martin", "1 rue de la Paix", "75002", "Paris", email="c@ex.fr"),
        items=[api.Item("T-shirt", 2, Decimal("25.00"))],          # prix TTC par défaut ; nature="services" au besoin
        allowances=[api.Allowance("Code BIENVENUE", Decimal("5.00"))],
        paid_at=day, payment_method="Carte bancaire", document=("order", 42))
    api.credit_invoice("order:42:refund", invoice, reason="Remboursement")

Chaque émission porte une clé unique : la rejouer renvoie la facture déjà émise.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal
import hashlib
import logging

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import conf
from .amounts import Allowance, Computed, Item, compute
from .models import Invoice, InvoiceLine, InvoiceSequence, InvoicingSettings

logger = logging.getLogger(__name__)

__all__ = ["Allowance", "Buyer", "InvoicingError", "Item", "credit_invoice", "invoices_for", "issue_invoice"]


class InvoicingError(Exception):
    pass


@dataclass
class Buyer:
    name: str
    address: str = ""
    postal_code: str = ""
    city: str = ""
    country: str = "FR"
    email: str = ""
    siren: str = ""
    vat_number: str = ""
    reference: str = ""
    delivery_address: str = ""


def _next_number(prefix: str, day: date_type) -> str:
    sequence, _ = InvoiceSequence.objects.get_or_create(prefix=prefix, year=day.year)
    sequence = InvoiceSequence.objects.select_for_update().get(pk=sequence.pk)
    sequence.last_number += 1
    sequence.save(update_fields=["last_number"])
    return f"{prefix}{day.year}-{sequence.last_number:05d}"


def _missing_seller_fields(seller: dict) -> list[str]:
    labels = {"name": "raison sociale", "address": "adresse", "postal_code": "code postal", "city": "ville",
              "siren": "SIREN (ou SIRET)"}
    return [label for key, label in labels.items() if not seller.get(key)]


def _attach_pdf(invoice):
    from .document import build_facturx_pdf

    content = build_facturx_pdf(invoice)
    invoice.pdf.save(f"{invoice.number}.pdf", ContentFile(content), save=False)
    invoice.pdf_sha256 = hashlib.sha256(content).hexdigest()
    invoice.save(update_fields=["pdf", "pdf_sha256"])


def issue_invoice(key: str, buyer: Buyer, items: list[Item], allowances: list[Allowance] = (), *,
                  prices_include_tax: bool = True, issue_date: date_type | None = None, sale_date: date_type | None = None,
                  due_date: date_type | None = None, paid_at: date_type | None = None, payment_method: str = "",
                  payment_terms: str = "", note: str = "", document=None, kind=Invoice.Kind.INVOICE,
                  credited_invoice: Invoice | None = None) -> Invoice:
    """Émet une facture : numéro, vendeur figé, montants, PDF Factur-X. Idempotent par `key`."""
    existing = Invoice.objects.filter(key=key).first()
    if existing:
        return existing
    if not items:
        raise InvoicingError("Une facture comporte au moins une ligne.")
    amounts = compute(list(items), list(allowances), prices_include_tax=prices_include_tax)
    return _store(key, buyer, amounts, prices_include_tax=prices_include_tax, issue_date=issue_date, sale_date=sale_date,
                  due_date=due_date, paid_at=paid_at, payment_method=payment_method, payment_terms=payment_terms,
                  note=note, document=document, kind=kind, credited_invoice=credited_invoice)


def _store(key, buyer, amounts, *, prices_include_tax, issue_date, sale_date, due_date, paid_at, payment_method,
           payment_terms, note, document, kind, credited_invoice):
    seller = conf.seller()
    missing = _missing_seller_fields(seller)
    if amounts.total_vat and not seller.get("vat_number"):
        missing.append("n° de TVA intracommunautaire (obligatoire dès qu'une facture comporte de la TVA)")
    if missing:
        raise InvoicingError("Identité du vendeur incomplète : " + ", ".join(missing) + " (paramètres de facturation).")
    settings = InvoicingSettings.get()
    default_nature = "services" if settings.operation_category == "services" else "goods"
    day = issue_date or timezone.localdate()
    document_type, document_id = document or ("", "")
    try:
        with transaction.atomic():
            prefix = settings.credit_note_prefix if kind == Invoice.Kind.CREDIT_NOTE else settings.invoice_prefix
            invoice = Invoice.objects.create(
                key=key, kind=kind, number=_next_number(prefix, day), issue_date=day, sale_date=sale_date,
                due_date=due_date, seller=seller,
                buyer_name=buyer.name[:160], buyer_address=buyer.address[:200], buyer_postal_code=buyer.postal_code[:12],
                buyer_city=buyer.city[:100], buyer_country=(buyer.country or "FR")[:2].upper(), buyer_email=buyer.email,
                buyer_siren=buyer.siren[:9], buyer_vat_number=buyer.vat_number[:20], buyer_reference=buyer.reference[:60],
                delivery_address=buyer.delivery_address[:300], prices_include_tax=prices_include_tax,
                lines_total=amounts.lines_total, allowances=[{**a, "amount": str(a["amount"]), "vat_rate": str(a["vat_rate"])}
                                                              for a in amounts.allowances],
                allowances_total=amounts.allowances_total,
                vat_breakdown=[{k: str(v) for k, v in row.items()} for row in amounts.vat],
                total_ht=amounts.total_ht, total_vat=amounts.total_vat, total_ttc=amounts.total_ttc,
                payment_terms=payment_terms[:200], paid_at=paid_at, payment_method=payment_method[:60],
                credited_invoice=credited_invoice, note=note,
                document_type=document_type or "", document_id=str(document_id or ""))
            InvoiceLine.objects.bulk_create([
                InvoiceLine(invoice=invoice, position=i, description=line["description"][:300], quantity=line["quantity"],
                            unit=line["unit"], net_price=line["net_price"], net_amount=line["net_amount"],
                            vat_rate=line["vat_rate"], nature=line.get("nature") or default_nature)
                for i, line in enumerate(amounts.lines, start=1)])
            _attach_pdf(invoice)
    except IntegrityError:
        existing = Invoice.objects.filter(key=key).first()
        if existing:  # émise entre-temps par un autre processus
            return existing
        raise
    transaction.on_commit(lambda: conf.on_issued(invoice))
    logger.info("%s %s émise (%s €)", invoice.get_kind_display(), invoice.number, invoice.total_ttc)
    return invoice


def credit_invoice(key: str, invoice: Invoice, reason: str = "", issue_date: date_type | None = None,
                   paid_at: date_type | None = None) -> Invoice:
    """Avoir total d'une facture : mêmes lignes et remises, référence à la facture d'origine."""
    if invoice.is_credit_note:
        raise InvoicingError("Un avoir ne s'annule pas par un avoir.")
    existing = Invoice.objects.filter(key=key).first()
    if existing:
        return existing
    # Montants repris tels quels (et non recalculés) : l'avoir annule la facture au centime près
    amounts = Computed(
        lines=[{"description": l.description, "quantity": l.quantity, "unit": l.unit, "net_price": l.net_price,
                "net_amount": l.net_amount, "vat_rate": l.vat_rate, "nature": l.nature} for l in invoice.lines.all()],
        allowances=[{"label": a["label"], "amount": Decimal(a["amount"]), "vat_rate": Decimal(a["vat_rate"])}
                    for a in invoice.allowances],
        vat=[{k: Decimal(v) for k, v in row.items()} for row in invoice.vat_breakdown],
        lines_total=invoice.lines_total, allowances_total=invoice.allowances_total, total_ht=invoice.total_ht,
        total_vat=invoice.total_vat, total_ttc=invoice.total_ttc, payable=invoice.total_ttc)
    note = f"Avoir sur la facture {invoice.number} du {invoice.issue_date:%d/%m/%Y}" + (f" : {reason}" if reason else ".")
    buyer = Buyer(invoice.buyer_name, invoice.buyer_address, invoice.buyer_postal_code, invoice.buyer_city,
                  invoice.buyer_country, invoice.buyer_email, invoice.buyer_siren, invoice.buyer_vat_number,
                  invoice.buyer_reference, invoice.delivery_address)
    return _store(key, buyer, amounts, prices_include_tax=invoice.prices_include_tax, issue_date=issue_date,
                  sale_date=invoice.sale_date, due_date=None, paid_at=paid_at, payment_method=invoice.payment_method,
                  payment_terms="", note=note, document=(invoice.document_type, invoice.document_id),
                  kind=Invoice.Kind.CREDIT_NOTE, credited_invoice=invoice)


def invoices_for(document_type: str, document_id) -> list[Invoice]:
    return list(Invoice.objects.filter(document_type=document_type, document_id=str(document_id)).order_by("issue_date", "pk"))
