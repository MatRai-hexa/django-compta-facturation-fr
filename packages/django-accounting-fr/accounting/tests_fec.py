"""Contrôleur de FEC : détection des anomalies de structure."""
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
import io

from . import api, exports
from .fec_check import COLUMNS, check_fec
from .models import Account, FiscalPeriod, LedgerEntry, Transaction
from .posting import Line, close_period, generate_opening_entries, post_entry

NAME = "123456789FEC20261231.txt"


def fec(*rows, header=None, separator="\t"):
    lines = [separator.join(header or COLUMNS)]
    lines += [separator.join(row) for row in rows]
    return ("\r\n".join(lines) + "\r\n").encode("iso-8859-15")


def row(num="VT2026-00001", day="20260115", account="411000", debit="10,00", credit="0,00", journal="VT",
        aux=("", ""), letter=("", ""), valid="20260115", label="Vente"):
    return [journal, "Ventes", num, day, account, "Compte " + account, aux[0], aux[1], "F1", day, label,
            debit, credit, letter[0], letter[1], valid, "", ""]


def sale(num="VT2026-00001", day="20260115"):
    return [row(num, day, valid=day), row(num, day, "707000", "0,00", "10,00", valid=day)]


class CheckFecTest(SimpleTestCase):
    def test_conform_file(self):
        report = check_fec(fec(*sale(), *sale("VT2026-00002", "20260116")), NAME)
        self.assertEqual((report.errors, report.warnings), ([], []))
        self.assertEqual((report.stats["ecritures"], report.stats["total_debit"]), (2, "20.00"))

    def test_pipe_separator_and_utf8(self):
        content = fec(*sale(), separator="|").decode("iso-8859-15").encode("utf-8")
        report = check_fec(content, NAME)
        self.assertTrue(report.ok)
        self.assertEqual((report.stats["separateur"], report.stats["encodage"]), ("|", "utf-8"))

    def test_file_name_and_siren(self):
        self.assertIn("Nom de fichier", check_fec(fec(*sale()), "export.txt").errors[0])
        self.assertIn("SIREN absent", check_fec(fec(*sale()), "000000000FEC20261231.txt").errors[0])

    def test_header(self):
        header = COLUMNS[:]
        header[0], header[1] = header[1], header[0]
        self.assertIn("En-tête non conforme", check_fec(fec(*sale(), header=header), NAME).errors[0])

    def test_line_level_rules(self):
        bad = [
            row(day="2026-01-15"),                                  # date au mauvais format
            row(debit="1 000,00", credit="0,00"),                   # séparateur de milliers
            row(aux=("C01", "")),                                   # auxiliaire incomplet
            row(letter=("A", "")),                                  # lettrage incomplet
            row(label=""),                                          # libellé obligatoire
        ]
        errors = check_fec(fec(*bad), NAME).errors
        for expected in ("n'est pas une date AAAAMMJJ", "montants « 1 000,00 »", "CompAuxNum et CompAuxLib",
                         "EcritureLet et DateLet", "zone EcritureLib vide"):
            self.assertTrue(any(expected in e for e in errors), expected)

    def test_balance_dates_and_numbering(self):
        unbalanced = [row(), row(account="707000", debit="0,00", credit="9,00")]
        report = check_fec(fec(*unbalanced, *sale("VT2026-00003", "20260110")), NAME)
        self.assertTrue(any("déséquilibrée" in e for e in report.errors))
        self.assertTrue(any("numéros manquants : 2" in e for e in report.errors))
        self.assertTrue(any("non chronologique" in w for w in report.warnings))
        self.assertTrue(any("Total du fichier déséquilibré" in e for e in report.errors))

    def test_entries_outside_the_fiscal_year(self):
        report = check_fec(fec(*sale(day="20270105")), NAME)
        self.assertTrue(any("après la clôture" in e for e in report.errors))

    def test_many_errors_are_counted_but_not_all_listed(self):
        rows = [row(day="x") for _ in range(80)]
        report = check_fec(fec(*rows), NAME)
        self.assertEqual(report.summary()["error:date:EcritureDate"], 80)
        self.assertLessEqual(len([e for e in report.errors if "EcritureDate" in e]), 50)


def acc(code):
    return Account.objects.get(code=code)


class GeneratedFecTest(TestCase):
    """Le FEC produit par l'application passe le contrôle, à-nouveaux compris."""

    def test_full_year_then_opening_entries(self):
        year = FiscalPeriod.objects.create(name="2024", date_start=date(2024, 1, 1), date_end=date(2024, 12, 31))
        customer = api.Party("dupont@example.com", "Camille Dupont – L’Atelier")  # caractères hors ISO 8859-15
        api.post_sale("inv:1:sale", date(2024, 3, 2), "F-1", "Vente F-1", customer,
                      [api.SaleLine(Decimal("120.00")), api.SaleLine(Decimal("6.00"), kind="shipping")])
        api.post_payment("inv:1:payment", date(2024, 3, 2), "pi_1", "Règlement F-1", Decimal("126.00"), "stripe", customer)
        api.post_fee("inv:1:fee", date(2024, 3, 2), "pi_1", "Frais Stripe F-1", Decimal("2.14"), "stripe")
        api.post_transfer("stripe:payout:po_1", date(2024, 3, 9), "po_1", "Virement Stripe", Decimal("123.86"), "stripe")
        api.post_sale("inv:2:sale", date(2024, 11, 20), "F-2", "Vente F-2", customer, [api.SaleLine(Decimal("50.00"))])
        post_entry("AC", date(2024, 4, 1), "Achat fournitures", [
            Line(acc("606000"), debit=Decimal("100.00")), Line(acc("445660"), debit=Decimal("20.00")),
            Line(acc("401000"), credit=Decimal("120.00"), auxiliary_code="F001", auxiliary_label="Papeterie Martin")],
            reference="FA-88", validate=True)
        close_period(year)

        filename, content = exports.fec_file(year.date_start, year.date_end, "123 456 789")
        report = check_fec(content, filename, period_start=year.date_start, period_end=year.date_end)
        self.assertEqual((report.errors, report.warnings), ([], []))
        self.assertIn("Camille Dupont - L'Atelier", content.decode("iso-8859-15"))

        nxt = FiscalPeriod.objects.create(name="2025", date_start=date(2025, 1, 1), date_end=date(2025, 12, 31))
        opening = generate_opening_entries(year, nxt)
        lines = {(e.account.code, e.auxiliary_code): e for e in opening.entries.all()}
        self.assertEqual(lines[("411000", "dupont@example.com")].debit, Decimal("50.00"))  # client détaillé
        self.assertEqual(lines[("401000", "F001")].auxiliary_label, "Papeterie Martin")
        filename, content = exports.fec_file(nxt.date_start, nxt.date_end, "123456789")
        report = check_fec(content, filename, period_start=nxt.date_start, period_end=nxt.date_end)
        self.assertEqual((report.errors, report.warnings), ([], []))
        self.assertTrue(content.decode("iso-8859-15").splitlines()[1].startswith("AN\t"))

    def test_command(self):
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        post_entry("OD", date(2026, 5, 1), "Test", [Line(acc("411000"), debit=Decimal("1")),
                                                     Line(acc("707000"), credit=Decimal("1"))], validate=True)
        out = io.StringIO()
        with self.assertRaisesMessage(CommandError, "1 anomalie"):   # SIREN non renseigné
            call_command("check_fec", "--year", "2026", stdout=out)
        self.assertIn("SIREN absent", out.getvalue())


class ExportPageCheckTest(TestCase):
    def test_check_button_shows_the_report(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        from django.test import override_settings
        from django.urls import reverse

        user = get_user_model().objects.create_user(username="c@x.fr", email="c@x.fr", password="x" * 12)
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label="accounting"))
        self.client.force_login(user)
        post_entry("OD", date(2024, 5, 1), "Test", [Line(acc("411000"), debit=Decimal("1")),
                                                     Line(acc("707000"), credit=Decimal("1"))], validate=True)
        with override_settings(ACCOUNTING={}):
            url = reverse("accounting:exports")
            page = self.client.get(url, {"start": "2024-01-01", "end": "2024-12-31", "check": "fec"})
            self.assertContains(page, "SIREN absent")
            from .models import AccountingSettings
            AccountingSettings.objects.filter(pk=1).update(siren="123456789")
            page = self.client.get(url, {"start": "2024-01-01", "end": "2024-12-31", "check": "fec"})
            self.assertContains(page, "Structure conforme")
