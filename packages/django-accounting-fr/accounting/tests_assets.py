"""Immobilisations : plan d'amortissement, dotations, sortie de l'actif."""
from datetime import date
from decimal import Decimal as D

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from . import api, assets, reports, seal
from .models import Account, DepreciationRecord, FiscalPeriod, FixedAsset, Transaction
from .posting import AccountingError
from .tests import accountant

SUPPLIER = api.Party("F042", "Informatique Pro")


def acc(code):
    return Account.objects.get(code=code)


def balances(txn):
    result = {}
    for e in txn.entries.select_related("account"):
        result[e.account.code] = result.get(e.account.code, D("0")) + e.debit - e.credit
    return result


@override_settings(ACCOUNTING={})
class AssetsTest(TestCase):
    def setUp(self):
        self.y2026 = FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        self.y2027 = FiscalPeriod.objects.create(name="2027", date_start=date(2027, 1, 1), date_end=date(2027, 12, 31))
        api.post_purchase("fa:1", date(2026, 3, 15), "FA-1", "Serveur", SUPPLIER,
                          [api.PurchaseLine(D("3600.00"), D("720.00"), account_code="218300")])
        self.server = FixedAsset.objects.create(label="Serveur", reference="INV-1", account=acc("218300"),
                                                acquisition_date=date(2026, 3, 15), service_date=date(2026, 3, 15),
                                                cost=D("3600.00"), duration_months=36)

    def test_days_360(self):
        self.assertEqual(assets.days360(date(2026, 1, 1), date(2026, 12, 31)), 360)
        self.assertEqual(assets.days360(date(2026, 3, 15), date(2026, 12, 31)), 286)
        self.assertEqual(assets.days360(date(2026, 2, 1), date(2026, 2, 28)), 30)  # fin de mois : 30
        self.assertEqual(assets.days360(date(2026, 1, 31), date(2026, 1, 31)), 1)

    def test_linear_schedule_prorata_temporis(self):
        plan = [(r["name"], r["amount"], r["net"]) for r in assets.schedule(self.server)]
        self.assertEqual(plan, [("2026", D("953.33"), D("2646.67")), ("2027", D("1200.00"), D("1446.67")),
                                ("2028", D("1200.00"), D("246.67")), ("2029", D("246.67"), D("0.00"))])
        self.assertEqual(sum(r[1] for r in plan), D("3600.00"))

    def test_post_depreciations_once_then_only_the_rest(self):
        txn = assets.post_depreciations(self.y2026)
        self.assertEqual((txn.journal.code, txn.type, txn.date, txn.is_validated, bool(txn.seal)),
                         ("OD", "depreciation", date(2026, 12, 31), True, True))
        self.assertEqual(balances(txn), {"681120": D("953.33"), "281830": D("-953.33")})
        with self.assertRaisesMessage(AccountingError, "Aucune dotation"):
            assets.post_depreciations(self.y2026)
        chair = FixedAsset.objects.create(label="Fauteuil", account=acc("218400"), acquisition_date=date(2026, 7, 1),
                                          service_date=date(2026, 7, 1), cost=D("720.00"), duration_months=60)
        second = assets.post_depreciations(self.y2026)  # immobilisation ajoutée après la première passe
        self.assertEqual(balances(second), {"681120": D("72.00"), "281840": D("-72.00")})
        self.assertEqual(DepreciationRecord.objects.filter(asset=chair).count(), 1)
        self.assertTrue(seal.verify()["ok"])

    def test_closed_period_refused(self):
        assets.post_depreciations(self.y2026)
        FiscalPeriod.objects.filter(pk=self.y2026.pk).update(is_closed=True)
        self.y2026.refresh_from_db()
        with self.assertRaisesMessage(AccountingError, "clôturé"):
            assets.post_depreciations(self.y2026)

    def test_balance_sheet_shows_net_book_value(self):
        assets.post_depreciations(self.y2026)
        sheet = reports.balance_sheet(date(2026, 12, 31))
        fixed = next(s for s in sheet["assets"] if s["key"] == "fixed")
        self.assertEqual(fixed["total"], D("2646.67"))
        self.assertTrue(sheet["is_balanced"])

    def test_sale_mid_year(self):
        assets.post_depreciations(self.y2026)
        exit_entry = assets.dispose(self.server, date(2027, 6, 30), D("2000.00"))
        complement = Transaction.objects.get(source_key=f"depreciation:asset:{self.server.pk}:disposal")
        self.assertEqual(balances(complement), {"681120": D("600.00"), "281830": D("-600.00")})
        self.assertEqual(balances(exit_entry), {"218300": D("-3600.00"), "281830": D("1553.33"), "675000": D("2046.67"),
                                                "462000": D("2000.00"), "775000": D("-2000.00")})
        for code in ("218300", "281830"):
            self.assertEqual(acc(code).get_balance(), D("0.00"), code)
        self.server.refresh_from_db()
        self.assertEqual((self.server.disposal_date, self.server.disposal_price), (date(2027, 6, 30), D("2000.00")))
        with self.assertRaisesMessage(AccountingError, "déjà sortie"):
            assets.dispose(self.server, date(2027, 7, 1))
        # Une dotation de fin d'exercice n'y revient pas
        with self.assertRaisesMessage(AccountingError, "Aucune dotation"):
            assets.post_depreciations(self.y2027)

    def test_scrapping_without_price_and_non_depreciable_asset(self):
        exit_entry = assets.dispose(self.server, date(2026, 3, 31))  # 16 jours (mois de 30 jours) : 53,33 amortis
        self.assertEqual(balances(exit_entry)["675000"], D("3546.67"))
        self.assertNotIn("462000", balances(exit_entry))
        brand = FixedAsset.objects.create(label="Fonds commercial", account=acc("218300"), acquisition_date=date(2026, 1, 5),
                                          service_date=date(2026, 1, 5), cost=D("10000.00"), method="none")
        self.assertEqual(assets.schedule(brand), [])
        self.assertEqual(assets.accrued(brand, date(2030, 1, 1)), D("0"))

    def test_validation(self):
        bad = FixedAsset(label="x", account=acc("281830"), acquisition_date=date(2026, 5, 1), service_date=date(2026, 4, 1),
                         cost=D("-1"), method="linear")
        with self.assertRaises(ValidationError) as ctx:
            bad.full_clean()
        self.assertEqual(set(ctx.exception.message_dict), {"account", "cost", "service_date", "duration_months"})

    def test_register_gaps(self):
        self.assertEqual(assets.register_gaps(), [])
        FixedAsset.objects.create(label="Portable", account=acc("218300"), acquisition_date=date(2026, 4, 1),
                                  service_date=date(2026, 4, 1), cost=D("1200.00"), duration_months=36)
        self.assertEqual(assets.register_gaps(), [{"account": "218300", "register": D("4800.00"), "books": D("3600.00"),
                                                   "gap": D("-1200.00")}])

    def test_pages(self):
        self.client.force_login(accountant())
        self.assertContains(self.client.get(reverse("accounting:fixed_assets")), "INV-1 · Serveur")
        response = self.client.post(reverse("accounting:fixed_asset_create"), {
            "label": "Écran", "account": acc("218300").pk, "acquisition_date": "2026-05-02", "service_date": "2026-05-02",
            "cost": "360.00", "method": "linear", "duration_months": "36"})
        screen = FixedAsset.objects.get(label="Écran")
        self.assertRedirects(response, reverse("accounting:fixed_asset_detail", args=[screen.pk]))
        self.client.post(reverse("accounting:fixed_assets"), {"period": self.y2026.pk})
        self.assertEqual(Transaction.objects.filter(type="depreciation").count(), 1)
        detail = self.client.get(reverse("accounting:fixed_asset_detail", args=[self.server.pk]))
        self.assertContains(detail, "Plan d'amortissement")
        self.assertContains(detail, "953,33")
        form = self.client.get(reverse("accounting:fixed_asset_edit", args=[self.server.pk])).context["form"]
        self.assertTrue(form.fields["cost"].disabled)  # amortissement commencé
        self.client.post(reverse("accounting:fixed_asset_detail", args=[screen.pk]), {"date": "2027-01-15", "price": ""})
        screen.refresh_from_db()
        self.assertEqual(screen.disposal_date, date(2027, 1, 15))



@override_settings(ACCOUNTING={})
class DegressiveAndImpairmentTest(TestCase):
    def setUp(self):
        for year in (2026, 2027, 2028, 2029, 2030):
            FiscalPeriod.objects.create(name=str(year), date_start=date(year, 1, 1), date_end=date(year, 12, 31))
        api.post_purchase("fa:m", date(2026, 4, 10), "FA-M", "Machine", SUPPLIER,
                          [api.PurchaseLine(D("10000.00"), D("2000.00"), account_code="215400")])
        self.machine = FixedAsset.objects.create(label="Machine", account=acc("215400"), acquisition_date=date(2026, 4, 10),
                                                 service_date=date(2026, 4, 10), cost=D("10000.00"), method="degressive",
                                                 duration_months=60)

    def test_degressive_plan_switches_to_linear(self):
        self.assertEqual(assets.degressive_coefficient(60), D("1.75"))
        plan = [(r["name"], r["amount"]) for r in assets.schedule(self.machine)]
        # Taux 35 %, 9 mois la première année (avril à décembre), bascule en linéaire en 2029
        self.assertEqual(plan, [("2026", D("2625.00")), ("2027", D("2581.25")), ("2028", D("1677.81")),
                                ("2029", D("1384.86")), ("2030", D("1384.86")), ("2031", D("346.22"))])
        self.assertEqual(sum(a for _, a in plan), D("10000.00"))
        self.assertEqual(assets.accrued(self.machine, date(2026, 6, 30)), D("875.00"))  # 3 mois sur 9

    def test_impairment_then_reversal_and_disposal_with_vat(self):
        assets.post_depreciations(FiscalPeriod.objects.get(name="2026"))
        first = assets.impair(self.machine, date(2027, 3, 31), D("1000.00"))
        self.assertEqual(balances(first), {"681600": D("1000.00"), "291540": D("-1000.00")})
        with self.assertRaisesMessage(AccountingError, "reprise dépasse"):
            assets.impair(self.machine, date(2027, 4, 1), D("-1500.00"))
        assets.impair(self.machine, date(2027, 6, 30), D("-400.00"))
        self.assertEqual(assets.posted(self.machine, kind="impairment"), D("600.00"))
        exit_entry = assets.dispose(self.machine, date(2027, 6, 30), D("5000.00"), vat_rate=D("0.20"))
        lines = balances(exit_entry)
        self.assertEqual((lines["291540"], lines["781600"]), (D("600.00"), D("-600.00")))
        self.assertEqual((lines["462000"], lines["775000"], lines["445711"]), (D("6000.00"), D("-5000.00"), D("-1000.00")))
        for code in ("215400", "281540", "291540"):
            self.assertEqual(acc(code).get_balance(), D("0.00"), code)
        self.assertTrue(seal.verify()["ok"])

    def test_degressive_needs_three_years(self):
        short = FixedAsset(label="x", account=acc("215400"), acquisition_date=date(2026, 1, 1), service_date=date(2026, 1, 1),
                           cost=D("100"), method="degressive", duration_months=24)
        with self.assertRaises(ValidationError):
            short.full_clean()
