"""Liquidation de la TVA et bilan."""
from datetime import date
from decimal import Decimal as D

from django.test import TestCase, override_settings
from django.urls import reverse

from . import api, reports, seal, vat
from .models import Account, AccountingSettings, FiscalPeriod, LedgerEntry, Transaction
from .posting import AccountingError, Line, close_period, generate_opening_entries, post_entry
from .tests import accountant

CUSTOMER = api.Party("C001", "Dupont SARL")
SUPPLIER = api.Party("F001", "Grossiste SA")


def acc(code):
    return Account.objects.get(code=code)


def sale(key, day, ttc, rate="0.2000"):
    return api.post_sale(key, day, key, f"Vente {key}", CUSTOMER, [api.SaleLine(D(ttc), D(rate))])


def purchase(key, day, base, vat_amount):
    return api.post_purchase(key, day, key, f"Achat {key}", SUPPLIER, [api.PurchaseLine(D(base), D(vat_amount))])


def balance(code):
    return acc(code).get_balance()


@override_settings(ACCOUNTING={})
class VatSettlementTest(TestCase):
    def setUp(self):
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))

    def test_vat_due_is_carried_to_44551(self):
        sale("v1", date(2026, 3, 5), "120.00")                    # TVA collectée 20,00
        sale("v2", date(2026, 3, 9), "105.50", "0.0550")          # TVA collectée 5,50
        purchase("a1", date(2026, 3, 10), "50.00", "10.00")      # TVA déductible 10,00
        preview = vat.preview(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual((preview["total_collected"], preview["total_deductible"], preview["due"], preview["credit"]),
                         (D("25.50"), D("10.00"), D("15.50"), D("0.00")))
        entry = vat.settle(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual((entry.journal.code, entry.type, entry.date, entry.is_validated),
                         ("OD", "vat", date(2026, 3, 31), True))
        for code, expected in (("445711", "0.00"), ("445713", "0.00"), ("445660", "0.00"), ("445510", "-15.50")):
            self.assertEqual(balance(code), D(expected), code)
        self.assertEqual(vat.settle(date(2026, 3, 1), date(2026, 3, 31)).pk, entry.pk)  # idempotente
        # Le récapitulatif de TVA de la période ignore l'écriture de liquidation
        summary = reports.vat_summary(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual((summary["collected"], summary["deductible"], summary["net"]), (D("25.50"), D("10.00"), D("15.50")))

    def test_credit_is_carried_forward_then_used(self):
        sale("v1", date(2026, 3, 5), "60.00")                     # collectée 10,00
        purchase("a1", date(2026, 3, 10), "200.00", "40.00")     # déductible 40,00
        vat.settle(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual((balance("445671"), balance("445510")), (D("30.00"), D("0.00")))  # crédit de 30 reporté
        sale("v2", date(2026, 4, 2), "300.00")                    # collectée 50,00
        preview = vat.preview(date(2026, 4, 1), date(2026, 4, 30))
        self.assertEqual((preview["carried_credit"], preview["due"]), (D("30.00"), D("20.00")))
        vat.settle(date(2026, 4, 1), date(2026, 4, 30))
        self.assertEqual((balance("445671"), balance("445510"), balance("445711")), (D("0.00"), D("-20.00"), D("0.00")))

    def test_settlement_blocked_by_drafts_and_by_a_later_settlement(self):
        sale("v1", date(2026, 3, 5), "120.00")
        draft = post_entry("VT", date(2026, 3, 20), "Brouillon", [
            Line(acc("411000"), debit=D("12")), Line(acc("707000"), credit=D("10")), Line(acc("445711"), credit=D("2"))])
        with self.assertRaisesMessage(AccountingError, "brouillon"):
            vat.settle(date(2026, 3, 1), date(2026, 3, 31))
        draft.delete()
        vat.settle(date(2026, 4, 1), date(2026, 4, 30))
        with self.assertRaisesMessage(AccountingError, "liquidation postérieure"):
            vat.settle(date(2026, 3, 1), date(2026, 3, 31))

    def test_nothing_to_settle(self):
        with self.assertRaisesMessage(AccountingError, "Aucune TVA"):
            vat.settle(date(2026, 3, 1), date(2026, 3, 31))

    def test_settle_from_the_vat_page(self):
        sale("v1", date(2026, 3, 5), "120.00")
        self.client.force_login(accountant())
        url = reverse("accounting:vat_report") + "?start=2026-03-01&end=2026-03-31"
        self.assertContains(self.client.get(url), "Passer l'écriture de liquidation")
        self.client.post(url)
        entry = Transaction.objects.get(type="vat")
        self.assertContains(self.client.get(url), entry.number)


@override_settings(ACCOUNTING={})
class BalanceSheetTest(TestCase):
    def setUp(self):
        self.period = FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        post_entry("BQ", date(2026, 1, 2), "Apport en capital", [Line(acc("512000"), debit=D("5000")),
                                                                 Line(acc("101000"), credit=D("5000"))], validate=True)
        purchase("a1", date(2026, 1, 10), "1000.00", "200.00")    # fournisseur 1 200, stock non inventorié
        sale("v1", date(2026, 2, 1), "600.00")                    # client 600 dont TVA 100
        api.post_payment("v1:pay", date(2026, 2, 3), "P", "Règlement", D("600.00"), "stripe", CUSTOMER)

    def section(self, sheet, side, key):
        return next((s for s in sheet[side] if s["key"] == key), None)

    def test_assets_equal_liabilities_with_current_result(self):
        sheet = reports.balance_sheet(date(2026, 2, 28))
        self.assertTrue(sheet["is_balanced"])
        self.assertEqual(sheet["total_assets"], D("5800.00"))
        self.assertEqual(self.section(sheet, "assets", "cash")["total"], D("5000.00"))  # banque
        # fonds à recevoir de Stripe (467) 600 et TVA déductible 200 : autres créances
        self.assertEqual(self.section(sheet, "assets", "other_receivables")["total"], D("800.00"))
        self.assertIsNone(self.section(sheet, "assets", "receivables"))  # client soldé
        self.assertEqual(self.section(sheet, "liabilities", "suppliers")["total"], D("1200.00"))
        self.assertEqual(self.section(sheet, "liabilities", "tax_social")["total"], D("100.00"))
        equity = self.section(sheet, "liabilities", "equity")
        self.assertEqual(sheet["result"], D("-500.00"))  # ventes 500 HT - achats 1 000
        self.assertEqual(equity["total"], D("4500.00"))
        self.assertEqual(equity["accounts"][-1]["label"], "Résultat de l'exercice (perte)")

    def test_credit_bank_balance_is_a_debt(self):
        post_entry("BQ", date(2026, 2, 10), "Règlement fournisseur", [
            Line(acc("401000"), debit=D("1200"), auxiliary_code="F001"), Line(acc("512000"), credit=D("1200"))], validate=True)
        post_entry("BQ", date(2026, 2, 11), "Achat matériel", [Line(acc("218300"), debit=D("4500")),
                                                               Line(acc("512000"), credit=D("4500"))], validate=True)
        sheet = reports.balance_sheet(date(2026, 2, 28))
        self.assertEqual(self.section(sheet, "liabilities", "financial_debts")["total"], D("700.00"))  # découvert
        self.assertEqual(self.section(sheet, "assets", "fixed")["total"], D("4500.00"))
        self.assertTrue(sheet["is_balanced"])

    def test_after_opening_entries_result_starts_again(self):
        close_period(self.period)
        following = FiscalPeriod.objects.create(name="2027", date_start=date(2027, 1, 1), date_end=date(2027, 12, 31))
        generate_opening_entries(self.period, following)
        sheet = reports.balance_sheet(date(2027, 1, 31))
        self.assertTrue(sheet["from_opening"])
        self.assertEqual(sheet["result"], D("0.00"))
        equity = self.section(sheet, "liabilities", "equity")
        self.assertEqual({r["account"].code: r["amount"] for r in equity["accounts"] if r["account"]},
                         {"101000": D("5000.00"), "129000": D("-500.00")})
        self.assertTrue(sheet["is_balanced"])

    def test_without_opening_entries_prior_results_are_shown(self):
        FiscalPeriod.objects.filter(pk=self.period.pk).update(is_closed=True)
        FiscalPeriod.objects.create(name="2027", date_start=date(2027, 1, 1), date_end=date(2027, 12, 31))
        sheet = reports.balance_sheet(date(2027, 1, 31))
        labels = [r.get("label") for r in self.section(sheet, "liabilities", "equity")["accounts"]]
        self.assertIn("Résultats des exercices antérieurs (non reportés)", labels)
        self.assertTrue(sheet["is_balanced"])

    def test_page_and_csv(self):
        self.client.force_login(accountant())
        page = self.client.get(reverse("accounting:balance_sheet") + "?as_of=2026-02-28")
        self.assertContains(page, "Bilan au 28/02/2026")
        self.assertContains(page, "Dettes fournisseurs")
        csv = self.client.get(reverse("accounting:balance_sheet") + "?as_of=2026-02-28&format=csv").content.decode("utf-8-sig")
        self.assertIn("Passif;Total passif;;;5800,00", csv)
        self.assertEqual(LedgerEntry.objects.filter(transaction__is_validated=False).count(), 0)


@override_settings(ACCOUNTING={})
class ServicesVatOnReceiptsTest(TestCase):
    """TVA des prestations de services exigible à l'encaissement (sauf option pour les débits)."""

    def setUp(self):
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))

    def services_sale(self, key="inv:1", day=date(2026, 3, 5), lines=None):
        return api.post_sale(key, day, key, f"Prestation {key}", CUSTOMER,
                             lines or [api.SaleLine(D("120.00"), D("0.2000"), kind="services")], document=("invoice", key))

    def pay(self, key, day, amount, refund=False):
        post = api.post_refund if refund else api.post_payment
        return post(key, day, key, "Règlement", D(amount), "transfer", CUSTOMER, document=("invoice", "inv:1"))

    def test_vat_waits_for_payment_then_follows_each_receipt(self):
        sale = self.services_sale()
        lines = {e.account.code: e for e in sale.entries.all()}
        self.assertEqual((lines["445800"].credit, lines["445800"].vat_base), (D("20.00"), D("100.00")))
        self.assertNotIn("445711", lines)
        march = reports.vat_summary(date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual((march["collected"], march["base"], march["pending"]), (D("0.00"), D("0.00"), D("20.00")))
        self.pay("inv:1:pay1", date(2026, 4, 2), "60.00")
        self.pay("inv:1:pay2", date(2026, 5, 6), "60.00")
        april = reports.vat_summary(date(2026, 4, 1), date(2026, 4, 30))
        self.assertEqual([(r["rate"], r["base"], r["vat"]) for r in april["rows"]], [(D("0.2000"), D("50.00"), D("10.00"))])
        self.assertEqual(reports.vat_summary(date(2026, 5, 1), date(2026, 5, 31))["collected"], D("10.00"))
        self.assertEqual((balance("445800"), balance("445711")), (D("0.00"), D("-20.00")))
        release = Transaction.objects.get(source_key="inv:1:pay1:tva")
        self.assertEqual((release.type, release.journal.code, release.is_validated), ("vat_release", "OD", True))
        # Liquidation d'avril : seule la TVA devenue exigible est due
        self.assertEqual(vat.preview(date(2026, 4, 1), date(2026, 4, 30))["due"], D("10.00"))

    def test_refund_after_payment_reduces_collected_vat(self):
        self.services_sale()
        self.pay("inv:1:pay", date(2026, 3, 10), "120.00")
        api.post_credit_note("inv:1:credit", "inv:1", date(2026, 6, 1), "AV-1", "Avoir")
        self.pay("inv:1:refund", date(2026, 6, 2), "120.00", refund=True)
        june = reports.vat_summary(date(2026, 6, 1), date(2026, 6, 30))
        self.assertEqual((june["collected"], june["base"]), (D("-20.00"), D("-100.00")))
        for code in ("445800", "445711", "411000"):
            self.assertEqual(balance(code), D("0.00"), code)

    def test_cancelled_before_payment_never_becomes_due(self):
        self.services_sale()
        api.post_credit_note("inv:1:credit", "inv:1", date(2026, 3, 20), "AV-1", "Avoir")
        self.assertEqual((balance("445800"), balance("445711")), (D("0.00"), D("0.00")))
        self.assertFalse(Transaction.objects.filter(type="vat_release").exists())

    def test_goods_vat_at_invoice_services_vat_at_receipt(self):
        sale = self.services_sale(lines=[api.SaleLine(D("60.00"), D("0.2000")), api.SaleLine(D("60.00"), D("0.2000"), kind="services")])
        lines = {e.account.code: (e.credit, e.vat_base) for e in sale.entries.all() if e.account.code.startswith("445")}
        self.assertEqual(lines, {"445711": (D("10.00"), D("50.00")), "445800": (D("10.00"), D("50.00"))})
        self.assertEqual(reports.vat_summary(date(2026, 3, 1), date(2026, 3, 31))["base"], D("50.00"))

    def test_option_for_debits_makes_services_vat_due_at_invoice(self):
        AccountingSettings.objects.filter(pk=1).update(vat_on_debits=True)
        self.services_sale()
        self.pay("inv:1:pay", date(2026, 4, 2), "120.00")
        self.assertEqual((balance("445711"), balance("445800")), (D("-20.00"), D("0.00")))
        self.assertFalse(Transaction.objects.filter(type="vat_release").exists())



@override_settings(ACCOUNTING={})
class Ca3Test(TestCase):
    def setUp(self):
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))

    def test_declaration_lines(self):
        sale("v1", date(2026, 3, 2), "120.00")                                   # 100 HT, TVA 20
        sale("v2", date(2026, 3, 3), "110.00", "0.1000")                         # 100 HT, TVA 10
        sale("v3", date(2026, 3, 4), "50.00", "0.0000")                          # exonéré
        purchase("a1", date(2026, 3, 5), "200.00", "40.00")                      # déductible autres biens
        api.post_purchase("a2", date(2026, 3, 6), "A2", "Ordinateur", SUPPLIER,
                          [api.PurchaseLine(D("1000.00"), D("200.00"), account_code="218300")])  # TVA sur immobilisation
        api.post_purchase("a3", date(2026, 3, 7), "A3", "Achat Allemagne", SUPPLIER,
                          [api.PurchaseLine(D("500.00"), vat_rate=D("0.20"))], reverse_charge="eu_goods")
        api.post_purchase("a4", date(2026, 3, 8), "A4", "Logiciel SaaS irlandais", SUPPLIER,
                          [api.PurchaseLine(D("100.00"), vat_rate=D("0.20"), account_code="651000")], reverse_charge="eu_services")
        data = reports.ca3(date(2026, 3, 1), date(2026, 3, 31))
        operations = {r["code"]: r["base"] for r in data["operations"]}
        self.assertEqual(operations, {"01": D("200.00"), "05": D("50.00"), "03": D("500.00"), "2A": D("100.00")})
        gross = {r["code"]: (r["base"], r["tax"]) for r in data["gross"]}
        self.assertEqual(gross, {"08": (D("700.00"), D("140.00")), "9B": (D("100.00"), D("10.00"))})
        self.assertEqual((data["total_gross"], data["intracom_vat"]), (D("150.00"), D("100.00")))
        self.assertEqual((data["line19"], data["line20"], data["total_deductible"]), (D("200.00"), D("160.00"), D("360.00")))
        self.assertEqual((data["credit"], data["due"]), (D("210.00"), D("0.00")))
        # Autoliquidation neutre pour la trésorerie : le fournisseur est crédité du seul HT
        self.assertEqual(Transaction.objects.get(source_key="a3").amount, D("600.00"))
        self.assertEqual(balance("401000"), D("-2040.00"))  # 240 + 1 200 + 500 + 100
        # La liquidation solde aussi la TVA autoliquidée et reporte le crédit
        vat.settle(date(2026, 3, 1), date(2026, 3, 31))
        for code in ("445200", "445662", "445620", "445660", "445711", "445712"):
            self.assertEqual(balance(code), D("0.00"), code)
        self.assertEqual(balance("445671"), D("210.00"))
        self.assertEqual(reports.ca3(date(2026, 4, 1), date(2026, 4, 30))["line22"], D("210.00"))

    def test_reverse_charge_requires_a_rate(self):
        with self.assertRaisesMessage(AccountingError, "taux de TVA"):
            api.post_purchase("x", date(2026, 3, 7), "X", "Achat", SUPPLIER, [api.PurchaseLine(D("10"))], reverse_charge="foreign")

    def test_page_and_csv(self):
        sale("v1", date(2026, 3, 2), "120.00")
        self.client.force_login(accountant())
        url = reverse("accounting:vat_report") + "?start=2026-03-01&end=2026-03-31"
        self.assertContains(self.client.get(url), "Aide à la déclaration CA3")
        csv = self.client.get(url + "&format=ca3").content.decode("utf-8-sig")
        self.assertIn("08;Taux normal 20 %;100,00;20,00", csv)
        self.assertIn("28;TVA nette due;;20,00", csv)



@override_settings(ACCOUNTING={})
class AnnualAccountsTest(TestCase):
    def setUp(self):
        from .posting import Line, post_entry
        self.y2025 = FiscalPeriod.objects.create(name="2025", date_start=date(2025, 1, 1), date_end=date(2025, 12, 31))
        self.y2026 = FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        post_entry("BQ", date(2025, 1, 2), "Apport", [Line(acc("512000"), debit=D("10000")), Line(acc("101000"), credit=D("10000"))],
                   validate=True)
        sale("v25", date(2025, 6, 1), "1200.00")
        api.post_payment("v25:pay", date(2025, 6, 2), "P", "Règlement", D("1200.00"), "transfer", CUSTOMER)
        purchase("a25", date(2025, 6, 3), "400.00", "80.00")
        close_period(self.y2025)
        generate_opening_entries(self.y2025, self.y2026)
        sale("v26", date(2026, 2, 1), "2400.00")
        api.post_purchase("fa", date(2026, 3, 1), "FA", "Ordinateur", SUPPLIER,
                          [api.PurchaseLine(D("3600.00"), D("720.00"), account_code="218300")])
        post_entry("OD", date(2026, 12, 31), "Dotation", [Line(acc("681120"), debit=D("900")), Line(acc("281830"), credit=D("900"))],
                   validate=True)

    def test_income_statement_and_detailed_balance_sheet(self):
        from . import annual
        data = annual.annual_accounts(self.y2026)
        income = data["income"]
        sections = {k: s["total"] for k, s in income["sections"].items()}
        self.assertEqual((sections["operating_income"], sections["operating_expenses"]), (D("2000.00"), D("900.00")))
        self.assertEqual((income["operating"], income["net"]), (D("1100.00"), D("1100.00")))
        self.assertEqual(data["income_previous"]["net"], D("600.00"))  # 1 000 de ventes - 400 d'achats
        sheet = data["balance"]
        self.assertTrue(sheet["is_balanced"])
        tangible = next(r for g in sheet["assets"] for r in g["rows"] if r["key"] == "tangible")
        self.assertEqual((tangible["gross"], tangible["contra"], tangible["net"]), (D("3600.00"), D("900.00"), D("2700.00")))
        liabilities = {r["key"]: r["amount"] for g in sheet["liabilities"] for r in g["rows"]}
        self.assertEqual((liabilities["capital"], liabilities["result"]), (D("10000.00"), D("1700.00")))  # 600 reportés + 1 100
        self.assertTrue(data["balance_previous"]["is_balanced"])

    def test_page_and_csv(self):
        self.client.force_login(accountant())
        page = self.client.get(reverse("accounting:annual_accounts") + f"?period={self.y2026.pk}")
        self.assertContains(page, "Comptes annuels — exercice 2026")
        self.assertContains(page, "Résultat d'exploitation")
        csv = self.client.get(reverse("accounting:annual_accounts") + f"?period={self.y2026.pk}&format=csv").content.decode("utf-8-sig")
        self.assertIn("Actif;Actif immobilisé;Immobilisations corporelles;3600,00;900,00;2700,00;", csv)
        self.assertIn("Compte de résultat;;Résultat net;;;1100,00;600,00", csv)



@override_settings(ACCOUNTING={})
class CurrencyTest(TestCase):
    def setUp(self):
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))

    def test_foreign_currency_sale_payment_exchange_gap_and_fec(self):
        from . import exports, reconciliation
        us = api.Party("C-US", "Acme Inc.")
        api.post_sale("us:1", date(2026, 3, 2), "F-US", "Vente USD", us, [api.SaleLine(D("920.00"), D("0"))],
                      currency=("USD", D("1000.00")))  # 1 000 $ au cours de 0,92
        api.post_payment("us:1:pay", date(2026, 4, 2), "P-US", "Règlement USD", D("900.00"), "transfer", us,
                         currency=("USD", D("1000.00")))  # 1 000 $ au cours de 0,90
        lines = list(LedgerEntry.objects.filter(account__code="411000", auxiliary_code="C-US"))
        self.assertEqual([(l.currency, l.currency_amount) for l in lines], [("USD", D("1000.00")), ("USD", D("1000.00"))])
        reconciliation.write_off([l.pk for l in lines], exchange=True, day=date(2026, 4, 2))
        self.assertEqual(balance("666000"), D("20.00"))  # perte de change, au-delà du plafond des petits écarts
        self.assertTrue(all(l.is_reconciled for l in LedgerEntry.objects.filter(account__code="411000", auxiliary_code="C-US")))
        _, content = exports.fec_file(date(2026, 1, 1), date(2026, 12, 31), "123456789")
        rows = [line.split("\t") for line in content.decode("iso-8859-15").splitlines()]
        header = rows[0]
        usd = [dict(zip(header, r)) for r in rows[1:] if r[header.index("Idevise")] == "USD"]
        self.assertEqual({r["Montantdevise"] for r in usd}, {"1000,00"})
        from .fec_check import check_fec
        self.assertEqual(check_fec(content, "123456789FEC20261231.txt").errors, [])
        self.assertTrue(seal.verify()["ok"])

    def test_exchange_write_off_needs_a_currency_line(self):
        from . import reconciliation
        sale("eur", date(2026, 3, 2), "100.00")
        line = LedgerEntry.objects.get(account__code="411000")
        with self.assertRaisesMessage(AccountingError, "en devise"):
            reconciliation.write_off([line.pk], exchange=True)

    def test_currency_needs_code_and_amount(self):
        with self.assertRaisesMessage(AccountingError, "devise"):
            post_entry("OD", date(2026, 3, 1), "x", [Line(acc("411000"), debit=D("10"), currency="USD"),
                                                     Line(acc("707000"), credit=D("10"))])



@override_settings(ACCOUNTING={})
class AnalyticTest(TestCase):
    def setUp(self):
        from .models import AnalyticSection
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        self.shop = AnalyticSection.objects.create(code="WEB", label="Boutique en ligne")
        self.store = AnalyticSection.objects.create(code="MAG", label="Magasin")

    def test_sections_from_api_and_after_validation(self):
        api.post_sale("w1", date(2026, 3, 1), "W1", "Vente web", CUSTOMER, [api.SaleLine(D("120.00"), analytic="WEB"),
                                                                            api.SaleLine(D("60.00"), analytic="MAG")])
        api.post_purchase("p1", date(2026, 3, 2), "P1", "Loyer magasin", SUPPLIER,
                          [api.PurchaseLine(D("50.00"), D("10.00"), account_code="613200", analytic="MAG")])
        purchase("p2", date(2026, 3, 3), "30.00", "6.00")  # non affecté
        data = reports.analytic_summary(date(2026, 1, 1), date(2026, 12, 31))
        rows = {(r["section"].code if r["section"] else None): (r["revenues"], r["expenses"], r["result"]) for r in data["rows"]}
        self.assertEqual(rows, {"WEB": (D("100.00"), D("0.00"), D("100.00")), "MAG": (D("50.00"), D("50.00"), D("0.00")),
                                None: (D("0.00"), D("30.00"), D("-30.00"))})
        # Ventilation modifiée après validation, sans casser l'empreinte
        self.client.force_login(accountant())
        txn = Transaction.objects.get(source_key="p2")
        line = txn.entries.get(account__code="607000")
        self.client.post(reverse("accounting:entry_detail", args=[txn.pk]), {"action": "analytic", f"analytic_{line.pk}": self.shop.pk})
        line.refresh_from_db()
        self.assertEqual(line.analytic, self.shop)
        self.assertTrue(seal.verify()["ok"])
        self.assertContains(self.client.get(reverse("accounting:analytic") + "?start=2026-01-01&end=2026-12-31"), "Boutique en ligne")
        csv = self.client.get(reverse("accounting:analytic") + "?start=2026-01-01&end=2026-12-31&format=csv").content.decode("utf-8-sig")
        self.assertIn("WEB;Boutique en ligne;100,00;30,00;70,00", csv)

    def test_unknown_section_and_management_page(self):
        with self.assertRaisesMessage(AccountingError, "Section analytique XX inconnue"):
            api.post_sale("w2", date(2026, 3, 1), "W2", "Vente", CUSTOMER, [api.SaleLine(D("12.00"), analytic="XX")])
        self.client.force_login(accountant())
        self.client.post(reverse("accounting:analytic_sections"), {"code": "evt", "label": "Salons", "is_active": "on"})
        from .models import AnalyticSection
        self.assertTrue(AnalyticSection.objects.filter(code="EVT").exists())
