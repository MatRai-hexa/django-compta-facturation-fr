"""
Synchronisation avec la plateforme agréée (lancée chaque heure par le planificateur) :
dépôt des factures électroniques, statuts, encaissements, factures reçues, e-reporting.

    python manage.py sync_einvoicing
"""
from django.core.management.base import BaseCommand

from ... import platforms
from ...einvoicing import sync


class Command(BaseCommand):
    help = "Synchronise la facturation électronique avec la plateforme agréée."

    def handle(self, *args, **options):
        platform = platforms.current()
        summary = sync()
        self.stdout.write(
            f"{platform.label} : {summary['deposited']} facture(s) déposée(s), {summary['events']} statut(s), "
            f"{summary['payments']} encaissement(s), {summary['received']} facture(s) reçue(s), "
            f"{summary['ereports']} e-reporting transmis.")
