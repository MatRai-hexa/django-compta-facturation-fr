"""Lettrage automatique des comptes de tiers (même pièce, même référence, même montant, tiers soldé)."""
from django.core.management.base import BaseCommand

from ...reconciliation import auto_reconcile


class Command(BaseCommand):
    help = "Lettre automatiquement les comptes clients et fournisseurs."

    def handle(self, *args, **options):
        count = auto_reconcile()
        self.stdout.write(f"Lettrage automatique : {count} lettrage(s).")
