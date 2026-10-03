"""Recalcule l'empreinte chaînée des écritures validées et les sceaux de clôture (code de sortie 1 si altération)."""
from django.core.management.base import BaseCommand, CommandError

from ...posting import audit
from ...seal import verify


class Command(BaseCommand):
    help = "Vérifie l'inaltérabilité des écritures validées (empreinte chaînée et sceaux de clôture)."

    def handle(self, *args, **options):
        result = verify()
        audit(None, "chain_verified", details=f"{result['count']} écriture(s), {len(result['errors'])} anomalie(s)")
        if result["ok"]:
            self.stdout.write(self.style.SUCCESS(
                f"Chaîne intacte : {result['count']} écriture(s) scellée(s), dernier maillon {result['last_index']} "
                f"({result['last_seal']})."))
            return
        for error in result["errors"]:
            self.stdout.write(self.style.ERROR(f"Maillon {error['index'] or '—'} {error['number']} : {error['message']}"))
        raise CommandError(f"{len(result['errors'])} anomalie(s) d'intégrité.")
