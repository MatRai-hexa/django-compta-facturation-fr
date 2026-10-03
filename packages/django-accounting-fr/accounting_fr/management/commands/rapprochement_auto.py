"""Pointage automatique des comptes de banque avec les opérations des relevés importés."""
from django.core.management.base import BaseCommand

from ...bank import auto_match, bank_accounts


class Command(BaseCommand):
    help = "Pointe automatiquement les relevés bancaires importés avec la comptabilité."

    def handle(self, *args, **options):
        count = sum(auto_match(account) for account in bank_accounts())
        self.stdout.write(f"Pointage bancaire automatique : {count} pointage(s).")
