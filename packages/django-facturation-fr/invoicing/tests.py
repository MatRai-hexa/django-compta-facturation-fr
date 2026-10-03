"""Tests de la facturation, indépendants de tout projet hôte."""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal as D
import io
import shutil
import tempfile

import facturx
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from lxml import etree
from pypdf import PdfReader

from . import api
from .amounts import Allowance, Item, compute, split_ttc
from .models import Invoice, InvoicingSettings

MEDIA = tempfile.mkdtemp(prefix="test-factures-")
NS = {"rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
      "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"}


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)


def configure_seller(**extra):
    s = InvoicingSettings.get()
    for key, value in {"seller_name": "Atelier Dupont", "seller_legal_form": "SAS", "seller_capital": "10 000 €",
                       "seller_address": "12 rue des Lilas", "seller_postal_code": "69001", "seller_city": "Lyon",
                       "seller_siret": "12345678900011", "seller_vat_number": "FR32123456789",
                       "seller_rcs": "RCS Lyon 123 456 789", "seller_email": "contact@dupont.example", **extra}.items():
        setattr(s, key, value)
    s.save()


CUSTOMER = api.Buyer("Camille Martin", "1 rue de la Paix", "75002", "Paris", email="camille@example.com")
COMPANY = api.Buyer("Société Martin SARL", "5 avenue Foch", "75016", "Paris", siren="987654321",
                    vat_number="FR12987654321", reference="CMD-42")


def basket():
    return ([Item("T-shirt", 2, D("25.00")), Item("Livre", 1, D("18.00"), D("0.0550")), Item("Port", 1, D("4.90"))],
            [Allowance("Code DIX", D("5.00")), Allowance("Code DIX", D("1.80"), D("0.0550"))])


def xml_of(invoice):
    _, xml = facturx.get_xml_from_pdf(invoice.pdf.read(), check_xsd=True)
    invoice.pdf.seek(0)
    return etree.fromstring(xml)


class AmountsTest(SimpleTestCase):
    def test_ttc_split_matches_accounting_and_br_co_17(self):
        for rate in ("0.2000", "0.1000", "0.0550", "0.0210"):
            for cents in range(1, 20001, 7):
                ttc = D(cents) / 100
                base, vat = split_ttc(ttc, D(rate))
                self.assertEqual(base + vat, ttc)
                self.assertEqual(base, (ttc / (1 + D(rate))).quantize(D("0.01"), rounding=ROUND_HALF_UP))
                self.assertLessEqual(abs(vat - (base * D(rate)).quantize(D("0.01"), rounding=ROUND_HALF_UP)), D("0.01"))

    def test_ttc_basket_with_allowances(self):
        result = compute(*basket())
        self.assertEqual(result.payable, D("66.10"))  # 50 + 18 + 4,90 - 6,80 : exactement le prix payé
        self.assertEqual([(r["rate"], r["base"], r["vat"]) for r in result.vat],
                         [(D("0.2000"), D("41.58"), D("8.32")), (D("0.0550"), D("15.36"), D("0.84"))])
        # BR-CO-13 / BR-CO-10 : base = lignes − remises, cohérence des totaux
        self.assertEqual(result.lines_total - result.allowances_total, result.total_ht)
        self.assertEqual(sum((l["net_amount"] for l in result.lines), D("0")), result.lines_total)

    def test_ht_prices(self):
        result = compute([Item("Prestation", 3, D("33.33"))], prices_include_tax=False)
        self.assertEqual((result.total_ht, result.total_vat, result.total_ttc), (D("99.99"), D("20.00"), D("119.99")))


@override_settings(MEDIA_ROOT=MEDIA, INVOICING={})
class IssueTest(TestCase):
    def setUp(self):
        configure_seller()

    def test_numbering_is_continuous_per_series_and_year(self):
        numbers = [api.issue_invoice(f"k{i}", CUSTOMER, [Item("A", 1, D("10"))], issue_date=date(2026, 3, i + 1)).number
                   for i in range(3)]
        self.assertEqual(numbers, ["F2026-00001", "F2026-00002", "F2026-00003"])
        nxt = api.issue_invoice("k-2027", CUSTOMER, [Item("A", 1, D("10"))], issue_date=date(2027, 1, 2))
        self.assertEqual(nxt.number, "F2027-00001")
        credit = api.credit_invoice("k0:credit", Invoice.objects.get(key="k0"))
        self.assertTrue(credit.number.startswith("AV"))

    def test_same_key_returns_the_same_invoice(self):
        first = api.issue_invoice("order:1", CUSTOMER, [Item("A", 1, D("10"))])
        second = api.issue_invoice("order:1", CUSTOMER, [Item("B", 9, D("99"))])
        self.assertEqual((first.pk, Invoice.objects.count()), (second.pk, 1))

    def test_issued_invoice_is_immutable(self):
        invoice = api.issue_invoice("order:1", CUSTOMER, [Item("A", 1, D("10"))])
        invoice.buyer_name = "Autre"
        with self.assertRaises(ValidationError):
            invoice.save()
        with self.assertRaises(ValidationError):
            invoice.delete()
        line = invoice.lines.get()
        line.net_amount = D("1")
        with self.assertRaises(ValidationError):
            line.save()

    def test_incomplete_seller_is_refused(self):
        InvoicingSettings.objects.filter(pk=1).update(seller_siret="", seller_city="")
        with self.assertRaisesMessage(api.InvoicingError, "ville, SIREN"):
            api.issue_invoice("x", CUSTOMER, [Item("A", 1, D("10"))])
        configure_seller(seller_vat_number="")
        with self.assertRaisesMessage(api.InvoicingError, "TVA intracommunautaire"):
            api.issue_invoice("x", CUSTOMER, [Item("A", 1, D("10"))])

    def test_vat_exempt_seller(self):
        configure_seller(seller_vat_number="", vat_exemption="TVA non applicable, art. 293 B du CGI")
        invoice = api.issue_invoice("x", CUSTOMER, [Item("Atelier", 1, D("40"), D("0"))])
        tax = xml_of(invoice).find(".//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax", NS)
        self.assertEqual(tax.findtext("ram:CategoryCode", namespaces=NS), "E")
        self.assertEqual(tax.findtext("ram:ExemptionReason", namespaces=NS), "TVA non applicable, art. 293 B du CGI")

    def test_credit_note_cancels_exactly(self):
        items, allowances = basket()
        invoice = api.issue_invoice("order:9", COMPANY, items, allowances, paid_at=date(2026, 10, 2))
        credit = api.credit_invoice("order:9:credit", invoice, reason="Retour du colis")
        self.assertEqual((credit.total_ht, credit.total_vat, credit.total_ttc), (invoice.total_ht, invoice.total_vat, invoice.total_ttc))
        self.assertEqual(credit.vat_breakdown, invoice.vat_breakdown)
        self.assertEqual(credit.credited_invoice, invoice)
        root = xml_of(credit)
        self.assertEqual(root.findtext(".//rsm:ExchangedDocument/ram:TypeCode", namespaces=NS), "381")
        self.assertEqual(root.findtext(".//ram:InvoiceReferencedDocument/ram:IssuerAssignedID", namespaces=NS), invoice.number)
        with self.assertRaises(api.InvoicingError):
            api.credit_invoice("again", credit)


@override_settings(MEDIA_ROOT=MEDIA, INVOICING={})
class FacturxTest(TestCase):
    def setUp(self):
        configure_seller()
        items, allowances = basket()
        self.invoice = api.issue_invoice("order:42", COMPANY, items, allowances, sale_date=date(2026, 10, 1),
                                         paid_at=date(2026, 10, 2), payment_method="Carte bancaire")

    def test_xml_is_en16931_and_matches_the_invoice(self):
        root = xml_of(self.invoice)
        self.assertEqual(facturx.get_level(root), "en16931")
        text = lambda path: root.findtext(path, namespaces=NS)  # noqa: E731
        self.assertEqual(text(".//rsm:ExchangedDocument/ram:ID"), self.invoice.number)
        self.assertEqual(text(".//ram:SellerTradeParty/ram:SpecifiedLegalOrganization/ram:ID"), "123456789")
        self.assertEqual(text(".//ram:SellerTradeParty/ram:SpecifiedTaxRegistration/ram:ID"), "FR32123456789")
        self.assertEqual(text(".//ram:BuyerTradeParty/ram:SpecifiedLegalOrganization/ram:ID"), "987654321")
        summation = ".//ram:SpecifiedTradeSettlementHeaderMonetarySummation/"
        self.assertEqual(text(summation + "ram:GrandTotalAmount"), "66.10")
        self.assertEqual(text(summation + "ram:TaxBasisTotalAmount"), "56.94")
        self.assertEqual(text(summation + "ram:AllowanceTotalAmount"), "5.88")
        self.assertEqual(text(summation + "ram:DuePayableAmount"), "0.00")
        notes = {n.findtext("ram:SubjectCode", namespaces=NS) for n in root.iterfind(".//ram:IncludedNote", NS)}
        self.assertTrue({"REG", "PMD", "PMT", "AAB"} <= notes)  # mentions obligatoires entre professionnels

    def test_unpaid_invoice_has_payment_terms(self):
        invoice = api.issue_invoice("cod:1", CUSTOMER, [Item("A", 1, D("10"))], payment_terms="À régler à la livraison.")
        root = xml_of(invoice)
        self.assertEqual(root.findtext(".//ram:SpecifiedTradePaymentTerms/ram:Description", namespaces=NS),
                         "À régler à la livraison.")
        self.assertEqual(root.findtext(".//ram:DuePayableAmount", namespaces=NS), "10.00")
        notes = {n.findtext("ram:SubjectCode", namespaces=NS) for n in root.iterfind(".//ram:IncludedNote", NS)}
        self.assertNotIn("PMD", notes)  # particulier : pas de pénalités professionnelles

    def test_pdf_a3_requirements(self):
        data = self.invoice.pdf.read()
        reader = PdfReader(io.BytesIO(data))
        root = reader.trailer["/Root"]
        self.assertIn("/OutputIntents", root)
        self.assertIn(b"pdfaid:part>3<", root["/Metadata"].get_object().get_data())
        for page in reader.pages:
            for font in page["/Resources"]["/Font"].values():
                descriptor = font.get_object()["/FontDescriptor"].get_object()
                self.assertIn("/FontFile2", descriptor)  # police intégrée
        import hashlib
        self.assertEqual(hashlib.sha256(data).hexdigest(), self.invoice.pdf_sha256)
        text = reader.pages[0].extract_text()
        for expected in ("Facture F2026-00001", "Société Martin SARL", "SIREN : 987654321", "Total TTC", "66,10 €",
                         "Facture acquittée le 02/10/2026", "Nature des opérations", "Indemnité forfaitaire"):
            self.assertIn(expected, text)


@override_settings(MEDIA_ROOT=MEDIA, INVOICING={})
class ViewsTest(TestCase):
    def setUp(self):
        configure_seller()
        self.invoice = api.issue_invoice("order:1", CUSTOMER, [Item("Vase", 1, D("30"))])
        self.user = get_user_model().objects.create_user(username="c@x.fr", email="c@x.fr", password="x" * 12)

    def login(self, *codenames):
        self.user.user_permissions.add(*Permission.objects.filter(content_type__app_label="invoicing", codename__in=codenames))
        self.client.force_login(self.user)

    def test_staff_pages(self):
        self.login("view_invoice", "add_invoice", "change_invoicingsettings")
        self.assertContains(self.client.get(reverse("invoicing:list")), self.invoice.number)
        self.assertContains(self.client.get(reverse("invoicing:detail", args=[self.invoice.pk])), "Camille Martin")
        pdf = self.client.get(reverse("invoicing:pdf", args=[self.invoice.pk]))
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertContains(self.client.get(reverse("invoicing:settings")), "Paramètres de facturation")
        response = self.client.post(reverse("invoicing:credit", args=[self.invoice.pk]), {"reason": "Erreur"})
        credit = Invoice.objects.get(kind=Invoice.Kind.CREDIT_NOTE)
        self.assertRedirects(response, reverse("invoicing:detail", args=[credit.pk]))
        self.client.post(reverse("invoicing:credit", args=[self.invoice.pk]))
        self.assertEqual(Invoice.objects.filter(kind=Invoice.Kind.CREDIT_NOTE).count(), 1)

    def test_permissions(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("invoicing:list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("invoicing:pdf", args=[self.invoice.pk])).status_code, 403)

    def test_public_link(self):
        response = self.client.get(reverse("invoicing:public_pdf", args=[self.invoice.public_token]))
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(self.client.get(reverse("invoicing:public_pdf", args=["mauvais-jeton"])).status_code, 404)


SELLER_FROM_PROJECT = {"name": "Boutique", "siret": "12345678900011", "siren": "123456789", "address": "1 rue",
                       "postal_code": "69001", "city": "Lyon", "vat_number": "FR32123456789"}


def project_seller():
    return SELLER_FROM_PROJECT


@override_settings(INVOICING={"SELLER": "invoicing.tests.project_seller"})
class SellerSourceTest(TestCase):
    def test_project_values_by_default(self):
        from . import conf
        self.assertEqual(conf.seller()["siren"], "123456789")

    def test_siret_entered_in_settings_drives_the_siren(self):
        from . import conf
        InvoicingSettings.objects.update_or_create(pk=1, defaults={"seller_siret": "73282932000074"})
        self.assertEqual((conf.seller()["siret"], conf.seller()["siren"]), ("73282932000074", "732829320"))
