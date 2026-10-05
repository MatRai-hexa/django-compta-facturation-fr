"""
E-reporting au format officiel : flux 10 des spécifications externes de la facturation électronique
(DGFiP, version 3.2 du 30/04/2026 : XSD dans `xsd/ereporting`, annexe 6 v1.10, annexe 7 v1.9).

Deux transmissions distinctes par période (règle G6.29) :

- transactions (bloc TransactionsReport) : 10.1, une occurrence par facture à un professionnel
  établi hors de France, avec ses remises et ses lignes (quantité, prix net, désignation) ; 10.3, ventes aux particuliers agrégées par jour, devise et catégorie
  (TLB1 biens, TPS1 services, TNT1 hors du champ de la TVA française) ;
- encaissements (bloc PaymentsReport), pour les seules prestations de services et sauf option pour
  la TVA d'après les débits : 10.2 par facture à un professionnel étranger, 10.4 par jour pour les
  particuliers. Les encaissements des factures électroniques passent par le statut 212.

Les périodes dépendent du régime de TVA (dossier général, tableau 13). Une période transmise dont
les données changent ensuite est retransmise en rectificative (RE), qui annule et remplace la
précédente. Chaque flux est validé contre le schéma officiel avant d'être conservé.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from django.utils import timezone
from lxml import etree

from . import conf
from .amounts import CENT, ZERO, _distribute, rate_of
from .models import EReport, Invoice, InvoicingSettings

XSD = Path(__file__).resolve().parent / "xsd" / "ereporting" / "ereporting.xsd"
PROFILE = "urn.cpro.gouv.fr:1p0:ereporting"  # TT-29 (règle S1.12)

# Périodes (transactions, encaissements) par régime de TVA
PERIODS = {"real_monthly": ("decade", "month"), "real_quarterly": ("month", "month"),
           "simplified": ("month", "month"), "franchise": ("bimonth", "bimonth")}

# États membres de l'Union européenne hors France (codes ISO 3166)
EU = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV",
      "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK"}


class EReportingError(Exception):
    """Flux impossible à établir (identité du déclarant ou de la plateforme incomplète, schéma non respecté)."""


# === Périodes ===

def _month_end(day: date) -> date:
    following = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


def period_bounds(day: date, period: str) -> tuple[date, date]:
    """Période (décade, mois, bimestre civil) contenant `day`."""
    if period == "decade":
        start_day = 1 if day.day <= 10 else 11 if day.day <= 20 else 21
        start = day.replace(day=start_day)
        end = start.replace(day=10) if start_day == 1 else start.replace(day=20) if start_day == 11 else _month_end(day)
        return start, end
    if period == "bimonth":
        first_month = day.month - (day.month - 1) % 2
        start = day.replace(month=first_month, day=1)
        return start, _month_end(start.replace(month=first_month + 1))
    return day.replace(day=1), _month_end(day)


def periods(settings=None) -> dict:
    """{"transactions": "decade", "payments": "month"} selon le régime de TVA."""
    settings = settings or InvoicingSettings.get()
    transactions, payments = PERIODS.get(settings.vat_regime, PERIODS["real_monthly"])
    return {EReport.TRANSACTIONS: transactions, EReport.PAYMENTS: payments}


def last_closed(kind: str, today: date, settings=None) -> tuple[date, date]:
    period = periods(settings)[kind]
    start, _ = period_bounds(today, period)
    return period_bounds(start - timedelta(days=1), period)


# === Classement des opérations ===

def scope(invoice) -> str:
    """« einvoice » (facture électronique), « b2bi » (professionnel établi hors de France) ou « b2c »."""
    country = (invoice.buyer_country or "FR").upper()
    if invoice.buyer_siren and country == "FR":
        return "einvoice"
    if country != "FR" and (invoice.buyer_vat_number or invoice.buyer_siren):
        return "b2bi"
    return "b2c"


def nature_breakdown(invoice) -> dict:
    """{(nature, taux): {"base", "vat"}} : la ventilation de TVA répartie entre biens et services."""
    weights = defaultdict(lambda: defaultdict(lambda: ZERO))
    for line in invoice.lines.all():
        weights[rate_of(line.vat_rate)][line.nature] += line.net_amount
    result = {}
    for row in invoice.vat_breakdown:
        rate = rate_of(row["rate"])
        natures = weights.get(rate) or {"goods": Decimal("1")}
        keys = sorted(natures)
        bases = _distribute(Decimal(row["base"]), [natures[k] for k in keys])
        vats = _distribute(Decimal(row["vat"]), [natures[k] for k in keys])
        for key, base, vat in zip(keys, bases, vats):
            result[(key, rate)] = {"base": base, "vat": vat}
    return result


def category(invoice, nature: str, rate: Decimal) -> str:
    """Catégorie de transactions (règle G1.68)."""
    if not rate and (invoice.buyer_country or "FR").upper() != "FR":
        return "TNT1"  # vente à distance ou exportation : TVA non due en France
    return "TPS1" if nature == "services" else "TLB1"


def _money(value) -> str:
    return f"{Decimal(value).quantize(CENT)}"


def _percent(rate) -> str:
    return f"{(Decimal(rate) * 100).normalize():f}"  # 0.0550 -> "5.5" (règle G1.24)


def _day(value) -> str:
    return value.strftime("%Y%m%d")  # AAAAMMJJ (règle G1.09)


# === Données d'une période ===

def transaction_rows(start: date, end: date):
    """Lignes 10.1 (factures, une par taux) et 10.3 (particuliers, par jour, catégorie et taux), signées."""
    rows, b2bi = [], []
    totals = defaultdict(lambda: {"base": ZERO, "vat": ZERO})
    documents = defaultdict(set)
    invoices = (Invoice.objects.filter(issue_date__gte=start, issue_date__lte=end)
                .prefetch_related("lines").order_by("issue_date", "number"))
    for invoice in invoices:
        where = scope(invoice)
        if where == "einvoice":
            continue
        sign = -1 if invoice.is_credit_note else 1
        if where == "b2bi":
            b2bi.append(invoice)
            for row in invoice.vat_breakdown:
                rows.append({"flow": "10.1", "invoice": invoice.number, "date": invoice.issue_date.isoformat(),
                             "currency": invoice.currency, "category": "", "rate": str(rate_of(row["rate"])),
                             "base": _money(sign * Decimal(row["base"])), "vat": _money(sign * Decimal(row["vat"])),
                             "count": 1})
            continue
        for (nature, rate), amounts in nature_breakdown(invoice).items():
            group = (invoice.issue_date.isoformat(), invoice.currency, category(invoice, nature, rate))
            totals[(*group, str(rate))]["base"] += sign * amounts["base"]
            totals[(*group, str(rate))]["vat"] += sign * amounts["vat"]
            documents[group].add(invoice.pk)
    for (day, currency, cat, rate), amounts in sorted(totals.items()):
        rows.append({"flow": "10.3", "invoice": "", "date": day, "currency": currency, "category": cat, "rate": rate,
                     "base": _money(amounts["base"]), "vat": _money(amounts["vat"]),
                     "count": len(documents[(day, currency, cat)])})
    return rows, b2bi


def payment_rows(start: date, end: date, settings=None):
    """Lignes 10.2 (factures) et 10.4 (particuliers, par jour) : encaissements TTC des prestations de services."""
    settings = settings or InvoicingSettings.get()
    if settings.vat_on_debits:
        return [], []  # TVA exigible à la facturation : pas de données de paiement
    rows, b2bi = [], []
    totals = defaultdict(lambda: ZERO)
    invoices = (Invoice.objects.filter(paid_at__gte=start, paid_at__lte=end)
                .prefetch_related("lines").order_by("paid_at", "number"))
    for invoice in invoices:
        where = scope(invoice)
        if where == "einvoice":
            continue  # statut 212 « Encaissée » du cycle de vie
        services = {rate: amounts["base"] + amounts["vat"]
                    for (nature, rate), amounts in nature_breakdown(invoice).items() if nature == "services"}
        if not services:
            continue
        sign = -1 if invoice.is_credit_note else 1  # remboursement : encaissement négatif
        if where == "b2bi":
            b2bi.append(invoice)
            rows += [{"flow": "10.2", "invoice": invoice.number, "date": invoice.paid_at.isoformat(), "rate": str(rate),
                      "amount": _money(sign * amount)} for rate, amount in sorted(services.items(), reverse=True)]
            continue
        for rate, amount in services.items():
            totals[(invoice.paid_at.isoformat(), str(rate))] += sign * amount
    rows += [{"flow": "10.4", "invoice": "", "date": day, "rate": rate, "amount": _money(amount)}
             for (day, rate), amount in sorted(totals.items())]
    return rows, b2bi


def fingerprint(rows) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


# === Flux 10 (XML) ===

def _el(parent, tag, text=None, **attrs):
    element = etree.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        element.text = str(text)
    return element


@lru_cache(maxsize=1)
def _schema():
    return etree.XMLSchema(etree.parse(str(XSD)))


def declarant(settings=None, platform=None) -> dict:
    """Identité du déclarant (le vendeur) et de l'émetteur du flux (la plateforme agréée), ou EReportingError."""
    settings = settings or InvoicingSettings.get()
    seller = conf.seller()
    registration = (getattr(platform, "registration_id", "") or settings.platform_registration).strip()
    company = (getattr(platform, "company_name", "") or settings.platform_company).strip()
    missing = []
    if not (seller.get("siren") or "").isdigit() or len(seller.get("siren") or "") != 9:
        missing.append("SIREN du vendeur (9 chiffres)")
    if not seller.get("name"):
        missing.append("raison sociale du vendeur")
    if len(registration) != 4:
        missing.append("matricule de la plateforme agréée (4 caractères)")
    if not company:
        missing.append("raison sociale de la plateforme agréée")
    if missing:
        raise EReportingError("E-reporting impossible, à compléter dans les paramètres de facturation : "
                              + ", ".join(missing) + ".")
    return {"seller": seller, "registration": registration, "company": company}


def _document(root, report, identity):
    document = _el(root, "ReportDocument")
    _el(document, "Id", report.transmission_id)
    stamp = _el(document, "IssueDateTime")
    _el(stamp, "DateTimeString", timezone.localtime().strftime("%Y%m%d%H%M%S"))  # AAAAMMJJHHMMSS (G7.53)
    _el(document, "TypeCode", report.type_code)
    sender = _el(document, "Sender")  # la plateforme agréée (G6.22, G7.51)
    _el(sender, "Id", identity["registration"], schemeId="0238")
    _el(sender, "Name", identity["company"][:150])
    _el(sender, "RoleCode", "WK")
    issuer = _el(document, "Issuer")  # le vendeur (G6.26, G7.52)
    _el(issuer, "Id", identity["seller"]["siren"], schemeId="0002")
    _el(issuer, "Name", identity["seller"]["name"][:150])
    _el(issuer, "RoleCode", "SE")


def _period(parent, report):
    period = _el(parent, "ReportPeriod")
    _el(period, "StartDate", _day(report.period_start))
    _el(period, "EndDate", _day(report.period_end))


def _buyer_id(invoice) -> tuple[str, str]:
    """(identifiant, schéma ISO 6523) de l'acheteur étranger (règle G2.19)."""
    country = invoice.buyer_country.upper()
    if country in EU and invoice.buyer_vat_number:
        return invoice.buyer_vat_number.replace(" ", "")[:18], "0223"
    return f"{country}{invoice.buyer_name[:16]}", "0227"


def _tax_category(invoice, rate, natures) -> tuple[str, str, str]:
    """(code UNTDID 5305, motif, code de motif) d'un taux de la facture à un professionnel étranger."""
    if rate:
        return "S", "", ""
    if natures == {"services"}:
        return "AE", "Autoliquidation par le preneur (article 283-2 du CGI)", "VATEX-EU-AE"
    if invoice.buyer_country.upper() in EU:
        return "K", "Livraison intracommunautaire exonérée (article 262 ter I du CGI)", "VATEX-EU-IC"
    return "G", "Exportation exonérée (article 262 I du CGI)", "VATEX-EU-G"


def _invoice(parent, invoice, settings, seller):
    breakdown = nature_breakdown(invoice)
    natures = {nature for nature, _ in breakdown}
    element = _el(parent, "Invoice")
    _el(element, "ID", invoice.number)
    _el(element, "IssueDate", _day(invoice.issue_date))
    _el(element, "TypeCode", "381" if invoice.is_credit_note else "380")
    _el(element, "CurrencyCode", invoice.currency)
    if invoice.due_date:
        _el(element, "DueDate", _day(invoice.due_date))
    if settings.vat_on_debits and "services" in natures:
        _el(element, "TaxDueDateTypeCode", "5")  # TVA exigible à la facturation (règle G1.44)
    process = _el(element, "BusinessProcess")
    kind = "M" if len(natures) > 1 else "S" if natures == {"services"} else "B"
    paid = invoice.paid_at is not None and invoice.paid_at <= invoice.issue_date
    _el(process, "ID", f"{kind}{2 if paid else 1}")  # cadre de facturation (règle G1.02)
    _el(process, "TypeID", PROFILE)
    if invoice.credited_invoice_id:
        reference = _el(element, "ReferencedDocument")
        _el(reference, "ID", invoice.credited_invoice.number)
        _el(reference, "IssueDate", _day(invoice.credited_invoice.issue_date))
    seller_el = _el(element, "Seller")
    _el(seller_el, "CompanyId", seller["siren"], schemeId="0002")
    if seller.get("vat_number"):
        _el(seller_el, "TaxRegistrationId", seller["vat_number"].replace(" ", ""), qualifyingId="VAT")
    _el(_el(seller_el, "PostalAddress"), "CountryId", (seller.get("country") or "FR").upper())
    buyer = _el(element, "Buyer")
    identifier, scheme = _buyer_id(invoice)
    _el(buyer, "CompanyId", identifier, schemeId=scheme)
    if scheme == "0223":
        _el(buyer, "TaxRegistrationId", identifier, qualifyingId="VAT")  # règle G2.33
    _el(_el(buyer, "PostalAddress"), "CountryId", invoice.buyer_country.upper())
    if invoice.sale_date and invoice.sale_date != invoice.issue_date:
        _el(_el(element, "Delivery"), "Date", _day(invoice.sale_date))  # règle G1.38
    categories = {rate: _tax_category(invoice, rate, {n for n, r in breakdown if r == rate}) for _, rate in breakdown}
    for allowance in invoice.allowances:  # remises au niveau du document (TG-20), montants HT
        rate = rate_of(allowance["vat_rate"])
        charge = _el(element, "AllowanceCharge", ChargeIndicator="false")
        _el(charge, "Amount", _money(allowance["amount"]))
        _el(charge, "TaxCategoryCode", categories[rate][0])
        _el(charge, "TaxPercent", _percent(rate))
    totals = _el(element, "MonetaryTotal")
    _el(totals, "TaxExclusiveAmount", _money(invoice.total_ht))
    _el(totals, "TaxAmount", _money(invoice.total_vat), CurrencyCode="EUR")
    for row in invoice.vat_breakdown:
        rate = rate_of(row["rate"])
        sub = _el(element, "TaxSubTotal")
        _el(sub, "TaxableAmount", _money(row["base"]))
        _el(sub, "TaxAmount", _money(row["vat"]))
        tax = _el(sub, "TaxCategory")
        code, reason, reason_code = categories[rate]
        _el(tax, "Code", code)
        _el(tax, "Percent", _percent(rate))
        if reason:
            _el(tax, "TaxExemptionReason", reason)
            _el(tax, "TaxExemptionReasonCode", reason_code)
    for line in invoice.lines.all():  # lignes de facture (TG-24)
        line_el = _el(element, "Line")
        _el(line_el, "BilledQuantity", f"{line.quantity.normalize():f}", UnitCode=line.unit)
        _el(_el(line_el, "Price"), "PriceAmount", f"{line.net_price.normalize():f}")  # prix unitaire net HT
        _el(_el(line_el, "Product"), "Name", line.description)


def _transactions(root, report, invoices, settings, seller):
    block = _el(root, "TransactionsReport")
    _period(block, report)
    for invoice in invoices:
        _invoice(block, invoice, settings, seller)
    groups = defaultdict(list)
    for row in report.rows:
        if row["flow"] == "10.3":
            groups[(row["date"], row["currency"], row["category"])].append(row)
    for (day, currency, cat), rows in groups.items():
        element = _el(block, "Transactions")
        _el(element, "Date", day.replace("-", ""))
        _el(element, "TransactionsCurrency", currency)
        if cat == "TPS1" and settings.vat_on_debits:
            _el(element, "TaxDueDateTypeCode", "5")  # règle G1.67
        _el(element, "CategoryCode", cat)
        _el(element, "TaxExclusiveAmount", _money(sum(Decimal(r["base"]) for r in rows)))
        _el(element, "TaxTotal", _money(sum(Decimal(r["vat"]) for r in rows)))
        _el(element, "TransactionsCount", rows[0]["count"])
        for row in sorted(rows, key=lambda r: -Decimal(r["rate"])):
            sub = _el(element, "TaxSubtotal")
            _el(sub, "TaxPercent", _percent(row["rate"]))
            _el(sub, "TaxableAmount", row["base"])
            _el(sub, "TaxTotal", row["vat"])


def _subtotals(payment, rows):
    for row in sorted(rows, key=lambda r: -Decimal(r["rate"])):
        sub = _el(payment, "SubTotals")
        _el(sub, "TaxPercent", _percent(row["rate"]))
        _el(sub, "CurrencyCode", "EUR")  # montants encaissés en euros (règle G6.27)
        _el(sub, "Amount", row["amount"])


def _payments(root, report, invoices):
    block = _el(root, "PaymentsReport")
    _period(block, report)
    by_invoice = defaultdict(list)
    by_day = defaultdict(list)
    for row in report.rows:
        (by_invoice[row["invoice"]] if row["flow"] == "10.2" else by_day[row["date"]]).append(row)
    for invoice in invoices:
        element = _el(block, "Invoice")
        _el(element, "InvoiceID", invoice.number)
        _el(element, "IssueDate", _day(invoice.issue_date))
        payment = _el(element, "Payment")
        _el(payment, "Date", _day(invoice.paid_at))
        _subtotals(payment, by_invoice[invoice.number])
    for day, rows in by_day.items():
        payment = _el(_el(block, "Transactions"), "Payment")
        _el(payment, "Date", day.replace("-", ""))
        _subtotals(payment, rows)


def render(report, invoices, settings=None, platform=None) -> str:
    """Flux 10 d'un e-reporting, validé contre le schéma officiel."""
    settings = settings or InvoicingSettings.get()
    identity = declarant(settings, platform)
    root = etree.Element("Report")
    _document(root, report, identity)
    if report.kind == EReport.TRANSACTIONS:
        _transactions(root, report, invoices, settings, identity["seller"])
    else:
        _payments(root, report, invoices)
    schema = _schema()
    if not schema.validate(root):
        errors = "; ".join(f"ligne {e.line} : {e.message}" for e in list(schema.error_log)[:5])
        raise EReportingError(f"Flux 10 non conforme au schéma officiel : {errors}")
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True).decode("utf-8")


# === E-reporting d'une période ===

def build(kind: str, start: date, end: date, platform=None, create: bool = True) -> EReport | None:
    """
    Établit (ou rétablit) l'e-reporting d'une période. Une période transmise dont les données ont
    changé devient une rectificative (RE, version suivante) qu'il reste à transmettre.
    """
    settings = InvoicingSettings.get()
    rows, invoices = transaction_rows(start, end) if kind == EReport.TRANSACTIONS else payment_rows(start, end, settings)
    report = EReport.objects.filter(kind=kind, period_start=start, period_end=end).first()
    if report is None:
        if not (rows and create):
            return None
        report = EReport(kind=kind, period_start=start, period_end=end,
                         platform=getattr(platform, "code", "") or settings.platform)
    digest = fingerprint(rows)
    if report.transmitted_at:
        if not report.fingerprint or digest == report.fingerprint:
            return report  # inchangée (ou transmise avant le format officiel)
        report.history = [*report.history, {
            "version": report.version, "type_code": report.type_code, "transmission_id": report.transmission_id,
            "transmitted_at": report.transmitted_at.isoformat(), "external_id": report.external_id,
            "total_ht": str(report.total_ht), "total_vat": str(report.total_vat), "total_paid": str(report.total_paid)}]
        report.version += 1
        report.type_code, report.transmitted_at, report.external_id = "RE", None, ""
    report.rows, report.fingerprint = rows, digest
    report.total_ht = sum((Decimal(r.get("base", 0)) for r in rows), ZERO)
    report.total_vat = sum((Decimal(r.get("vat", 0)) for r in rows), ZERO)
    report.total_paid = sum((Decimal(r.get("amount", 0)) for r in rows), ZERO)
    seller_siren = conf.seller().get("siren") or "000000000"
    report.transmission_id = (f"ER-{seller_siren}-{'T' if kind == EReport.TRANSACTIONS else 'P'}"
                              f"{start:%Y%m%d}-{end:%Y%m%d}-V{report.version}")  # unique par période (G8.05)
    try:
        report.xml, report.error = render(report, invoices, settings, platform), ""
    except EReportingError as exc:
        report.xml, report.error = "", str(exc)
    report.save()
    return report
