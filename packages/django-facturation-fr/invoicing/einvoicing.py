"""
Facturation électronique : transmission des factures émises, cycle de vie, factures reçues, e-reporting.

- Facture à un professionnel établi en France (SIREN de l'acheteur) : facture électronique,
  déposée sur la plateforme agréée, puis suivie (statuts 200 à 213) ; l'encaissement est
  transmis (212) dès que la facture est payée.
- Vente à un particulier ou à un professionnel étranger : pas de facture électronique, mais
  e-reporting au format officiel (flux 10, voir `ereporting`) des transactions et, pour les
  prestations de services, des encaissements, période par période.
- Facture fournisseur reçue (Factur-X, CII ou UBL) : lue, contrôlée, puis prise en charge,
  approuvée, mise en litige ou refusée par l'acheteur ; le projet peut alors la comptabiliser.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import logging

import facturx
from django.core.files.base import ContentFile
from lxml import etree
from django.db import transaction
from django.utils import timezone

from . import conf, ereporting, lifecycle, platforms
from .ereporting import period_bounds  # noqa: F401 (API historique)
from .models import EReport, IncomingInvoice, Invoice, LifecycleEvent, Transmission

logger = logging.getLogger(__name__)
ZERO = Decimal("0.00")


class EInvoicingError(Exception):
    pass


# === Aiguillage ===

def route(invoice) -> str:
    """« einvoice » : facture électronique entre professionnels en France ; « ereporting » sinon."""
    if invoice.buyer_siren and (invoice.buyer_country or "FR") == "FR":
        return "einvoice"
    return "ereporting"


# === Factures émises ===

def _event(code, message="", source="plateforme", at=None, sent=False, **target):
    return LifecycleEvent.objects.create(code=code, message=message[:300], source=source, at=at or timezone.now(),
                                         sent=sent, **target)


def record_status(transmission, code, message="", source="plateforme", at=None):
    """Nouveau statut d'une facture émise (sans doublon avec le dernier connu)."""
    if transmission.status == code:
        return False
    transmission.status = code
    transmission.error = message if code == lifecycle.REJECTED else ""
    transmission.save(update_fields=["status", "error", "updated_at"])
    _event(code, message, source, at, transmission=transmission)
    return True


def transmit(invoice, platform=None):
    """Dépose une facture électronique sur la plateforme (une seule fois)."""
    platform = platform or platforms.current()
    transmission, _ = Transmission.objects.get_or_create(invoice=invoice, defaults={"platform": platform.code})
    if transmission.external_id or transmission.status == lifecycle.DEPOSITED:
        return transmission
    try:
        external_id = platform.send_invoice(invoice)
    except platforms.PlatformError as exc:
        transmission.error = str(exc)[:2000]
        transmission.save(update_fields=["error", "updated_at"])
        logger.warning("Facture %s : dépôt impossible : %s", invoice.number, exc)
        return transmission
    if platform.automatic:
        transmission.external_id = external_id
        transmission.save(update_fields=["external_id", "updated_at"])
        record_status(transmission, lifecycle.DEPOSITED)
    return transmission


def mark_deposited(transmission, user=None, external_id=""):
    """Dépôt manuel effectué sur le portail de la plateforme."""
    transmission.external_id = external_id or transmission.external_id
    transmission.save(update_fields=["external_id", "updated_at"])
    record_status(transmission, lifecycle.DEPOSITED, "Déposée sur le portail de la plateforme", source=_who(user))


def _who(user):
    return getattr(user, "email", "") or "utilisateur"


def report_payment(transmission, platform=None, user=None):
    """Statut 212 « Encaissée » : montant et date du paiement de la facture."""
    invoice = transmission.invoice
    if transmission.payment_reported or not invoice.paid_at or transmission.status in (lifecycle.REFUSED, lifecycle.REJECTED):
        return False
    platform = platform or platforms.current()
    platform.send_payment(invoice, invoice.total_ttc, invoice.paid_at)
    transmission.payment_reported = True
    transmission.save(update_fields=["payment_reported", "updated_at"])
    _event(lifecycle.CASHED, f"{invoice.total_ttc} € encaissés le {invoice.paid_at:%d/%m/%Y}", _who(user) if user else "boutique",
           sent=platform.automatic, transmission=transmission)
    return True


def pending_invoices():
    return Invoice.objects.filter(transmission__isnull=True, kind__in=[Invoice.Kind.INVOICE, Invoice.Kind.CREDIT_NOTE]) \
        .exclude(buyer_siren="").filter(buyer_country="FR")


# === Factures reçues ===

def _to_decimal(value):
    return Decimal(str(value)).quantize(Decimal("0.01")) if value not in (None, "") else ZERO


def _to_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10]) if value else None


def parse_incoming(content: bytes) -> tuple[dict, str]:
    """Données EN 16931 (BT-…) d'une facture reçue : PDF Factur-X, XML CII ou XML UBL."""
    xml = content
    if content[:5] == b"%PDF-":
        try:
            _, xml = facturx.get_xml_from_pdf(content, check_xsd=False)
        except Exception as exc:  # noqa: BLE001 - PDF endommagé
            raise EInvoicingError(f"PDF illisible : {exc}") from exc
        if not xml:
            raise EInvoicingError("PDF sans données Factur-X : ce n'est pas une facture électronique.")
    try:
        # Fichier venu de l'extérieur : ni entités externes, ni accès réseau, ni DTD
        root = etree.fromstring(xml, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False,
                                                     huge_tree=False))
        flavor = facturx.get_flavor(root)
        data = facturx.parse_ubl_cii_xml(root, check_xsd=False)
    except Exception as exc:  # noqa: BLE001 - fichier illisible
        raise EInvoicingError(f"Facture illisible : {exc}") from exc
    if flavor not in ("factur-x", "ubl", "ubl-2.1", "ubl-2.1-invoice", "ubl-2.1-creditnote", "zugferd"):
        raise EInvoicingError(f"Format « {flavor} » non pris en charge (Factur-X, CII ou UBL attendu).")
    return data, flavor


def receive(content: bytes, filename: str, platform_code: str = "manual", external_id: str = "") -> IncomingInvoice:
    """Enregistre une facture fournisseur (sans doublon) au statut « Mise à disposition »."""
    digest = hashlib.sha256(content).hexdigest()
    existing = IncomingInvoice.objects.filter(file_sha256=digest).first()
    if existing:
        return existing
    data, flavor = parse_incoming(content)
    seller, buyer = data.get("BG-4") or {}, data.get("BG-7") or {}
    if not data.get("BT-1") or not data.get("BT-2"):
        raise EInvoicingError("Numéro ou date de facture absent.")
    breakdown = [{"rate": str(Decimal(str(row.get("BT-119", "0"))) / 100), "base": str(_to_decimal(row.get("BT-116"))),
                  "vat": str(_to_decimal(row.get("BT-117"))), "category": row.get("BT-118", "")}
                 for row in data.get("BG-23") or []]
    lines = [{"description": line.get("BT-153", ""), "quantity": str(line.get("BT-129", "")),
              "amount": str(_to_decimal(line.get("BT-131")))} for line in data.get("BG-25") or []]
    own_siren = conf.seller().get("siren", "")
    buyer_siren = str(buyer.get("legal_identifier") or "")
    with transaction.atomic():
        incoming = IncomingInvoice(
            platform=platform_code, external_id=external_id, file_sha256=digest, flavor=flavor,
            is_credit_note=str(data.get("BT-3")) in ("381", "396"), number=str(data["BT-1"])[:60],
            issue_date=_to_date(data["BT-2"]), due_date=_to_date(data.get("BT-9")),
            seller_name=str(seller.get("name", ""))[:200] or "Fournisseur", seller_siren=str(seller.get("legal_identifier") or "")[:20],
            seller_vat_number=str(seller.get("vat_identifier") or "")[:30], buyer_siren=buyer_siren[:20],
            currency=str(data.get("BT-5") or "EUR")[:3], total_ht=_to_decimal(data.get("BT-109")),
            total_vat=_to_decimal(data.get("BT-110")), total_ttc=_to_decimal(data.get("BT-112")),
            amount_due=_to_decimal(data.get("BT-115")), vat_breakdown=breakdown, lines=lines,
            data=json.loads(facturx.data_dict_to_json(data)),
            status=lifecycle.AVAILABLE,
            status_reason=("Destinataire différent de la société (SIREN " + buyer_siren + ")") if own_siren and buyer_siren
            and buyer_siren != own_siren else "")
        incoming.file.save(filename[:80] or f"{incoming.number}.xml", ContentFile(content), save=False)
        incoming.save()
        _event(lifecycle.AVAILABLE, f"Reçue ({flavor})", "plateforme" if platform_code != "manual" else "import",
               incoming=incoming)
    return incoming


def set_incoming_status(incoming, code: int, message: str = "", user=None, platform=None):
    """Statut donné par l'acheteur (prise en charge, approbation, litige, refus…), transmis à la plateforme."""
    if code not in lifecycle.BUYER_STATUSES:
        raise EInvoicingError(f"Statut {code} réservé au vendeur ou à la plateforme.")
    if incoming.status in lifecycle.FINAL:
        raise EInvoicingError(f"Facture déjà {lifecycle.label(incoming.status).lower()} : statut définitif.")
    if code == lifecycle.REFUSED and not message:
        raise EInvoicingError("Un refus doit être motivé.")
    platform = platform or platforms.current()
    if platform.automatic and incoming.platform == platform.code:
        platform.send_buyer_status(incoming, code, message)
    incoming.status, incoming.status_reason = code, message[:300]
    incoming.save(update_fields=["status", "status_reason"])
    _event(code, message, _who(user), sent=platform.automatic, incoming=incoming)
    conf.on_received_status(incoming, code)
    return incoming


# === E-reporting ===

def build_ereport(start: date, end: date, platform_code: str = "", kind: str = EReport.TRANSACTIONS) -> EReport | None:
    """E-reporting (flux 10) d'une période : transactions ou encaissements (voir `ereporting.build`)."""
    platform = platforms.current()
    return ereporting.build(kind, start, end, platform=platform)


def transmit_ereport(report, platform=None, user=None, today: date | None = None):
    """Transmet le flux 10 à la plateforme ; en dépôt manuel, l'utilisateur le marque transmis."""
    platform = platform or platforms.current()
    if report.transmitted_at:
        return report
    if report.period_end >= (today or timezone.localdate()):
        report.error = "Période en cours : l'e-reporting se transmet une fois la période close."
        report.save(update_fields=["error"])
        return report
    if not report.xml:
        return report  # flux non établi : l'erreur de paramétrage est affichée
    try:
        report.external_id = platform.send_ereport(report) or ""
    except platforms.PlatformError as exc:
        report.error = str(exc)[:2000]
        report.save(update_fields=["error"])
        return report
    if platform.automatic or user is not None:  # dépôt manuel : marqué transmis par l'utilisateur
        report.transmitted_at, report.error = timezone.now(), ""
        report.save(update_fields=["external_id", "transmitted_at", "error"])
    return report


def sync_ereports(platform, today: date) -> int:
    """
    E-reporting de la dernière période close de chaque type, puis rectificatives des périodes déjà
    transmises (quatre derniers mois) dont les données ont changé depuis.
    """
    transmitted = 0
    candidates = []
    for kind, _ in EReport.KINDS:
        start, end = ereporting.last_closed(kind, today)
        candidates.append((kind, start, end))
    recent = EReport.objects.filter(transmitted_at__isnull=False, period_end__gte=today - timedelta(days=124))
    candidates += [(r.kind, r.period_start, r.period_end) for r in recent]
    for kind, start, end in dict.fromkeys(candidates):
        report = ereporting.build(kind, start, end, platform=platform)
        if report and not report.transmitted_at and (report.rows or report.type_code == "RE"):
            transmitted += bool(transmit_ereport(report, platform, today=today).transmitted_at)
    return transmitted


# === Synchronisation (planificateur) ===

def sync(today: date | None = None) -> dict:
    """Dépôts en attente, statuts, encaissements, factures reçues, e-reporting des périodes closes (et rectificatives)."""
    platform = platforms.current()
    summary = {"deposited": 0, "events": 0, "payments": 0, "received": 0, "ereports": 0}
    if platform.automatic:
        open_transmissions = list(Transmission.objects.filter(platform=platform.code).exclude(status__in=lifecycle.FINAL)
                                  .exclude(external_id="").select_related("invoice"))
        by_id = {t.external_id: t for t in open_transmissions}
        for event in platform.fetch_events(open_transmissions):
            transmission = by_id.get(event.external_id)
            if transmission and record_status(transmission, event.code, event.message, at=event.at):
                summary["events"] += 1
        for received in platform.fetch_incoming():
            try:
                receive(received.content, received.filename, platform.code, received.external_id)
                summary["received"] += 1
            except EInvoicingError as exc:
                logger.warning("Facture reçue %s illisible : %s", received.external_id, exc)
    for invoice in pending_invoices():  # après les statuts : un dépôt n'avance qu'à la synchronisation suivante
        if transmit(invoice, platform).status == lifecycle.DEPOSITED:
            summary["deposited"] += 1
    for transmission in Transmission.objects.filter(payment_reported=False, invoice__paid_at__isnull=False) \
            .exclude(status__isnull=True).exclude(status__in=[lifecycle.REFUSED, lifecycle.REJECTED]).select_related("invoice"):
        summary["payments"] += report_payment(transmission, platform)
    summary["ereports"] = sync_ereports(platform, today or timezone.localdate())
    return summary
