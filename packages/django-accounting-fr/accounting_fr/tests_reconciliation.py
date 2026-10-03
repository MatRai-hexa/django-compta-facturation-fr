"""Lettrage des comptes de tiers : automatique, manuel, écarts, délettrage, balance âgée, écrans, FEC."""
from datetime import date
from decimal import Decimal
import io

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from . import api, exports, reconciliation
from .fec_check import check_fec
from .models import Account, FiscalPeriod, Journal, LedgerEntry, Transaction
from .posting import Line, close_period, generate_opening_entries, post_entry
from .reconciliation import ReconciliationError

ALICE = api.Party("alice@example.com", "Alice Martin")
BOB = api.Party("bob@example.com", "Bob Durand")
D = Decimal


def acc(code):
    return Account.objects.get(code=code)


def customer_lines(party=ALICE):
    return LedgerEntry.objects.filter(account__code="411000", auxiliary_code=party.code).order_by("pk")


def sale(key, day, amount, party=ALICE, document=None, reference=""):
    return api.post_sale(key, day, reference or key, f"Vente {key}", party, [api.SaleLine(D(amount))], document=document)


def payment(key, day, amount, party=ALICE, document=None, reference=""):
    return api.post_payment(key, day, reference or key, f"Règlement {key}", D(amount), "stripe", party, document=document)


@override_settings(ACCOUNTING={})
class CodesTest(TestCase):
    def test_codes_follow_a_to_z_then_aa(self):
        self.assertEqual([reconciliation.int_to_code(n) for n in (1, 26, 27, 52, 703)], ["A", "Z", "AA", "AZ", "AAA"])
        for n in (1, 26, 27, 702, 703, 18278):
            self.assertEqual(reconciliation.code_to_int(reconciliation.int_to_code(n)), n)


@override_settings(ACCOUNTING={})
class AutoReconcileTest(TestCase):
    def setUp(self):
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))

    def test_same_document_sale_payment_and_refund(self):
        sale("o1:sale", date(2026, 3, 1), "120.00", document=("order", "1"))
        payment("o1:pay", date(2026, 3, 1), "120.00", document=("order", "1"))
        sale("o2:sale", date(2026, 3, 2), "50.00", document=("order", "2"))
        payment("o2:pay", date(2026, 3, 2), "50.00", document=("order", "2"))
        api.post_credit_note("o2:credit", "o2:sale", date(2026, 3, 5), "AV1", "Avoir o2")
        api.post_refund("o2:refund", date(2026, 3, 5), "re_1", "Remboursement o2", D("50.00"), "stripe", ALICE,
                        document=("order", "2"))
        self.assertEqual(reconciliation.auto_reconcile(), 2)
        refs = list(customer_lines().values_list("reconciliation_ref", flat=True))
        self.assertEqual(refs, ["A", "A", "B", "B", "B", "B"])
        line = customer_lines().first()
        self.assertTrue(line.is_reconciled)
        self.assertEqual(line.reconciled_at, date.today())
        self.assertEqual(reconciliation.auto_reconcile(), 0)  # idempotent

    def test_reference_then_unique_amount_then_solde(self):
        sale("s1", date(2026, 1, 5), "100.00", reference="F1")
        payment("p1", date(2026, 1, 20), "100.00", reference="F1")      # même référence
        sale("s2", date(2026, 2, 5), "80.00", party=BOB)
        payment("p2", date(2026, 2, 25), "80.00", party=BOB)            # montant unique chez Bob
        sale("s3", date(2026, 2, 6), "30.00", party=BOB)
        sale("s4", date(2026, 2, 7), "30.00", party=BOB)
        payment("p3", date(2026, 3, 1), "60.00", party=BOB)             # règle les deux : tiers soldé
        self.assertEqual(reconciliation.auto_reconcile(), 3)
        self.assertFalse(LedgerEntry.objects.filter(account__code="411000", reconciliation_ref="").exists())
        bob = dict(customer_lines(BOB).values_list("transaction__source_key", "reconciliation_ref"))
        self.assertEqual(bob["s2"], bob["p2"])
        self.assertEqual(bob["s3"], bob["s4"])
        self.assertEqual(bob["s4"], bob["p3"])
        self.assertNotEqual(bob["s2"], bob["s3"])

    def test_ambiguous_amounts_and_partial_payments_stay_open(self):
        sale("s1", date(2026, 1, 5), "40.00")
        sale("s2", date(2026, 1, 6), "40.00")
        payment("p1", date(2026, 1, 20), "40.00")  # laquelle des deux ? à lettrer à la main
        sale("s3", date(2026, 1, 7), "70.00", party=BOB)
        payment("p2", date(2026, 1, 21), "50.00", party=BOB)
        self.assertEqual(reconciliation.auto_reconcile(), 0)

    def test_tiers_and_drafts(self):
        sale("s1", date(2026, 1, 5), "100.00")
        sale("s2", date(2026, 1, 6), "25.00", party=BOB)
        api.post_payment("p1", date(2026, 1, 7), "P1", "Règlement", D("100.00"), "stripe", ALICE, validate=False)
        rows = {r["code"]: r for r in reconciliation.tiers(acc("411000"))}
        self.assertEqual((rows["alice@example.com"]["balance"], rows["alice@example.com"]["count"]), (D("100.00"), 1))
        self.assertEqual(rows["bob@example.com"]["label"], "Bob Durand")
        self.assertEqual(reconciliation.auto_reconcile(), 0)  # un brouillon ne se lettre pas

    def test_command(self):
        sale("s1", date(2026, 1, 5), "10.00")
        payment("p1", date(2026, 1, 6), "10.00")
        out = io.StringIO()
        call_command("lettrage_auto", stdout=out)
        self.assertIn("1 lettrage", out.getvalue())


@override_settings(ACCOUNTING={})
class ManualReconcileTest(TestCase):
    def setUp(self):
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        sale("s1", date(2026, 1, 5), "40.00")
        sale("s2", date(2026, 1, 6), "40.00")
        payment("p1", date(2026, 1, 20), "40.00")
        self.s1, self.s2, self.p1 = customer_lines()

    def test_reconcile_and_unreconcile(self):
        self.assertEqual(reconciliation.reconcile([self.s2.pk, self.p1.pk]), "A")
        self.s2.refresh_from_db()
        self.assertEqual(self.s2.reconciliation_ref, "A")
        # les lignes d'une écriture validée restent verrouillées pour tout le reste
        self.assertEqual(self.s2.debit, D("40.00"))
        self.assertEqual(reconciliation.unreconcile(acc("411000"), "A"), 2)
        self.assertFalse(customer_lines().exclude(reconciliation_ref="").exists())
        self.assertEqual(reconciliation.reconcile([self.s1.pk, self.p1.pk]), "A")  # code réattribué

    def test_refusals(self):
        with self.assertRaisesMessage(ReconciliationError, "déséquilibrées"):
            reconciliation.reconcile([self.s1.pk, self.s2.pk, self.p1.pk])
        with self.assertRaisesMessage(ReconciliationError, "au moins deux"):
            reconciliation.reconcile([self.s1.pk])
        sale("b1", date(2026, 1, 8), "40.00", party=BOB)
        with self.assertRaisesMessage(ReconciliationError, "même tiers"):
            reconciliation.reconcile([customer_lines(BOB).get().pk, self.p1.pk])
        payment("b2", date(2026, 1, 9), "40.00", party=BOB)
        vat = LedgerEntry.objects.filter(account__code="445711").first()
        bank = LedgerEntry.objects.filter(account__code="467100").first()
        with self.assertRaisesMessage(ReconciliationError, "même compte"):
            reconciliation.reconcile([vat.pk, self.p1.pk])
        with self.assertRaisesMessage(ReconciliationError, "pas un compte de tiers"):
            reconciliation.reconcile([bank.pk, LedgerEntry.objects.filter(account__code="467100").last().pk])
        reconciliation.reconcile([self.s1.pk, self.p1.pk])
        with self.assertRaisesMessage(ReconciliationError, "déjà lettrée"):
            reconciliation.reconcile([self.s2.pk, self.p1.pk])

    def test_write_off_small_gap(self):
        payment("p2", date(2026, 1, 21), "39.50")
        p2 = customer_lines().last()
        code = reconciliation.write_off([self.s2.pk, p2.pk])
        od = Transaction.objects.get(journal__code="OD")
        self.assertTrue(od.is_validated)
        lines = {e.account.code: e for e in od.entries.all()}
        self.assertEqual((lines["658000"].debit, lines["411000"].credit), (D("0.50"), D("0.50")))
        self.assertEqual(lines["411000"].auxiliary_code, ALICE.code)
        self.assertEqual(lines["411000"].reconciliation_ref, code)
        self.assertEqual(customer_lines().filter(reconciliation_ref=code).count(), 3)

    def test_write_off_overpayment_and_limit(self):
        payment("p2", date(2026, 1, 21), "41.00")
        p2 = customer_lines().last()
        reconciliation.write_off([self.s2.pk, p2.pk])
        self.assertTrue(LedgerEntry.objects.filter(account__code="758000", credit=D("1.00")).exists())
        payment("p3", date(2026, 1, 22), "10.00")
        with self.assertRaisesMessage(ReconciliationError, "au-delà"):
            reconciliation.write_off([self.s1.pk, customer_lines().last().pk])

    def test_closed_period_lines_are_locked(self):
        reconciliation.reconcile([self.s1.pk, self.p1.pk])
        close_period(FiscalPeriod.objects.get(name="2026"))
        with self.assertRaisesMessage(ReconciliationError, "clôturé"):
            reconciliation.unreconcile(acc("411000"), "A")
        with self.assertRaisesMessage(ReconciliationError, "clôturé"):
            reconciliation.reconcile([self.s2.pk, self.s1.pk])
        self.assertEqual(reconciliation.tiers(acc("411000")), [])


@override_settings(ACCOUNTING={})
class OpeningAndFecTest(TestCase):
    def test_opening_balance_reconciles_with_next_year_payment_and_fec_passes(self):
        year = FiscalPeriod.objects.create(name="2025", date_start=date(2025, 1, 1), date_end=date(2025, 12, 31))
        sale("s1", date(2025, 3, 1), "120.00")
        payment("p1", date(2025, 3, 1), "120.00")
        sale("s2", date(2025, 12, 20), "60.00")
        reconciliation.auto_reconcile(day=date(2025, 12, 31))
        close_period(year)
        filename, content = exports.fec_file(year.date_start, year.date_end, "123456789")
        report = check_fec(content, filename, period_start=year.date_start, period_end=year.date_end)
        self.assertEqual(report.errors, [])
        rows = [line.split("\t") for line in content.decode("iso-8859-15").splitlines()[1:]]
        lettered = [r for r in rows if r[4] == "411000" and r[13]]
        self.assertEqual([(r[13], r[14]) for r in lettered], [("A", "20251231"), ("A", "20251231")])

        nxt = FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        generate_opening_entries(year, nxt)
        payment("p2", date(2026, 1, 15), "60.00")
        self.assertEqual(reconciliation.auto_reconcile(), 1)  # à-nouveau du tiers + règlement
        self.assertFalse(reconciliation.tiers(acc("411000")))


@override_settings(ACCOUNTING={})
class AgedBalanceTest(TestCase):
    def setUp(self):
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        sale("s1", date(2026, 6, 25), "100.00")             # 5 jours
        sale("s2", date(2026, 5, 1), "50.00")               # 60 jours
        sale("s3", date(2026, 1, 10), "30.00", party=BOB)   # plus de 90 jours
        payment("p1", date(2026, 6, 28), "50.00", reference="s2")
        self.as_of = date(2026, 6, 30)

    def test_buckets_and_as_of(self):
        data = reconciliation.aged_balance(acc("411000"), self.as_of)
        self.assertEqual(data["labels"], ["0 à 30 j", "31 à 60 j", "61 à 90 j", "plus de 90 j"])
        alice = next(r for r in data["rows"] if r["code"] == ALICE.code)
        self.assertEqual(alice["buckets"], [D("50.00"), D("50.00"), D("0"), D("0")])  # 100 − 50 réglés, 50 à 60 j
        self.assertEqual(alice["total"], D("100.00"))
        self.assertEqual(data["total"], D("130.00"))
        reconciliation.auto_reconcile(day=date(2026, 7, 2))
        data = reconciliation.aged_balance(acc("411000"), self.as_of)  # lettré après la date : encore ouvert
        self.assertEqual(data["total"], D("130.00"))
        data = reconciliation.aged_balance(acc("411000"), date(2026, 7, 5))
        alice = next(r for r in data["rows"] if r["code"] == ALICE.code)
        self.assertEqual(alice["buckets"], [D("100.00"), D("0"), D("0"), D("0")])
        self.assertIn("Bob Durand", exports.aged_balance_csv(data).decode("utf-8"))

    def test_supplier_balance_reads_credit(self):
        api.post_purchase("a1", date(2026, 6, 1), "FA1", "Achat FA1", api.Party("123456789", "Papeterie"),
                          [api.PurchaseLine(D("100.00"), D("20.00"))])
        data = reconciliation.aged_balance(acc("401000"), self.as_of)
        self.assertEqual((data["rows"][0]["label"], data["total"]), ("Papeterie", D("120.00")))


def make_user(*perms):
    u = get_user_model().objects.create_user(username="c@x.fr", email="c@x.fr", password="x" * 12)
    u.user_permissions.add(*Permission.objects.filter(content_type__app_label="accounting", codename__in=perms))
    return u


@override_settings(ACCOUNTING={})
class PagesTest(TestCase):
    def setUp(self):
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        sale("s1", date(2026, 1, 5), "40.00")
        sale("s2", date(2026, 1, 6), "40.00")
        payment("p1", date(2026, 1, 20), "40.00")
        sale("b1", date(2026, 1, 8), "15.00", party=BOB)
        payment("b2", date(2026, 1, 9), "15.00", party=BOB)

    def test_read_only_user(self):
        self.client.force_login(make_user("view_reports"))
        page = self.client.get(reverse("accounting:reconciliation"))
        self.assertContains(page, "Alice Martin")
        self.assertNotContains(page, "Bob Durand")  # solde nul : masqué par défaut
        self.assertContains(self.client.get(reverse("accounting:reconciliation") + "?all=1"), "Bob Durand")
        url = reverse("accounting:reconciliation_account", args=["411000"]) + f"?aux={ALICE.code}"
        self.assertContains(self.client.get(url), 'name="lines"', count=3)
        self.assertEqual(self.client.post(reverse("accounting:reconciliation")).status_code, 403)
        self.assertEqual(self.client.post(url, {"action": "reconcile"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("accounting:reconciliation_account", args=["707000"])).status_code, 404)
        aged = self.client.get(reverse("accounting:aged_balance"))
        self.assertContains(aged, "Alice Martin")
        self.assertEqual(self.client.get(reverse("accounting:aged_balance") + "?format=csv").status_code, 403)

    def test_reconcile_from_pages(self):
        self.client.force_login(make_user("view_reports", "reconcile_entries", "export_data"))
        self.assertContains(self.client.get(reverse("accounting:reconciliation")), "1 tiers a un solde nul")
        self.client.post(reverse("accounting:reconciliation"), {"scope": "all"})
        self.assertEqual(customer_lines(BOB).exclude(reconciliation_ref="").count(), 2)
        s1, s2, p1 = customer_lines()
        url = reverse("accounting:reconciliation_account", args=["411000"]) + f"?aux={ALICE.code}"
        response = self.client.post(url, {"action": "reconcile", "lines": [s1.pk, s2.pk]}, follow=True)
        self.assertContains(response, "déséquilibrées")
        self.client.post(url, {"action": "reconcile", "lines": [s1.pk, p1.pk]})
        s1.refresh_from_db()
        self.assertEqual(s1.reconciliation_ref, "B")
        # sans le droit de valider, pas d'écriture d'écart
        self.assertEqual(self.client.post(url, {"action": "write_off", "lines": [s2.pk]}).status_code, 403)
        self.assertContains(self.client.get(url + "&all=1"), "délettrer")
        self.client.post(url, {"unreconcile": "B"})
        s1.refresh_from_db()
        self.assertEqual(s1.reconciliation_ref, "")
        csv = self.client.get(reverse("accounting:aged_balance") + "?format=csv")
        self.assertIn("Alice Martin", csv.content.decode("utf-8"))
        ledger = self.client.get(reverse("accounting:general_ledger", args=["411000"]))
        self.assertContains(ledger, "Let.")

    def test_manual_entry_with_tiers(self):
        self.client.force_login(make_user("view_transaction", "add_transaction", "validate_transaction"))
        form = self.client.get(reverse("accounting:entry_create"))
        self.assertContains(form, '<option value="alice@example.com">Alice Martin</option>', html=True)
        data = {"journal": Journal.objects.get(code="BQ").pk, "date": "2026-02-01", "reference": "VIR-1", "description": "Virement Alice",
                "validate": "on", "lines-TOTAL_FORMS": "2", "lines-INITIAL_FORMS": "0", "lines-MIN_NUM_FORMS": "2",
                "lines-MAX_NUM_FORMS": "1000",
                "lines-0-account": acc("512000").pk, "lines-0-debit": "40.00",
                "lines-1-account": acc("411000").pk, "lines-1-credit": "40.00", "lines-1-auxiliary_code": ALICE.code}
        self.client.post(reverse("accounting:entry_create"), data)
        line = LedgerEntry.objects.get(transaction__reference="VIR-1", account__code="411000")
        self.assertEqual((line.auxiliary_code, line.auxiliary_label), (ALICE.code, "Alice Martin"))


@override_settings(ACCOUNTING={})
class ExportCenterTest(TestCase):
    def test_csv_and_xlsx_downloads(self):
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        post_entry("BQ", date(2026, 1, 3), "Frais", [Line(acc("627000"), debit=D("5")), Line(acc("512000"), credit=D("5"))])
        self.client.force_login(make_user("export_data"))
        url = reverse("accounting:exports") + "?start=2026-01-01&end=2026-12-31&download="
        self.assertEqual(self.client.get(url + "csv")["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("spreadsheetml", self.client.get(url + "xlsx")["Content-Type"])
