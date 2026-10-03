"""
PDF de la facture et assemblage Factur-X (PDF/A-3 avec le XML CII EN 16931 en pièce jointe).

Polices intégrées (Bitstream Vera, fournie avec ReportLab) et profil de couleurs sRGB : deux
exigences du PDF/A. Le XML est validé par le schéma XSD Factur-X avant d'être joint.
"""
from __future__ import annotations

from decimal import Decimal
import io
import os

import facturx
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, StreamObject, TextStringObject
from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import conf
from .cii import build_cii, legal_notes
from .models import InvoicingSettings

FONT, BOLD = "Vera", "VeraBd"
_fonts_ready = False
OPERATION_LABELS = {"goods": "Livraisons de biens", "services": "Prestations de services", "both": "Livraisons de biens et prestations de services"}


def _register_fonts():
    global _fonts_ready
    if not _fonts_ready:
        folder = os.path.join(os.path.dirname(rl_config.__file__), "fonts")
        pdfmetrics.registerFont(TTFont(FONT, os.path.join(folder, "Vera.ttf")))
        pdfmetrics.registerFont(TTFont(BOLD, os.path.join(folder, "VeraBd.ttf")))
        _fonts_ready = True


def _euros(value) -> str:
    text = f"{Decimal(value):,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} €"


def _rate(value) -> str:
    return f"{(Decimal(value) * 100).normalize():f}".replace(".", ",") + " %"


def _unit_price(line) -> str:
    """Prix unitaire HT : au centime s'il redonne le montant de la ligne, sinon avec les décimales utiles."""
    cents = line.net_price.quantize(Decimal("0.01"))
    if (cents * line.quantity).quantize(Decimal("0.01")) == line.net_amount:
        return _euros(cents)
    precise = line.net_price.quantize(Decimal("0.0001")).normalize()
    return f"{precise:f}".replace(".", ",") + " €"


def _quantity(value) -> str:
    return f"{Decimal(value).normalize():f}".replace(".", ",")


def _styles(color):
    base = ParagraphStyle("base", fontName=FONT, fontSize=9, leading=12)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=7.5, leading=10, textColor=colors.HexColor("#4b5563")),
        "bold": ParagraphStyle("bold", parent=base, fontName=BOLD),
        "title": ParagraphStyle("title", parent=base, fontName=BOLD, fontSize=16, leading=20, textColor=colors.HexColor(color)),
        "right": ParagraphStyle("right", parent=base, alignment=TA_RIGHT),
        "bold_right": ParagraphStyle("bold_right", parent=base, fontName=BOLD, alignment=TA_RIGHT),
    }


def _p(text, style):
    escaped = str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
    return Paragraph(escaped, style)


def render_pdf(invoice, settings: InvoicingSettings) -> bytes:
    _register_fonts()
    branding = conf.branding()
    color = branding.get("color") or "#1f2937"
    s = _styles(color)
    seller = invoice.seller
    buffer = io.BytesIO()
    title = f"{'Avoir' if invoice.is_credit_note else 'Facture'} {invoice.number}"
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=18 * mm, title=title, author=seller.get("name", ""), subject=title,
                            creator="django-facturation-fr", initialFontName=FONT, initialFontSize=9)
    story = []

    # En-tête : vendeur (logo ou nom) et titre
    seller_lines = [seller.get("address", ""), f"{seller.get('postal_code', '')} {seller.get('city', '')}".strip(),
                    seller.get("email", ""), seller.get("phone", "")]
    logo = branding.get("logo_path")
    head = [Image(logo, width=40 * mm, height=16 * mm, kind="proportional")] if logo and os.path.exists(logo) else []
    head.append(_p(seller.get("name", ""), s["bold"]))
    head.append(_p("\n".join(filter(None, seller_lines)), s["small"]))
    dates = [f"Date : {invoice.issue_date:%d/%m/%Y}"]
    if invoice.sale_date and invoice.sale_date != invoice.issue_date:
        dates.append(f"Date de la vente : {invoice.sale_date:%d/%m/%Y}")
    if invoice.due_date and not invoice.paid_at:
        dates.append(f"Échéance : {invoice.due_date:%d/%m/%Y}")
    if invoice.buyer_reference:
        dates.append(f"Référence : {invoice.buyer_reference}")
    header = Table([[head, [_p(title, s["title"]), _p("\n".join(dates), s["base"])]]], colWidths=[95 * mm, 79 * mm])
    header.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), FONT), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [header, Spacer(1, 8 * mm)]

    # Client
    buyer = [invoice.buyer_name, invoice.buyer_address, f"{invoice.buyer_postal_code} {invoice.buyer_city}".strip()]
    if invoice.buyer_country and invoice.buyer_country != "FR":
        buyer.append(invoice.buyer_country)
    if invoice.buyer_siren:
        buyer.append(f"SIREN : {invoice.buyer_siren}")
    if invoice.buyer_vat_number:
        buyer.append(f"N° TVA : {invoice.buyer_vat_number}")
    blocks = [[_p("Facturé à", s["small"]), _p("Livré à" if invoice.delivery_address else "", s["small"])],
              [_p("\n".join(filter(None, buyer)), s["base"]), _p(invoice.delivery_address, s["base"])]]
    client = Table(blocks, colWidths=[87 * mm, 87 * mm])
    client.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), FONT), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [client, Spacer(1, 7 * mm)]

    # Lignes
    rows = [[_p(h, s["bold"]) for h in ("Désignation", "Qté", "PU HT", "TVA", "Montant HT")]]
    for line in invoice.lines.all():
        rows.append([_p(line.description, s["base"]), _p(_quantity(line.quantity), s["right"]),
                     _p(_unit_price(line), s["right"]), _p(_rate(line.vat_rate), s["right"]),
                     _p(_euros(line.net_amount), s["right"])])
    for allowance in invoice.allowances:
        rows.append([_p(allowance["label"], s["base"]), "", "", _p(_rate(allowance["vat_rate"]), s["right"]),
                     _p("-" + _euros(allowance["amount"]), s["right"])])
    table = Table(rows, colWidths=[86 * mm, 16 * mm, 26 * mm, 18 * mm, 28 * mm], repeatRows=1)
    table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), FONT), 
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(color)),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [table, Spacer(1, 5 * mm)]

    # TVA par taux et totaux
    vat_rows = [[_p(h, s["bold"]) for h in ("Taux", "Base HT", "TVA")]]
    vat_rows += [[_p(_rate(r["rate"]), s["base"]), _p(_euros(r["base"]), s["right"]), _p(_euros(r["vat"]), s["right"])]
                 for r in invoice.vat_breakdown]
    totals = [[_p("Total HT", s["base"]), _p(_euros(invoice.total_ht), s["right"])],
              [_p("Total TVA", s["base"]), _p(_euros(invoice.total_vat), s["right"])],
              [_p("Total TTC", s["bold"]), _p(_euros(invoice.total_ttc), s["bold_right"])]]
    if invoice.paid_at:
        totals.append([_p("Déjà réglé", s["base"]), _p("-" + _euros(invoice.total_ttc), s["right"])])
        totals.append([_p("Net à payer", s["bold"]), _p(_euros(0), s["bold_right"])])
    vat_table = Table(vat_rows, colWidths=[20 * mm, 28 * mm, 24 * mm])
    vat_table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), FONT), ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#9ca3af")),
                                   ("LEFTPADDING", (0, 0), (-1, -1), 2)]))
    total_table = Table(totals, colWidths=[34 * mm, 30 * mm])
    total_table.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), FONT), ("LINEABOVE", (0, 2), (-1, 2), 0.8, colors.HexColor(color))]))
    summary = Table([[vat_table, "", total_table]], colWidths=[76 * mm, 34 * mm, 64 * mm])
    summary.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), FONT), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [summary, Spacer(1, 7 * mm)]

    # Paiement et mentions
    mentions = []
    if invoice.credited_invoice_id:
        mentions.append(invoice.note or f"Avoir sur la facture {invoice.credited_invoice.number} "
                                        f"du {invoice.credited_invoice.issue_date:%d/%m/%Y}.")
    if invoice.paid_at:
        mentions.append(f"Facture acquittée le {invoice.paid_at:%d/%m/%Y}" + (f" ({invoice.payment_method})." if invoice.payment_method else "."))
    elif not invoice.is_credit_note:
        mentions.append(invoice.payment_terms or "Paiement à réception de la facture.")
        if seller.get("iban"):
            mentions.append(f"Règlement par virement : IBAN {seller['iban']}")
    mentions.append(f"Nature des opérations : {OPERATION_LABELS.get(settings.operation_category, settings.operation_category)}.")
    if settings.vat_on_debits:
        mentions.append("Option pour le paiement de la taxe d'après les débits.")
    if settings.vat_exemption and not invoice.total_vat:
        mentions.append(settings.vat_exemption)
    for code, text in legal_notes(invoice, settings):
        if code in ("PMD", "PMT", "AAB"):
            mentions.append(text)
    for text in mentions:
        story.append(_p(text, s["base"]))
    if invoice.note and not invoice.credited_invoice_id:
        story.append(_p(invoice.note, s["base"]))

    identity = " · ".join(filter(None, [
        " ".join(filter(None, [seller.get("name"), seller.get("legal_form"),
                               f"au capital de {seller['capital']}" if seller.get("capital") else ""])),
        f"SIREN {seller['siren']}" if seller.get("siren") else "", f"SIRET {seller['siret']}" if seller.get("siret") else "",
        seller.get("rcs"), f"TVA {seller['vat_number']}" if seller.get("vat_number") else ""]))
    footer = "\n".join(filter(None, [identity, settings.footer]))

    def page_footer(canvas, document):
        canvas.saveState()
        canvas.setFont(FONT, 7)
        canvas.setFillColor(colors.HexColor("#6b7280"))
        y = 10 * mm
        for text in reversed(footer.splitlines()):
            canvas.drawCentredString(A4[0] / 2, y, text[:180])
            y += 3.2 * mm
        canvas.drawRightString(A4[0] - 18 * mm, 6 * mm, f"{invoice.number} - page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    return buffer.getvalue()


def _srgb_profile() -> bytes:
    from PIL import ImageCms
    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _add_output_intent(pdf: bytes) -> bytes:
    """Profil de couleurs sRGB (OutputIntent) exigé par le PDF/A."""
    reader = PdfReader(io.BytesIO(pdf))
    writer = PdfWriter(clone_from=reader)
    icc = StreamObject()
    icc._data = _srgb_profile()
    icc.update({NameObject("/N"): NumberObject(3)})
    intent = DictionaryObject({
        NameObject("/Type"): NameObject("/OutputIntent"), NameObject("/S"): NameObject("/GTS_PDFA1"),
        NameObject("/OutputConditionIdentifier"): TextStringObject("sRGB IEC61966-2.1"),
        NameObject("/Info"): TextStringObject("sRGB IEC61966-2.1"),
        NameObject("/DestOutputProfile"): writer._add_object(icc),
    })
    writer._root_object[NameObject("/OutputIntents")] = ArrayObject([writer._add_object(intent)])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def build_facturx_pdf(invoice) -> bytes:
    settings = InvoicingSettings.get()
    xml = build_cii(invoice, settings)
    facturx.xml_check_xsd(xml, flavor="factur-x", level="en16931")  # lève une exception si le XML est invalide
    pdf = _add_output_intent(render_pdf(invoice, settings))
    title = f"{'Avoir' if invoice.is_credit_note else 'Facture'} {invoice.number}"
    return facturx.generate_from_binary(
        pdf, xml, flavor="factur-x", level="en16931", check_xsd=False, lang="fr-FR",
        pdf_metadata={"author": invoice.seller.get("name", ""), "keywords": "Factur-X, facture", "title": title,
                      "subject": f"{title} - {invoice.buyer_name}"})
