"""
XML de la facture au format UN/CEFACT CII, profil Factur-X EN 16931.

L'ordre des éléments suit le schéma XSD (vérifié à chaque génération).
"""
from decimal import Decimal

from lxml import etree

NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "qdt": "urn:un:unece:uncefact:data:standard:QualifiedDataType:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}
GUIDELINE = "urn:cen.eu:en16931:2017"
TYPE_CODES = {"invoice": "380", "credit_note": "381"}


def _el(parent, tag, text=None, **attrs):
    prefix, name = tag.split(":")
    element = etree.SubElement(parent, f"{{{NS[prefix]}}}{name}", **attrs)
    if text is not None:
        element.text = str(text)
    return element


def _amount(value, places=2):
    return f"{Decimal(value):.{places}f}"


def _percent(rate):
    return f"{(Decimal(rate) * 100).normalize():f}"


def _date(parent, tag, day, qualified=False):
    holder = _el(parent, tag)
    _el(holder, "qdt:DateTimeString" if qualified else "udt:DateTimeString", f"{day:%Y%m%d}", format="102")


def _category(rate):
    return "S" if Decimal(rate) > 0 else "E"


def _tax(parent, rate, exemption):
    tax = _el(parent, "ram:CategoryTradeTax" if parent.tag.endswith("AllowanceCharge") else "ram:ApplicableTradeTax")
    _el(tax, "ram:TypeCode", "VAT")
    _el(tax, "ram:CategoryCode", _category(rate))
    _el(tax, "ram:RateApplicablePercent", _percent(rate))
    return tax


def _party(parent, tag, name, address, postal_code, city, country, siren="", vat_number="", email=""):
    party = _el(parent, tag)
    _el(party, "ram:Name", name)
    if siren:
        organization = _el(party, "ram:SpecifiedLegalOrganization")
        _el(organization, "ram:ID", siren, schemeID="0002")  # 0002 : SIREN
    postal = _el(party, "ram:PostalTradeAddress")
    if postal_code:
        _el(postal, "ram:PostcodeCode", postal_code)
    if address:
        _el(postal, "ram:LineOne", address)
    if city:
        _el(postal, "ram:CityName", city)
    _el(postal, "ram:CountryID", (country or "FR").upper())
    if email:
        uri = _el(party, "ram:URIUniversalCommunication")
        _el(uri, "ram:URIID", email, schemeID="EM")
    if vat_number:
        registration = _el(party, "ram:SpecifiedTaxRegistration")
        _el(registration, "ram:ID", vat_number.replace(" ", ""), schemeID="VA")
    return party


def legal_notes(invoice, settings):
    """Mentions réglementaires (code de sujet UNTDID 4451) : capital et RCS, pénalités, indemnité, escompte."""
    seller = invoice.seller
    notes = []
    identity = " ".join(filter(None, [seller.get("legal_form"), f"au capital de {seller['capital']}" if seller.get("capital") else "",
                                      seller.get("rcs")]))
    if identity:
        notes.append(("REG", f"{seller.get('name', '')} {identity}".strip()))
    if invoice.is_business and settings.late_penalties:
        notes.append(("PMD", settings.late_penalties))
        notes.append(("PMT", "Indemnité forfaitaire pour frais de recouvrement : 40 €."))
        notes.append(("AAB", "Pas d'escompte pour paiement anticipé."))
    if invoice.note:
        notes.append(("", invoice.note))
    return notes


def build_cii(invoice, settings) -> bytes:
    seller = invoice.seller
    root = etree.Element(f"{{{NS['rsm']}}}CrossIndustryInvoice", nsmap=NS)
    context = _el(root, "rsm:ExchangedDocumentContext")
    _el(_el(context, "ram:GuidelineSpecifiedDocumentContextParameter"), "ram:ID", GUIDELINE)

    document = _el(root, "rsm:ExchangedDocument")
    _el(document, "ram:ID", invoice.number)
    _el(document, "ram:TypeCode", TYPE_CODES[invoice.kind])
    _date(document, "ram:IssueDateTime", invoice.issue_date)
    for code, content in legal_notes(invoice, settings):
        note = _el(document, "ram:IncludedNote")
        _el(note, "ram:Content", content)
        if code:
            _el(note, "ram:SubjectCode", code)

    transaction = _el(root, "rsm:SupplyChainTradeTransaction")
    for line in invoice.lines.all():
        item = _el(transaction, "ram:IncludedSupplyChainTradeLineItem")
        _el(_el(item, "ram:AssociatedDocumentLineDocument"), "ram:LineID", line.position)
        _el(_el(item, "ram:SpecifiedTradeProduct"), "ram:Name", line.description)
        price = _el(_el(item, "ram:SpecifiedLineTradeAgreement"), "ram:NetPriceProductTradePrice")
        _el(price, "ram:ChargeAmount", f"{line.net_price.normalize():f}" if line.net_price else "0")
        _el(_el(item, "ram:SpecifiedLineTradeDelivery"), "ram:BilledQuantity", f"{line.quantity.normalize():f}",
            unitCode=line.unit)
        settlement = _el(item, "ram:SpecifiedLineTradeSettlement")
        _tax(settlement, line.vat_rate, settings.vat_exemption)
        _el(_el(settlement, "ram:SpecifiedTradeSettlementLineMonetarySummation"), "ram:LineTotalAmount",
            _amount(line.net_amount))

    agreement = _el(transaction, "ram:ApplicableHeaderTradeAgreement")
    if invoice.buyer_reference:
        _el(agreement, "ram:BuyerReference", invoice.buyer_reference)
    _party(agreement, "ram:SellerTradeParty", seller.get("name", ""), seller.get("address", ""), seller.get("postal_code", ""),
           seller.get("city", ""), seller.get("country", "FR"), seller.get("siren", ""), seller.get("vat_number", ""),
           seller.get("email", ""))
    _party(agreement, "ram:BuyerTradeParty", invoice.buyer_name, invoice.buyer_address, invoice.buyer_postal_code,
           invoice.buyer_city, invoice.buyer_country, invoice.buyer_siren, invoice.buyer_vat_number, invoice.buyer_email)
    if invoice.buyer_reference:
        _el(_el(agreement, "ram:BuyerOrderReferencedDocument"), "ram:IssuerAssignedID", invoice.buyer_reference)

    delivery = _el(transaction, "ram:ApplicableHeaderTradeDelivery")
    if invoice.sale_date:
        _date(_el(delivery, "ram:ActualDeliverySupplyChainEvent"), "ram:OccurrenceDateTime", invoice.sale_date)

    settlement = _el(transaction, "ram:ApplicableHeaderTradeSettlement")
    _el(settlement, "ram:InvoiceCurrencyCode", invoice.currency)
    if seller.get("iban") and not invoice.paid_at:
        means = _el(settlement, "ram:SpecifiedTradeSettlementPaymentMeans")
        _el(means, "ram:TypeCode", "58")  # virement SEPA
        _el(_el(means, "ram:PayeePartyCreditorFinancialAccount"), "ram:IBANID", seller["iban"].replace(" ", ""))
    for row in invoice.vat_breakdown:
        tax = _el(settlement, "ram:ApplicableTradeTax")
        _el(tax, "ram:CalculatedAmount", _amount(row["vat"]))
        _el(tax, "ram:TypeCode", "VAT")
        if _category(row["rate"]) == "E":
            _el(tax, "ram:ExemptionReason", settings.vat_exemption or "Exonération de TVA")
        _el(tax, "ram:BasisAmount", _amount(row["base"]))
        _el(tax, "ram:CategoryCode", _category(row["rate"]))
        if settings.vat_on_debits:
            _el(tax, "ram:DueDateTypeCode", "5")  # TVA exigible à la date de facturation (option pour les débits)
        _el(tax, "ram:RateApplicablePercent", _percent(row["rate"]))
    for allowance in invoice.allowances:
        charge = _el(settlement, "ram:SpecifiedTradeAllowanceCharge")
        _el(_el(charge, "ram:ChargeIndicator"), "udt:Indicator", "false")
        _el(charge, "ram:ActualAmount", _amount(allowance["amount"]))
        _el(charge, "ram:Reason", allowance["label"])
        _tax(charge, allowance["vat_rate"], settings.vat_exemption)
    due = Decimal("0.00") if invoice.paid_at else invoice.total_ttc
    if due > 0:
        terms = _el(settlement, "ram:SpecifiedTradePaymentTerms")
        _el(terms, "ram:Description", invoice.payment_terms or "Paiement à réception")
        if invoice.due_date:
            _date(terms, "ram:DueDateDateTime", invoice.due_date)
    summation = _el(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    _el(summation, "ram:LineTotalAmount", _amount(invoice.lines_total))
    if invoice.allowances_total:
        _el(summation, "ram:AllowanceTotalAmount", _amount(invoice.allowances_total))
    _el(summation, "ram:TaxBasisTotalAmount", _amount(invoice.total_ht))
    _el(summation, "ram:TaxTotalAmount", _amount(invoice.total_vat), currencyID=invoice.currency)
    _el(summation, "ram:GrandTotalAmount", _amount(invoice.total_ttc))
    if invoice.paid_at:
        _el(summation, "ram:TotalPrepaidAmount", _amount(invoice.total_ttc))
    _el(summation, "ram:DuePayableAmount", _amount(due))
    if invoice.credited_invoice_id:
        original = invoice.credited_invoice
        reference = _el(settlement, "ram:InvoiceReferencedDocument")
        _el(reference, "ram:IssuerAssignedID", original.number)
        _date(reference, "ram:FormattedIssueDateTime", original.issue_date, qualified=True)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)
