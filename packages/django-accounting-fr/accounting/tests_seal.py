"""Empreinte chaînée des écritures validées et sceau de clôture."""
from datetime import date
from decimal import Decimal as D
import io
import os
import shutil
import tempfile

from django.core import mail
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from . import seal
from .models import Account, AccountingSettings, FiscalPeriod, LedgerEntry, SealChain, Transaction
from .posting import Line, close_period, post_entry, reverse_entry
from .tests import accountant


def acc(code):
    return Account.objects.get(code=code)


def entry(amount="10.00", day=date(2026, 3, 1), validate=True):
    return post_entry("OD", day, f"Écriture {amount}", [Line(acc("411000"), debit=D(amount)),
                                                         Line(acc("707000"), credit=D(amount))], validate=validate)


@override_settings(ACCOUNTING={})
class SealTest(TestCase):
    def setUp(self):
        self.period = FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        self.first, self.second, self.third = entry("10.00"), entry("20.00"), entry("30.00")

    def test_validated_entries_are_chained(self):
        self.assertEqual([t.seal_index for t in (self.first, self.second, self.third)], [1, 2, 3])
        self.assertEqual(self.second.seal, seal.digest(self.first.seal, seal.payload(self.second)))
        self.assertEqual(SealChain.objects.get().last_seal, self.third.seal)
        self.assertEqual(entry("5.00", validate=False).seal, "")  # brouillon : pas d'empreinte
        result = seal.verify()
        self.assertEqual((result["ok"], result["count"]), (True, 3))

    def test_lettering_and_reversal_links_do_not_break_the_chain(self):
        LedgerEntry.objects.filter(transaction=self.first).update(reconciliation_ref="A", is_reconciled=True)
        reverse_entry(self.second, date(2026, 3, 2))  # renseigne reversal_of sur la contre-passation
        self.assertTrue(seal.verify()["ok"])

    def test_direct_database_change_is_detected(self):
        LedgerEntry.objects.filter(transaction=self.second, account__code="707000").update(credit=D("2.00"))
        LedgerEntry.objects.filter(transaction=self.second, account__code="411000").update(debit=D("2.00"))
        result = seal.verify()
        self.assertFalse(result["ok"])
        self.assertEqual([(e["index"], e["number"]) for e in result["errors"]], [(2, self.second.number)])

    def test_deleted_entry_is_detected(self):
        with connection.cursor() as cursor:  # contourne les protections de l'application
            cursor.execute("DELETE FROM accounting_ledgerentry WHERE transaction_id = %s", [self.second.pk])
            cursor.execute("DELETE FROM accounting_transaction WHERE id = %s", [self.second.pk])
        result = seal.verify()
        self.assertFalse(result["ok"])
        self.assertIn("Maillon 2 manquant", result["errors"][0]["message"])

    def test_deleted_last_entry_is_detected(self):
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM accounting_ledgerentry WHERE transaction_id = %s", [self.third.pk])
            cursor.execute("DELETE FROM accounting_transaction WHERE id = %s", [self.third.pk])
        self.assertIn("Fin de chaîne", seal.verify()["errors"][0]["message"])

    def test_entry_validated_outside_the_chain_is_detected(self):
        draft = entry("7.00", validate=False)
        Transaction.objects.filter(pk=draft.pk).update(is_validated=True, number="OD2026-99999")
        self.assertIn("sans empreinte", seal.verify()["errors"][0]["message"])

    def test_closing_seal(self):
        close_period(self.period)
        self.period.refresh_from_db()
        self.assertEqual((self.period.closing_index, self.period.closing_seal), (3, self.third.seal))
        self.assertTrue(seal.verify()["ok"])
        # Chaîne entièrement recalculée après une modification : le sceau de clôture ne correspond plus
        LedgerEntry.objects.filter(transaction=self.third).update(label="réécrit")
        previous = self.second.seal
        rewritten = seal.digest(previous, seal.payload(Transaction.objects.get(pk=self.third.pk)))
        Transaction.objects.filter(pk=self.third.pk).update(seal=rewritten)
        SealChain.objects.update(last_seal=rewritten)
        messages = [e["message"] for e in seal.verify()["errors"]]
        self.assertEqual(messages, ["Sceau de clôture de l'exercice 2026 non retrouvé dans la chaîne."])

    def test_command_and_page(self):
        call_command("verifier_integrite", stdout=io.StringIO())
        LedgerEntry.objects.filter(transaction=self.first).update(label="modifié")
        with self.assertRaisesMessage(CommandError, "1 anomalie"):
            call_command("verifier_integrite", stdout=io.StringIO())
        self.client.force_login(accountant())
        page = self.client.get(reverse("accounting:integrity") + "?verifier=1")
        self.assertContains(page, "Contenu modifié après validation")
        self.assertContains(self.client.get(reverse("accounting:entry_detail", args=[self.first.pk])), "Empreinte n° 1")



@override_settings(ACCOUNTING={})
class AnchorTest(TestCase):
    def setUp(self):
        self.archive = tempfile.mkdtemp(prefix="sceaux-")
        FiscalPeriod.objects.create(name="2026", date_start=date(2026, 1, 1), date_end=date(2026, 12, 31))
        AccountingSettings.objects.filter(pk=1).update(seal_recipients="expert@cabinet.example, dirigeant@boutique.example")
        self.first, self.second = entry("10.00"), entry("20.00")

    def tearDown(self):
        shutil.rmtree(self.archive, ignore_errors=True)

    def test_daily_anchor_by_email_and_file_only_when_chain_changed(self):
        with override_settings(ACCOUNTING={"SEAL_ARCHIVE_DIR": self.archive}):
            sent = seal.anchor()
            self.assertEqual([(s.channel, s.index, s.error) for s in sent], [("email", 2, ""), ("file", 2, "")])
            self.assertEqual(mail.outbox[0].to, ["expert@cabinet.example", "dirigeant@boutique.example"])
            self.assertIn(self.second.seal, mail.outbox[0].body)
            self.assertEqual(seal.anchor(), [])  # chaîne inchangée
            entry("30.00")
            self.assertEqual(len(seal.anchor()), 2)
            archive = os.path.join(self.archive, os.listdir(self.archive)[0])
            lines = open(archive, encoding="utf-8").read().splitlines()
            self.assertEqual([line.split(";")[2] for line in lines], ["2", "3"])

    def test_check_kept_seal_and_detect_rewrite_after_sending(self):
        seal.anchor()
        self.assertTrue(seal.check_anchor(2, self.second.seal)[0])
        # Chaîne entièrement recalculée après l'envoi : le sceau conservé ne correspond plus
        LedgerEntry.objects.filter(transaction=self.second).update(label="réécrit")
        rewritten = seal.digest(self.first.seal, seal.payload(Transaction.objects.get(pk=self.second.pk)))
        Transaction.objects.filter(pk=self.second.pk).update(seal=rewritten)
        SealChain.objects.update(last_seal=rewritten)
        ok, message = seal.check_anchor(2, self.second.seal)
        self.assertEqual((ok, "réécrites" in message), (False, True))
        self.assertIn("différent de la chaîne", seal.verify()["errors"][0]["message"])

    def test_closing_sends_the_closing_seal(self):
        with self.captureOnCommitCallbacks(execute=True):
            close_period(FiscalPeriod.objects.get())
        self.assertIn("clôture de l'exercice 2026", mail.outbox[-1].subject)

    def test_command_and_page(self):
        out = io.StringIO()
        call_command("ancrer_sceau", stdout=out)
        self.assertIn("envoyé", out.getvalue())
        self.client.force_login(accountant())
        page = self.client.post(reverse("accounting:integrity"), {"index": "2", "seal": self.second.seal})
        self.assertContains(page, "Sceau conforme")
        self.assertContains(page, "expert@cabinet.example")
