"""
Contrôle de structure d'un FEC (fichier ou exercice de la base).

    python manage.py check_fec 123456789FEC20261231.txt
    python manage.py check_fec --year 2026          # FEC de l'exercice, généré puis contrôlé
    python manage.py check_fec --year 2026 --output dossier/   # et enregistré
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from ... import conf, exports
from ...fec_check import check_fec
from ...models import FiscalPeriod


class Command(BaseCommand):
    help = "Contrôle la structure d'un fichier des écritures comptables (FEC)."

    def add_arguments(self, parser):
        parser.add_argument("file", nargs="?", help="Fichier FEC à contrôler")
        parser.add_argument("--year", help="Exercice (nom) dont le FEC est généré puis contrôlé")
        parser.add_argument("--output", help="Dossier où enregistrer le FEC généré")

    def handle(self, *args, file=None, year=None, output=None, **options):
        start = end = None
        if file:
            path = Path(file)
            content, filename = path.read_bytes(), path.name
        elif year:
            period = FiscalPeriod.objects.filter(name=year).first()
            if period is None:
                raise CommandError(f"Exercice « {year} » introuvable.")
            start, end = period.date_start, period.date_end
            filename, content = exports.fec_file(start, end, conf.company()["siren"])
            if output:
                Path(output).mkdir(parents=True, exist_ok=True)
                (Path(output) / filename).write_bytes(content)
                self.stdout.write(f"FEC enregistré : {Path(output) / filename}")
        else:
            raise CommandError("Indiquer un fichier ou --year.")

        report = check_fec(content, filename, period_start=start, period_end=end)
        stats = report.stats
        self.stdout.write(f"{filename} : {stats.get('ecritures', 0)} écriture(s), {stats.get('lignes', 0)} ligne(s), "
                          f"encodage {stats.get('encodage')}, débit {stats.get('total_debit')} / crédit {stats.get('total_credit')}")
        for error in report.errors:
            self.stdout.write(self.style.ERROR(f"ERREUR    {error}"))
        for warning in report.warnings:
            self.stdout.write(self.style.WARNING(f"ATTENTION {warning}"))
        if report.ok:
            self.stdout.write(self.style.SUCCESS("Structure conforme."))
        else:
            raise CommandError(f"{len(report.errors)} anomalie(s) bloquante(s).")
