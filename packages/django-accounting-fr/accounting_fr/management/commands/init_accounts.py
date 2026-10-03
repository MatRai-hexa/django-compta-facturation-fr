from django.core.management.base import BaseCommand

from accounting_fr.chart import ACCOUNTS, JOURNALS, VAT_RATES, ensure_chart_of_accounts
from accounting_fr.models import Account


class Command(BaseCommand):
    help = "Installe le plan comptable, les journaux, les taux de TVA et les paramètres par défaut (sans rien écraser)."

    def handle(self, *args, **options):
        before = Account.objects.count()
        ensure_chart_of_accounts()
        created = Account.objects.count() - before
        self.stdout.write(self.style.SUCCESS(
            f"Plan comptable prêt : {created} compte(s) ajouté(s) sur {len(ACCOUNTS)}, "
            f"{len(JOURNALS)} journaux, {len(VAT_RATES)} taux de TVA."))
