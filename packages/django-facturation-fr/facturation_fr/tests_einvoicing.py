"""Facturation électronique : aiguillage, plateforme, cycle de vie, factures reçues, e-reporting."""
from datetime import date
from decimal import Decimal as D
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from . import api, einvoicing, ereporting, lifecycle
from .models import EReport, IncomingInvoice, Invoice, InvoicingSettings, Transmission
from .platforms import sandbox
from .tests import COMPANY, CUSTOMER, configure_seller

MEDIA = tempfile.mkdtemp(prefix="test-einvoicing-")
RECEIVED = []


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)


def record_hook(incoming, code):
    RECEIVED.append((incoming.number, code))


def use_platform(code):
    InvoicingSettings.objects.filter(pk=1).update(platform=code)


@override_settings(MEDIA_ROOT=MEDIA, INVOICING={})
class OutgoingTest(TestCase):
    def setUp(self):
        configure_seller()
        sandbox.SENT_STATUSES.clear()

    def b2b(self, key="b2b:1", buyer=COMPANY, **extra):
        return api.issue_invoice(key, buyer, [api.Item("Prestation", 1, D("120.00"))], **extra)

    def test_routing(self):
        self.assertEqual(einvoicing.route(self.b2b()), "einvoice")
        self.assertEqual(einvoicing.route(api.issue_invoice("b2c", CUSTOMER, [api.Item("A", 1, D("10"))])), "ereporting")
        foreign = api.Buyer("GmbH", "Hauptstr. 1", "10115", "Berlin", country="DE", siren="", vat_number="DE123456789")
        self.assertEqual(einvoicing.route(api.issue_invoice("de", foreign, [api.Item("A", 1, D("10"))])), "ereporting")

    def test_sandbox_lifecycle_until_cashed(self):
        use_platform("sandbox")
        invoice = self.b2b(paid_at=date(2026, 10, 2), payment_method="Virement")
        self.assertEqual(einvoicing.sync()["deposited"], 1)
        transmission = Transmission.objects.get(invoice=invoice)
        self.assertEqual(transmission.status, lifecycle.DEPOSITED)
        einvoicing.sync()
        einvoicing.sync()
        einvoicing.sync()
        transmission.refresh_from_db()
        self.assertEqual(transmission.status, lifecycle.AVAILABLE)
        self.assertTrue(transmission.payment_reported)
        self.assertIn(("212", invoice.number, "120.00", "2026-10-02"), sandbox.SENT_STATUSES)
        self.assertEqual([e.code for e in transmission.events.all()], [200, 212, 201, 202, 203])
        self.assertEqual(einvoicing.sync()["events"], 0)  # plus rien à faire

    def test_unknown_recipient_is_rejected(self):
        use_platform("sandbox")
        invoice = self.b2b(buyer=api.Buyer("Inconnu", "1 rue", "75001", "Paris", siren="000000000"))
        einvoicing.sync()
        einvoicing.sync()
        transmission = Transmission.objects.get(invoice=invoice)
        self.assertEqual((transmission.status, transmission.error), (lifecycle.REJECTED, "Destinataire inconnu de l'annuaire"))

    def test_manual_platform(self):
        invoice = self.b2b(paid_at=date(2026, 10, 2))
        einvoicing.sync()
        transmission = Transmission.objects.get(invoice=invoice)
        self.assertIsNone(transmission.status)  # à déposer par l'utilisateur
        self.assertFalse(transmission.payment_reported)
        einvoicing.mark_deposited(transmission, external_id="PA-123")
        einvoicing.sync()
        transmission.refresh_from_db()
        self.assertEqual((transmission.status, transmission.external_id, transmission.payment_reported),
                         (lifecycle.DEPOSITED, "PA-123", True))


@override_settings(MEDIA_ROOT=MEDIA, INVOICING={"ON_RECEIVED_STATUS": ["facturation_fr.tests_einvoicing.record_hook"]})
class IncomingTest(TestCase):
    def setUp(self):
        RECEIVED.clear()
        # Une facture « fournisseur » : produite par ce module pour un autre vendeur, adressée à notre société
        configure_seller(seller_name="Papeterie Martin", seller_siret="73282932000074", seller_vat_number="FR40732829320")
        supplier_invoice = api.issue_invoice(
            "fournisseur:1", api.Buyer("Atelier Dupont", "12 rue des Lilas", "69001", "Lyon", siren="123456789"),
            [api.Item("Ramettes de papier", 10, D("4.50"))],
            prices_include_tax=False, due_date=date(2026, 11, 1), payment_terms="30 jours")
        self.content = supplier_invoice.pdf.read()
        configure_seller()  # retour à notre identité (SIREN 123456789)

    def test_receive_factur_x(self):
        incoming = einvoicing.receive(self.content, "facture.pdf")
        self.assertEqual((incoming.seller_name, incoming.seller_siren, incoming.buyer_siren),
                         ("Papeterie Martin", "732829320", "123456789"))
        self.assertEqual((incoming.total_ht, incoming.total_vat, incoming.total_ttc), (D("45.00"), D("9.00"), D("54.00")))
        self.assertEqual((incoming.due_date, incoming.status, incoming.flavor), (date(2026, 11, 1), 203, "factur-x"))
        self.assertEqual(incoming.vat_breakdown, [{"rate": "0.2", "base": "45.00", "vat": "9.00", "category": "S"}])
        self.assertEqual(incoming.status_reason, "")
        self.assertEqual(einvoicing.receive(self.content, "doublon.pdf").pk, incoming.pk)

    def test_receive_bare_cii_xml_and_wrong_recipient(self):
        import facturx
        _, xml = facturx.get_xml_from_pdf(self.content)
        InvoicingSettings.objects.filter(pk=1).update(seller_siret="55203253400646")
        incoming = einvoicing.receive(xml, "facture.xml")
        self.assertEqual(incoming.flavor, "factur-x")
        self.assertIn("Destinataire différent", incoming.status_reason)

    def test_unreadable_files(self):
        for content in (b"%PDF-1.4 pas une facture", b"<html></html>", b"\x00\x01"):
            with self.subTest(content), self.assertRaises(einvoicing.EInvoicingError):
                einvoicing.receive(content, "x")

    def test_buyer_statuses(self):
        incoming = einvoicing.receive(self.content, "facture.pdf")
        with self.assertRaisesMessage(einvoicing.EInvoicingError, "motivé"):
            einvoicing.set_incoming_status(incoming, lifecycle.REFUSED)
        with self.assertRaises(einvoicing.EInvoicingError):
            einvoicing.set_incoming_status(incoming, lifecycle.CASHED)  # statut du vendeur
        einvoicing.set_incoming_status(incoming, lifecycle.APPROVED)
        self.assertEqual(RECEIVED, [(incoming.number, 205)])
        einvoicing.set_incoming_status(incoming, lifecycle.REFUSED, "Livraison incomplète")
        with self.assertRaisesMessage(einvoicing.EInvoicingError, "définitif"):
            einvoicing.set_incoming_status(incoming, lifecycle.APPROVED)
        self.assertEqual([e.code for e in incoming.events.all()], [203, 205, 210])

    def test_sandbox_inbox_and_buyer_status_sent(self):
        use_platform("sandbox")
        sandbox.SENT_STATUSES.clear()
        sandbox.INBOX.append(("facture.pdf", self.content))
        self.assertEqual(einvoicing.sync()["received"], 1)
        incoming = IncomingInvoice.objects.get()
        self.assertEqual(incoming.platform, "sandbox")
        einvoicing.set_incoming_status(incoming, lifecycle.DISPUTED, "Prix différent du devis")
        self.assertIn(("207", incoming.number, "Prix différent du devis"), sandbox.SENT_STATUSES)


GERMAN = api.Buyer("Muster GmbH", "Hauptstr. 1", "10115", "Berlin", country="DE", vat_number="DE123456789")
SWISS = api.Buyer("Uhren AG", "Bahnhofstr. 3", "8001", "Zurich", country="CH", vat_number="CHE-123.456.789")


def configure_ereporting(**extra):
    InvoicingSettings.objects.filter(pk=1).update(
        platform_registration="AB12", platform_company="Plateforme Exemple SAS", **extra)


def xml_of(report):
    from lxml import etree
    return etree.fromstring(report.xml.encode())


@override_settings(MEDIA_ROOT=MEDIA, INVOICING={})
class EReportingTest(TestCase):
    def setUp(self):
        configure_seller()
        configure_ereporting()

    def test_periods(self):
        bounds = einvoicing.period_bounds
        self.assertEqual(bounds(date(2026, 2, 25), "decade"), (date(2026, 2, 21), date(2026, 2, 28)))
        self.assertEqual(bounds(date(2026, 2, 5), "decade"), (date(2026, 2, 1), date(2026, 2, 10)))
        self.assertEqual(bounds(date(2026, 2, 15), "month"), (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(bounds(date(2026, 4, 3), "bimonth"), (date(2026, 3, 1), date(2026, 4, 30)))
        regimes = {"real_monthly": ("decade", "month"), "real_quarterly": ("month", "month"),
                   "simplified": ("month", "month"), "franchise": ("bimonth", "bimonth")}
        for regime, expected in regimes.items():
            InvoicingSettings.objects.filter(pk=1).update(vat_regime=regime)
            self.assertEqual(tuple(ereporting.periods().values()), expected, regime)
        InvoicingSettings.objects.filter(pk=1).update(vat_regime="real_monthly")
        self.assertEqual(ereporting.last_closed("payments", date(2026, 10, 2)), (date(2026, 9, 1), date(2026, 9, 30)))

    def test_b2c_transactions_by_day_category_and_rate(self):
        day = date(2026, 9, 3)
        api.issue_invoice("c1", CUSTOMER, [api.Item("T-shirt", 1, D("24.00")), api.Item("Livre", 1, D("10.55"), D("0.055")),
                                           api.Item("Atelier couture", 1, D("60.00"), nature="services")], issue_date=day)
        api.issue_invoice("c2", CUSTOMER, [api.Item("T-shirt", 1, D("12.00"))], issue_date=day)
        api.issue_invoice("pro", COMPANY, [api.Item("Lot", 1, D("120.00"))], issue_date=day)  # facture électronique : exclue
        api.credit_invoice("c2:credit", Invoice.objects.get(key="c2"), issue_date=date(2026, 9, 4))
        report = einvoicing.build_ereport(date(2026, 9, 1), date(2026, 9, 10))
        rows = {(r["date"], r["category"], r["rate"]): (r["base"], r["vat"], r["count"]) for r in report.rows}
        self.assertEqual(rows, {
            ("2026-09-03", "TLB1", "0.2000"): ("30.00", "6.00", 2),
            ("2026-09-03", "TLB1", "0.0550"): ("10.00", "0.55", 2),
            ("2026-09-03", "TPS1", "0.2000"): ("50.00", "10.00", 1),
            ("2026-09-04", "TLB1", "0.2000"): ("-10.00", "-2.00", 1),
        })
        self.assertEqual((report.total_ht, report.total_vat, report.type_code, report.error), (D("80.00"), D("14.55"), "IN", ""))
        root = xml_of(report)  # validé contre le XSD officiel à la génération
        self.assertEqual(root.tag, "Report")
        self.assertEqual(root.findtext("ReportDocument/TypeCode"), "IN")
        self.assertEqual(root.find("ReportDocument/Sender/Id").attrib, {"schemeId": "0238"})
        self.assertEqual(root.findtext("ReportDocument/Sender/Id"), "AB12")
        self.assertEqual((root.findtext("ReportDocument/Issuer/Id"), root.findtext("ReportDocument/Issuer/RoleCode")),
                         ("123456789", "SE"))
        self.assertEqual(root.findtext("TransactionsReport/ReportPeriod/StartDate"), "20260901")
        self.assertIsNone(root.find("PaymentsReport"))  # deux transmissions distinctes (G6.29)
        first = root.find("TransactionsReport/Transactions")
        self.assertEqual([first.findtext(t) for t in ("Date", "TransactionsCurrency", "CategoryCode", "TaxExclusiveAmount",
                                                      "TaxTotal", "TransactionsCount")],
                         ["20260903", "EUR", "TLB1", "40.00", "6.55", "2"])
        self.assertEqual([t.findtext("TaxPercent") for t in first.findall("TaxSubtotal")], ["20", "5.5"])

    def test_foreign_business_invoice_is_reported_one_by_one(self):
        invoice = api.issue_invoice("de", GERMAN, [api.Item("Lot", 2, D("50.00"), D("0"))], prices_include_tax=False,
                                    issue_date=date(2026, 9, 5), sale_date=date(2026, 9, 2), paid_at=date(2026, 9, 5))
        api.credit_invoice("de:credit", invoice, issue_date=date(2026, 9, 8))
        api.issue_invoice("ch", SWISS, [api.Item("Montre", 1, D("300.00"), D("0"))], prices_include_tax=False,
                          issue_date=date(2026, 9, 6))
        report = einvoicing.build_ereport(date(2026, 9, 1), date(2026, 9, 10))
        self.assertEqual([(r["flow"], r["invoice"], r["base"]) for r in report.rows],
                         [("10.1", invoice.number, "100.00"), ("10.1", Invoice.objects.get(key="ch").number, "300.00"),
                          ("10.1", "AV2026-00001", "-100.00")])  # par date d'émission
        german, swiss, credit = xml_of(report).findall("TransactionsReport/Invoice")
        self.assertEqual([german.findtext(t) for t in ("ID", "IssueDate", "TypeCode", "BusinessProcess/ID",
                                                       "BusinessProcess/TypeID", "Delivery/Date")],
                         [invoice.number, "20260905", "380", "B2", "urn.cpro.gouv.fr:1p0:ereporting", "20260902"])
        self.assertEqual((german.find("Buyer/CompanyId").attrib["schemeId"], german.findtext("Buyer/TaxRegistrationId")),
                         ("0223", "DE123456789"))
        self.assertEqual(german.findtext("TaxSubTotal/TaxCategory/Code"), "K")
        self.assertEqual((credit.findtext("TypeCode"), credit.findtext("ReferencedDocument/ID")), ("381", invoice.number))
        self.assertEqual((swiss.findtext("Buyer/CompanyId"), swiss.find("Buyer/CompanyId").attrib["schemeId"],
                          swiss.findtext("TaxSubTotal/TaxCategory/Code")), ("CHUhren AG", "0227", "G"))

    def test_foreign_business_invoice_lines_and_allowances(self):
        invoice = api.issue_invoice(
            "de", GERMAN, [api.Item("Lot", 2, D("50.00"), D("0")), api.Item("Câble", D("1.5"), D("3.333333"), D("0"), unit="MTR")],
            [api.Allowance("Remise fidélité", D("10.00"), D("0"))], prices_include_tax=False, issue_date=date(2026, 9, 5))
        element = xml_of(einvoicing.build_ereport(date(2026, 9, 1), date(2026, 9, 10))).find("TransactionsReport/Invoice")
        self.assertEqual([child.tag for child in element][-5:], ["AllowanceCharge", "MonetaryTotal", "TaxSubTotal", "Line", "Line"])
        allowance = element.find("AllowanceCharge")
        self.assertEqual((allowance.get("ChargeIndicator"), allowance.findtext("Amount"), allowance.findtext("TaxCategoryCode"),
                          allowance.findtext("TaxPercent")), ("false", "10.00", "K", "0"))
        self.assertEqual(element.findtext("MonetaryTotal/TaxExclusiveAmount"), str(invoice.total_ht))
        lines = [(line.findtext("BilledQuantity"), line.find("BilledQuantity").get("UnitCode"), line.findtext("Price/PriceAmount"),
                  line.findtext("Product/Name")) for line in element.findall("Line")]
        self.assertEqual(lines, [("2", "C62", "50", "Lot"), ("1.5", "MTR", "3.333333", "Câble")])

    def test_payments_of_services_only(self):
        day = date(2026, 9, 12)
        api.issue_invoice("goods", CUSTOMER, [api.Item("T-shirt", 1, D("24.00"))], issue_date=day, paid_at=day)
        mixed = api.issue_invoice("mixed", CUSTOMER, [api.Item("T-shirt", 1, D("24.00")),
                                                      api.Item("Atelier", 1, D("60.00"), nature="services")],
                                  issue_date=day, paid_at=day)
        api.credit_invoice("mixed:credit", mixed, issue_date=date(2026, 9, 20), paid_at=date(2026, 9, 20))
        api.issue_invoice("de", GERMAN, [api.Item("Audit", 1, D("500.00"), D("0"), nature="services")],
                          prices_include_tax=False, issue_date=day, paid_at=date(2026, 9, 15))
        api.issue_invoice("pro", COMPANY, [api.Item("Conseil", 1, D("100"), nature="services")], issue_date=day, paid_at=day)
        report = einvoicing.build_ereport(date(2026, 9, 1), date(2026, 9, 30), kind=EReport.PAYMENTS)
        self.assertEqual([(r["flow"], r["date"], r["rate"], r["amount"]) for r in report.rows], [
            ("10.2", "2026-09-15", "0.0000", "500.00"),
            ("10.4", "2026-09-12", "0.2000", "60.00"),
            ("10.4", "2026-09-20", "0.2000", "-60.00")])
        self.assertEqual(report.total_paid, D("500.00"))
        root = xml_of(report)
        self.assertIsNone(root.find("TransactionsReport"))
        self.assertEqual([root.findtext(f"PaymentsReport/Invoice/{t}") for t in ("IssueDate", "Payment/Date", "Payment/SubTotals/Amount")],
                         ["20260912", "20260915", "500.00"])
        days = root.findall("PaymentsReport/Transactions/Payment")
        self.assertEqual([(p.findtext("Date"), p.findtext("SubTotals/TaxPercent"), p.findtext("SubTotals/Amount")) for p in days],
                         [("20260912", "20", "60.00"), ("20260920", "20", "-60.00")])
        InvoicingSettings.objects.filter(pk=1).update(vat_on_debits=True)  # TVA exigible à la facturation
        self.assertEqual(einvoicing.build_ereport(date(2026, 9, 1), date(2026, 9, 30), kind=EReport.PAYMENTS).rows, [])

    def test_missing_platform_registration_blocks_the_flow(self):
        InvoicingSettings.objects.filter(pk=1).update(platform_registration="")
        api.issue_invoice("c1", CUSTOMER, [api.Item("A", 1, D("12.00"))], issue_date=date(2026, 9, 2))
        report = einvoicing.build_ereport(date(2026, 9, 1), date(2026, 9, 10))
        self.assertEqual(report.xml, "")
        self.assertIn("matricule de la plateforme agréée", report.error)
        self.assertIsNone(einvoicing.transmit_ereport(report, today=date(2026, 10, 1)).transmitted_at)

    def test_sync_transmits_closed_periods_then_a_rectification(self):
        use_platform("sandbox")
        InvoicingSettings.objects.filter(pk=1).update(platform_registration="", platform_company="")  # fournis par le connecteur
        api.issue_invoice("c1", CUSTOMER, [api.Item("A", 1, D("12.00")), api.Item("Cours", 1, D("24.00"), nature="services")],
                          issue_date=date(2026, 9, 25), paid_at=date(2026, 9, 25))
        self.assertEqual(einvoicing.sync(today=date(2026, 10, 2))["ereports"], 2)  # transactions et encaissements
        transactions = EReport.objects.get(kind="transactions", period_start=date(2026, 9, 21))
        payments = EReport.objects.get(kind="payments", period_start=date(2026, 9, 1))
        self.assertEqual((transactions.period_end, transactions.external_id), (date(2026, 9, 30), "SBX-ER-T20260921-V1"))
        self.assertEqual(payments.total_paid, D("24.00"))
        self.assertEqual(xml_of(transactions).findtext("ReportDocument/Sender/Id"), "0000")
        self.assertEqual(einvoicing.sync(today=date(2026, 10, 3))["ereports"], 0)  # rien de nouveau
        # Facture de la période close émise après coup : rectificative qui annule et remplace
        api.issue_invoice("c2", CUSTOMER, [api.Item("B", 1, D("12.00"))], issue_date=date(2026, 9, 26))
        self.assertEqual(einvoicing.sync(today=date(2026, 10, 4))["ereports"], 1)
        transactions.refresh_from_db()
        self.assertEqual((transactions.type_code, transactions.version, transactions.total_ht), ("RE", 2, D("40.00")))
        self.assertEqual(transactions.history[0]["transmission_id"], "ER-123456789-T20260921-20260930-V1")
        self.assertEqual(xml_of(transactions).findtext("ReportDocument/TypeCode"), "RE")
        self.assertEqual(xml_of(transactions).findtext("ReportDocument/Id"), "ER-123456789-T20260921-20260930-V2")
        self.assertIn(("ereport", "transactions", "RE", transactions.transmission_id), sandbox.SENT_STATUSES)

    def test_current_period_is_not_transmitted(self):
        api.issue_invoice("c1", CUSTOMER, [api.Item("A", 1, D("12.00"))], issue_date=date(2026, 9, 2))
        report = einvoicing.build_ereport(date(2026, 9, 1), date(2026, 9, 10))
        einvoicing.transmit_ereport(report, user=object(), today=date(2026, 9, 10))
        self.assertIsNone(report.transmitted_at)
        self.assertIn("Période en cours", report.error)


@override_settings(MEDIA_ROOT=MEDIA, INVOICING={})
class PagesTest(TestCase):
    def setUp(self):
        configure_seller(seller_name="Fournisseur SAS", seller_siret="73282932000074")
        self.supplier_pdf = api.issue_invoice("f", api.Buyer("Nous", "1 rue", "69001", "Lyon", siren="123456789"),
                                              [api.Item("Carton", 1, D("10"))]).pdf.read()
        configure_seller()
        user = get_user_model().objects.create_user(username="c@x.fr", email="c@x.fr", password="x" * 12)
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label="invoicing"))
        self.client.force_login(user)

    def test_upload_review_and_approve(self):
        response = self.client.post(reverse("invoicing:incoming_upload"),
                                    {"files": [SimpleUploadedFile("f.pdf", self.supplier_pdf, "application/pdf"),
                                               SimpleUploadedFile("x.pdf", b"%PDF-1.4 rien", "application/pdf")]}, follow=True)
        self.assertContains(response, "1 facture(s) importée(s)")
        self.assertContains(response, "x.pdf")
        incoming = IncomingInvoice.objects.get()
        self.assertContains(self.client.get(reverse("invoicing:incoming_detail", args=[incoming.pk])), "Fournisseur SAS")
        self.client.post(reverse("invoicing:incoming_status", args=[incoming.pk]), {"status": "205", "expense_account": "606000"})
        incoming.refresh_from_db()
        self.assertEqual((incoming.status, incoming.expense_account), (205, "606000"))
        self.assertEqual(self.client.get(reverse("invoicing:incoming_file", args=[incoming.pk]))["Content-Type"], "application/pdf")

    def test_dashboard_manual_deposit_and_ereport_csv(self):
        invoice = api.issue_invoice("pro", COMPANY, [api.Item("Lot", 1, D("120"))])
        self.client.post(reverse("invoicing:sync"))
        transmission = Transmission.objects.get(invoice=invoice)
        page = self.client.get(reverse("invoicing:einvoicing"))
        self.assertContains(page, "Dépôt manuel")
        self.assertContains(page, invoice.number)
        self.client.post(reverse("invoicing:transmission_status", args=[transmission.pk]), {"external_id": "PA-9", "status": ""})
        transmission.refresh_from_db()
        self.assertEqual(transmission.status, 200)
        configure_ereporting()
        api.issue_invoice("b2c", CUSTOMER, [api.Item("A", 1, D("12"))], issue_date=date(2026, 9, 2))
        report = einvoicing.build_ereport(date(2026, 9, 1), date(2026, 9, 10))
        self.assertContains(self.client.get(reverse("invoicing:einvoicing")), "Marquer transmis")
        xml = self.client.get(reverse("invoicing:ereport_download", args=[report.pk]))
        self.assertEqual(xml["Content-Type"], "application/xml; charset=utf-8")
        self.assertIn(b"<CategoryCode>TLB1</CategoryCode>", xml.content)
        csv = self.client.get(reverse("invoicing:ereport_download", args=[report.pk]) + "?format=csv").content.decode("utf-8-sig")
        self.assertIn("10.3;;2026-09-02;TLB1;0,2000;10,00;2,00;;1", csv)
        self.client.post(reverse("invoicing:ereport_transmit", args=[report.pk]))
        self.assertIsNotNone(EReport.objects.get(pk=report.pk).transmitted_at)


class XmlSecurityTest(TestCase):
    def test_external_entities_are_not_resolved(self):
        import os
        secret = os.path.join(MEDIA, "secret.txt")
        with open(secret, "w") as f:
            f.write("MOT-DE-PASSE")
        xml = f"""<?xml version="1.0"?>
<!DOCTYPE x [<!ENTITY e SYSTEM "file:///{secret.replace(os.sep, '/')}">]>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
  xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100">
  <rsm:ExchangedDocument><ram:ID>&e;</ram:ID></rsm:ExchangedDocument></rsm:CrossIndustryInvoice>""".encode()
        try:
            data, _ = einvoicing.parse_incoming(xml)
        except einvoicing.EInvoicingError:
            return  # refusé : très bien aussi
        self.assertNotIn("MOT-DE-PASSE", str(data))
