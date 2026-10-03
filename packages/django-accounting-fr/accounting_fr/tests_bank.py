"""Rapprochement bancaire : formats de relevés, import, pointage, écritures depuis le relevé, état, écrans."""
from datetime import date
from decimal import Decimal
import base64

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from . import api, bank, bank_formats
from .models import Account, BankLine, BankStatement, FiscalPeriod, Journal, LedgerEntry, Transaction
from .posting import Line, close_period, generate_opening_entries, post_entry

D = Decimal

CSV_FR = """Compte courant n° 00012345678;;;;
Date;Libellé;Débit euros;Crédit euros;Date de valeur
14/01/2026;VIR STRIPE PAYMENTS UK;;123,86;14/01/2026
20/01/2026;PRLV SEPA PAPETERIE MARTIN FA-2026-118;300,00;;20/01/2026
31/01/2026;FRAIS TENUE DE COMPTE;8,00;;31/01/2026
;Solde au 31/01/2026;;815,86;
""".encode("cp1252")

OFX = b"""OFXHEADER:100
DATA:OFXSGML
VERSION:102
CHARSET:1252

<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>EUR
<BANKACCTFROM><BANKID>30004<ACCTID>00012345678<ACCTTYPE>CHECKING</BANKACCTFROM>
<BANKTRANLIST><DTSTART>20260101<DTEND>20260131
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260114120000[+1:CET]<TRNAMT>123.86<FITID>2026011400001<NAME>VIR STRIPE<MEMO>PAYMENTS UK</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260131<TRNAMT>-8,00<FITID>2026013100002<NAME>FRAIS TENUE DE COMPTE</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>1115.86<DTASOF>20260131</LEDGERBAL>
</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""

CAMT = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
<Acct><Id><IBAN>FR7630004000031234567890143</IBAN></Id></Acct>
<Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">1000.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-01-01</Dt></Dt></Bal>
<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">823.86</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-01-31</Dt></Dt></Bal>
<Ntry><Amt Ccy="EUR">123.86</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts>BOOK</Sts><BookgDt><Dt>2026-01-14</Dt></BookgDt><ValDt><Dt>2026-01-14</Dt></ValDt>
  <AcctSvcrRef>ABC1</AcctSvcrRef><NtryDtls><TxDtls><RltdPties><Dbtr><Nm>STRIPE PAYMENTS UK</Nm></Dbtr></RltdPties>
  <RmtInf><Ustrd>PAYOUT po_1</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
<Ntry><Amt Ccy="EUR">300.00</Amt><CdtDbtInd>DBIT</CdtDbtInd><Sts>BOOK</Sts><BookgDt><Dt>2026-01-20</Dt></BookgDt>
  <AcctSvcrRef>ABC2</AcctSvcrRef><AddtlNtryInf>PRLV SEPA PAPETERIE MARTIN</AddtlNtryInf></Ntry>
<Ntry><Amt Ccy="EUR">50.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><Sts>PDNG</Sts><BookgDt><Dt>2026-01-31</Dt></BookgDt></Ntry>
</Stmt></BkToCstmrStmt></Document>
"""


def cfonb_record(code, day="140126", amount="", label="", reference="", number="", extra=""):
    record = [" "] * 120

    def put(position, text):  # position à partir de 1, comme dans la norme
        for i, char in enumerate(text):
            record[position - 1 + i] = char

    put(1, code)
    put(3, "30004")
    put(12, "00031")
    put(17, "EUR")
    put(20, "2")
    put(22, "12345678901")
    put(35, day)
    if code == "04":
        put(43, day)
        put(49, label.ljust(31)[:31])
        put(82, number.rjust(7, "0"))
        put(105, reference.ljust(16)[:16])
    if code == "05":
        put(46, "LIB")
        put(49, extra.ljust(70)[:70])
    if amount:
        put(91, amount)
    return "".join(record)


class FormatsTest(SimpleTestCase):
    def test_amounts_and_dates(self):
        for text, expected in [("1 234,56", "1234.56"), ("-12.30", "-12.30"), ("1.234,56 €", "1234.56"),
                               ("(12,00)", "-12.00"), ("+5", "5.00"), ("1,234.56", "1234.56"), ("12,5-", "-12.50")]:
            self.assertEqual(bank_formats.amount(text), D(expected), text)
        self.assertIsNone(bank_formats.amount("abc"))
        self.assertEqual(bank_formats.parse_date("14/01/26"), date(2026, 1, 14))
        self.assertEqual(bank_formats.parse_date("2026-01-14T10:00:00"), date(2026, 1, 14))

    def test_french_csv_with_debit_and_credit_columns(self):
        statement = bank_formats.parse(CSV_FR, "releve.csv")
        self.assertEqual(statement.file_format, "csv")
        self.assertEqual([(l.date, l.amount) for l in statement.lines],
                         [(date(2026, 1, 14), D("123.86")), (date(2026, 1, 20), D("-300.00")), (date(2026, 1, 31), D("-8.00"))])
        self.assertEqual(statement.lines[0].value_date, date(2026, 1, 14))
        self.assertEqual((statement.date_start, statement.date_end), (date(2026, 1, 14), date(2026, 1, 31)))

    def test_signed_amount_csv(self):
        raw = b"Booking date,Description,Amount,Reference\n2026-01-15,STRIPE PAYOUT,123.86,po_1\n2026-01-16,Card fee,-1.20,\n"
        statement = bank_formats.parse(raw, "export.csv")
        self.assertEqual([l.amount for l in statement.lines], [D("123.86"), D("-1.20")])
        self.assertEqual(statement.lines[0].reference, "po_1")

    def test_unknown_columns_then_mapping(self):
        raw = "Jour;Texte;Valeur\n14/01/2026;VIREMENT;12,50\n".encode()
        raw = raw.replace(b"Jour", b"Quand")
        with self.assertRaises(bank_formats.ColumnsError) as ctx:
            bank_formats.parse(raw, "x.csv")
        self.assertEqual(ctx.exception.headers, ["Quand", "Texte", "Valeur"])
        statement = bank_formats.parse(raw, "x.csv", {"date": "0", "label": "1", "amount": "2"})
        self.assertEqual((statement.lines[0].label, statement.lines[0].amount), ("VIREMENT", D("12.50")))

    def test_ofx(self):
        statement = bank_formats.parse(OFX, "releve.ofx")
        self.assertEqual(statement.file_format, "ofx")
        self.assertEqual([(l.date, l.amount, l.uid) for l in statement.lines],
                         [(date(2026, 1, 14), D("123.86"), "2026011400001"), (date(2026, 1, 31), D("-8.00"), "2026013100002")])
        self.assertEqual(statement.lines[0].label, "VIR STRIPE PAYMENTS UK")
        self.assertEqual((statement.closing_balance, statement.account_number), (D("1115.86"), "00012345678"))
        self.assertEqual((statement.date_start, statement.date_end), (date(2026, 1, 1), date(2026, 1, 31)))

    def test_camt053(self):
        statement = bank_formats.parse(CAMT, "releve.xml")
        self.assertEqual(statement.file_format, "camt053")
        self.assertEqual([l.amount for l in statement.lines], [D("123.86"), D("-300.00")])  # en attente ignorée
        self.assertEqual(statement.lines[0].label, "STRIPE PAYMENTS UK PAYOUT po_1")
        self.assertEqual((statement.opening_balance, statement.closing_balance), (D("1000.00"), D("823.86")))
        self.assertEqual((statement.date_start, statement.date_end), (date(2026, 1, 1), date(2026, 1, 31)))
        self.assertEqual(statement.account_number, "FR7630004000031234567890143")

    def test_camt_refuses_entities(self):
        evil = b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]><Document>&x;</Document>'
        with self.assertRaises(bank_formats.StatementError):
            bank_formats.parse_camt(evil)

    def test_cfonb120(self):
        raw = "\r\n".join([
            cfonb_record("01", "010126", amount="0000000010000{"),
            cfonb_record("04", "140126", amount="0000000001238F", label="VIR STRIPE", number="1"),
            cfonb_record("05", "140126", extra="PAYMENTS UK PO_1"),
            cfonb_record("04", "200126", amount="0000000003000}", label="PRLV SEPA PAPETERIE", reference="FA-2026-118",
                         number="2"),
            cfonb_record("07", "310126", amount="0000000008238F"),
        ]).encode("latin-1")
        self.assertEqual(bank_formats.detect_format(raw), "cfonb120")
        statement = bank_formats.parse(raw, "releve.txt")
        self.assertEqual([(l.date, l.amount) for l in statement.lines],
                         [(date(2026, 1, 14), D("123.86")), (date(2026, 1, 20), D("-300.00"))])
        self.assertEqual(statement.lines[0].label, "VIR STRIPE PAYMENTS UK PO_1")
        self.assertEqual(statement.lines[1].reference, "FA-2026-118")
        self.assertEqual((statement.opening_balance, statement.closing_balance), (D("1000.00"), D("823.86")))
        one_line = raw.replace(b"\r\n", b"")  # fichier sans fins de ligne
        self.assertEqual(len(bank_formats.parse(one_line).lines), 2)

    def test_empty_file(self):
        with self.assertRaises(bank_formats.StatementError):
            bank_formats.parse(b"Date;Libelle;Montant\n", "vide.csv")


def acc(code):
    return Account.objects.get(code=code)


@override_settings(ACCOUNTING={})
class BankTestCase(TestCase):
    def setUp(self):
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        self.bank = acc("512000")
        post_entry("BQ", date(2026, 1, 1), "Apport", [Line(self.bank, debit=D("1000")), Line(acc("101000"), credit=D("1000"))],
                   reference="APPORT", validate=True)
        api.post_transfer("stripe:payout:po_1", date(2026, 1, 12), "po_1", "Virement Stripe", D("123.86"), "stripe")
        self.supplier = post_entry(
            "BQ", date(2026, 1, 19), "Règlement Papeterie Martin",
            [Line(acc("401000"), debit=D("300"), auxiliary_code="552100554", auxiliary_label="Papeterie Martin"),
             Line(self.bank, credit=D("300"))], reference="FA-2026-118", validate=True)

    def import_csv(self, raw=CSV_FR, name="janvier.csv", closing=D("815.86")):
        return bank.import_statement(self.bank, raw, name, closing_balance=closing)


class ImportAndMatchTest(BankTestCase):
    def test_import_matches_payout_and_supplier_payment(self):
        result = self.import_csv()
        self.assertEqual((result.created, result.duplicates, result.matched), (3, 0, 2))
        self.assertEqual(result.statement.closing_balance, D("815.86"))
        open_lines = BankLine.objects.filter(match__isnull=True)
        self.assertEqual([l.label for l in open_lines], ["FRAIS TENUE DE COMPTE"])
        payout = LedgerEntry.objects.get(account=self.bank, transaction__source_key="stripe:payout:po_1:banque")
        self.assertEqual(payout.bank_match.lines.get().amount, D("123.86"))
        self.assertTrue(payout.transaction.is_validated)

    def test_reimport_and_overlap(self):
        self.import_csv()
        with self.assertRaisesMessage(bank.BankError, "déjà été importé"):
            self.import_csv()
        overlap = CSV_FR.replace(b";Solde", b"02/02/2026;CB LIBRAIRIE;15,00;;02/02/2026\n;Solde")
        result = self.import_csv(overlap, "fevrier.csv", closing=None)
        self.assertEqual((result.created, result.duplicates), (1, 3))

    def test_same_day_identical_operations_are_kept(self):
        raw = b"Date;Libelle;Montant\n05/01/2026;CB CAFE;-3,20\n05/01/2026;CB CAFE;-3,20\n"
        self.assertEqual(self.import_csv(raw, "a.csv", None).created, 2)
        raw2 = raw + b"06/01/2026;CB CAFE;-3,20\n"
        self.assertEqual(self.import_csv(raw2, "b.csv", None).created, 1)

    def test_not_a_bank_account(self):
        with self.assertRaisesMessage(bank.BankError, "aucun journal de banque"):
            bank.import_statement(acc("411000"), CSV_FR, "x.csv")

    def test_ambiguous_amounts_use_reference_or_stay_open(self):
        for n in (1, 2):
            post_entry("BQ", date(2026, 1, 20), f"Règlement {n}", [Line(acc("401000"), debit=D("50")),
                                                                  Line(self.bank, credit=D("50"))],
                       reference=f"FAC-{n}0{n}", validate=True)
        raw = b"Date;Libelle;Montant\n21/01/2026;PRLV FOURNISSEUR;-50,00\n22/01/2026;PRLV FOURNISSEUR;-50,00\n"
        self.assertEqual(self.import_csv(raw, "x.csv", None).matched, 0)  # deux pour deux : à pointer à la main
        BankLine.objects.all().delete()
        raw = b"Date;Libelle;Montant\n23/01/2026;PRLV FAC-202 FOURNISSEUR;-50,00\n24/01/2026;PRLV FOURNISSEUR;-50,00\n"
        self.assertEqual(self.import_csv(raw, "y.csv", None).matched, 2)
        line = BankLine.objects.get(label__contains="FAC-202")
        self.assertEqual(line.match.entries.get().transaction.reference, "FAC-202")  # la référence départage

    def test_window_and_drafts(self):
        post_entry("BQ", date(2026, 3, 1), "Brouillon", [Line(self.bank, debit=D("77")), Line(acc("411000"), credit=D("77"))])
        raw = b"Date;Libelle;Montant\n01/03/2026;REMISE;77,00\n"
        self.assertEqual(self.import_csv(raw, "x.csv", None).matched, 0)  # un brouillon ne se pointe pas
        raw = b"Date;Libelle;Montant\n01/03/2026;VIREMENT STRIPE;123,86\n"
        self.assertEqual(self.import_csv(raw, "y.csv", None).matched, 0)  # virement Stripe du 12/01 : trop loin

    def test_manual_match_unmatch_and_delete(self):
        raw = b"Date;Libelle;Montant\n25/01/2026;REMISE CHEQUES;150,00\n"
        for n, amount in ((1, "100"), (2, "50")):
            post_entry("BQ", date(2026, 1, 24), f"Chèque {n}", [Line(self.bank, debit=D(amount)),
                                                              Line(acc("411000"), credit=D(amount))], validate=True)
        result = self.import_csv(raw, "x.csv", None)
        self.assertEqual(result.matched, 0)
        line = BankLine.objects.get()
        cheques = list(LedgerEntry.objects.filter(account=self.bank, transaction__description__startswith="Chèque"))
        with self.assertRaisesMessage(bank.BankError, "Montants différents"):
            bank.match(self.bank, [line.pk], [cheques[0].pk])
        found = bank.match(self.bank, [line.pk], [e.pk for e in cheques])
        self.assertEqual(found.entries.count(), 2)
        with self.assertRaisesMessage(bank.BankError, "déjà pointée"):
            bank.match(self.bank, [line.pk], [cheques[0].pk])
        with self.assertRaisesMessage(bank.BankError, "pointées"):
            bank.delete_statement(result.statement)
        bank.unmatch(self.bank, found.pk)
        self.assertFalse(LedgerEntry.objects.filter(bank_match__isnull=False).exists())
        bank.delete_statement(result.statement)
        self.assertFalse(BankLine.objects.exists())

    def test_create_entry_for_bank_fee_and_supplier_payment(self):
        self.import_csv()
        fee = BankLine.objects.get(label="FRAIS TENUE DE COMPTE")
        txn = bank.create_entry(fee, acc("627000"))
        self.assertTrue(txn.is_validated)
        self.assertEqual((txn.journal.code, txn.date, txn.amount), ("BQ", date(2026, 1, 31), D("8.00")))
        lines = {e.account.code: e for e in txn.entries.all()}
        self.assertEqual((lines["627000"].debit, lines["512000"].credit), (D("8.00"), D("8.00")))
        fee.refresh_from_db()
        self.assertEqual(fee.match.method, "entry")
        with self.assertRaisesMessage(bank.BankError, "déjà pointée"):
            bank.create_entry(fee, acc("627000"))

        api.post_purchase("fa:9", date(2026, 2, 1), "FA-9", "Facture FA-9", api.Party("552100554", "Papeterie Martin"),
                          [api.PurchaseLine(D("50.00"), D("10.00"))])
        raw = b"Date;Libelle;Montant\n05/02/2026;PRLV PAPETERIE MARTIN;-60,00\n"
        self.import_csv(raw, "fev.csv", None)
        line = BankLine.objects.get(amount=D("-60.00"))
        bank.create_entry(line, acc("401000"), auxiliary_code="552100554", auxiliary_label="Papeterie Martin")
        supplier = LedgerEntry.objects.filter(account__code="401000", auxiliary_code="552100554",
                                              transaction__date__gte=date(2026, 2, 1))
        self.assertEqual(set(supplier.values_list("reconciliation_ref", flat=True)) - {""}, {"A"})  # lettrée avec la facture
        self.assertEqual(supplier.exclude(reconciliation_ref="").count(), 2)


class StateTest(BankTestCase):
    def test_reconciled_after_booking_bank_fee(self):
        self.import_csv()
        day = date(2026, 1, 31)
        data = bank.state(self.bank, day)
        self.assertEqual(data["book_balance"], D("823.86"))
        self.assertEqual([e.transaction.reference for e in data["unmatched_entries"]], ["APPORT"])
        self.assertEqual(data["pending_lines"], D("-8.00"))
        self.assertEqual(data["bank_balance"], D("815.86"))
        self.assertEqual(data["gap"], D("1000.00"))  # l'apport, antérieur au relevé, n'y figurera jamais
        self.assertEqual(bank.mark_prior(self.bank, date(2026, 1, 13)), 1)
        bank.create_entry(BankLine.objects.get(label="FRAIS TENUE DE COMPTE"), acc("627000"))
        data = bank.state(self.bank, day)
        self.assertEqual((data["pending_lines"], data["book_balance"], data["unmatched_entries"]),
                         (D("0.00"), D("815.86"), []))
        self.assertEqual(data["gap"], D("0.00"))

    def test_opening_balance_and_dates(self):
        statement = bank.import_statement(self.bank, CAMT, "janvier.xml").statement
        self.assertEqual((statement.opening_balance, statement.closing_balance), (D("1000.00"), D("823.86")))
        self.assertEqual(bank.bank_balance(self.bank, date(2026, 1, 15)), D("1123.86"))  # 1000 + 123,86
        self.assertEqual(bank.bank_balance(self.bank, date(2026, 2, 10)), D("823.86"))
        self.assertEqual(bank.bank_balance(self.bank, date(2025, 12, 31)), D("1000.00"))
        # l'apport du 01/01 est dans le solde initial du relevé : il ne figurera sur aucune opération
        bank.mark_prior(self.bank, statement.date_start)
        self.assertEqual(bank.state(self.bank, date(2026, 1, 31))["gap"], D("0.00"))

    def test_match_dated_after_day_is_still_pending(self):
        raw = b"Date;Libelle;Montant\n02/02/2026;PRLV SEPA PAPETERIE;-300,00\n"
        bank.import_statement(self.bank, raw, "x.csv")  # 14 jours après l'écriture : pointage manuel
        bank.match(self.bank, [BankLine.objects.get().pk], [self.supplier.entries.get(account=self.bank).pk])
        data = bank.state(self.bank, date(2026, 1, 31))
        self.assertIn(self.supplier.pk, [e.transaction_id for e in data["unmatched_entries"]])
        data = bank.state(self.bank, date(2026, 2, 2))
        self.assertNotIn(self.supplier.pk, [e.transaction_id for e in data["unmatched_entries"]])

    def test_opening_entries_do_not_double_count(self):
        bank.import_statement(self.bank, CSV_FR, "janvier.csv", closing_balance=D("815.86"))
        bank.create_entry(BankLine.objects.get(label="FRAIS TENUE DE COMPTE"), acc("627000"))
        post_entry("BQ", date(2026, 12, 30), "Chèque émis", [Line(acc("401000"), debit=D("40")),
                                                             Line(self.bank, credit=D("40"))], validate=True)
        year = FiscalPeriod.objects.get(name="2026")
        close_period(year)
        nxt = FiscalPeriod.objects.create(name="2027", date_start=date(2027, 1, 1), date_end=date(2027, 12, 31))
        generate_opening_entries(year, nxt)
        self.assertEqual(bank.book_balance(self.bank, date(2027, 1, 2)), D("775.86"))
        data = bank.state(self.bank, date(2027, 1, 2))
        self.assertIn("Chèque émis", [e.transaction.description for e in data["unmatched_entries"]])  # en attente
        self.assertNotIn("opening", [e.transaction.type for e in data["unmatched_entries"]])
        raw = b"Date;Libelle;Montant\n04/01/2027;CHEQUE 0001;-40,00\n"
        self.assertEqual(bank.import_statement(self.bank, raw, "2027.csv").matched, 1)  # pointé par-delà la clôture


def make_user(*perms):
    u = get_user_model().objects.create_user(username="b@x.fr", email="b@x.fr", password="x" * 12)
    u.user_permissions.add(*Permission.objects.filter(content_type__app_label="accounting", codename__in=perms))
    return u


class PagesTest(BankTestCase):
    def test_read_only(self):
        self.import_csv()
        self.client.force_login(make_user("view_reports"))
        index = self.client.get(reverse("accounting:bank_index"))
        self.assertContains(index, "janvier.csv")
        self.assertNotContains(index, 'name="file"')
        page = self.client.get(reverse("accounting:bank_account", args=["BQ"]))
        self.assertContains(page, "FRAIS TENUE DE COMPTE")
        self.assertNotContains(page, "Comptabiliser")
        self.assertEqual(self.client.post(reverse("accounting:bank_account", args=["BQ"]), {"action": "auto"}).status_code, 403)
        self.assertEqual(self.client.get(reverse("accounting:bank_account", args=["411000"])).status_code, 404)
        state = self.client.get(reverse("accounting:bank_state", args=["BQ"]) + "?as_of=2026-01-31")
        self.assertContains(state, "815,86")
        self.assertEqual(self.client.get(reverse("accounting:bank_state", args=["BQ"]) + "?format=csv").status_code, 403)

    def test_import_mapping_and_entry_from_pages(self):
        user = make_user("view_reports", "reconcile_bank", "add_transaction", "validate_transaction", "export_data")
        self.client.force_login(user)
        url = reverse("accounting:bank_index")
        response = self.client.post(url, {"journal": Journal.objects.get(code="BQ").pk, "closing_balance": "815,86",
                                          "file": SimpleUploadedFile("janvier.csv", CSV_FR)}, follow=True)
        self.assertContains(response, "3 opération(s) importée(s), 2 pointée(s) automatiquement")
        statement = BankStatement.objects.get()
        self.assertEqual(statement.imported_by, user)

        odd = "Quand;Texte;Valeur\n03/02/2026;CB LIBRAIRIE;-15,00\n".encode()
        response = self.client.post(url, {"journal": Journal.objects.get(code="BQ").pk, "file": SimpleUploadedFile("odd.csv", odd)})
        self.assertContains(response, "Colonnes du relevé")
        self.assertContains(response, "1. Quand")
        response = self.client.post(url, {"journal": Journal.objects.get(code="BQ").pk, "filename": "odd.csv", "content": base64.b64encode(odd).decode(),
                                          "map_date": "0", "map_label": "1", "map_amount": "2"}, follow=True)
        self.assertContains(response, "1 opération(s) importée(s)")

        page_url = reverse("accounting:bank_account", args=["BQ"])
        fee = BankLine.objects.get(label="FRAIS TENUE DE COMPTE")
        self.client.post(page_url, {"action": "entry", "line": fee.pk, "counterpart": acc("627000").pk, "label": "Frais"})
        fee.refresh_from_db()
        self.assertIsNotNone(fee.match)
        self.assertEqual(Transaction.objects.get(source_key=f"bankline:{fee.pk}").description, "Frais")
        self.client.post(page_url, {"unmatch": fee.match_id})
        fee.refresh_from_db()
        self.assertIsNone(fee.match)
        books = BankLine.objects.get(label="CB LIBRAIRIE")
        apport = LedgerEntry.objects.get(account=self.bank, transaction__reference="APPORT")
        response = self.client.post(page_url, {"action": "match", "lines": [books.pk], "entries": [apport.pk]}, follow=True)
        self.assertContains(response, "Montants différents")
        csv = self.client.get(reverse("accounting:bank_state", args=["BQ"]) + "?as_of=2026-02-28&format=csv")
        self.assertIn("CB LIBRAIRIE", csv.content.decode("utf-8"))
        self.client.post(reverse("accounting:bank_statement_delete", args=[BankStatement.objects.get(filename="odd.csv").pk]))
        self.assertFalse(BankLine.objects.filter(label="CB LIBRAIRIE").exists())
        page = self.client.get(page_url)
        self.assertContains(page, "1 écriture antérieure au premier relevé importé ne figurera")
        self.assertContains(page, 'value="2026-01-13"')
        self.client.post(page_url, {"action": "prior", "until": "2026-01-13"})
        apport.refresh_from_db()
        self.assertEqual(apport.bank_match.method, "prior")

    def test_entry_requires_validation_right(self):
        self.import_csv()
        self.client.force_login(make_user("view_reports", "reconcile_bank"))
        fee = BankLine.objects.get(label="FRAIS TENUE DE COMPTE")
        response = self.client.post(reverse("accounting:bank_account", args=["BQ"]),
                                    {"action": "entry", "line": fee.pk, "counterpart": acc("627000").pk})
        self.assertEqual(response.status_code, 403)
