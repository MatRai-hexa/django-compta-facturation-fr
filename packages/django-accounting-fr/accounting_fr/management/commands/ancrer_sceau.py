"""Envoie le sceau de la chaîne des écritures hors du logiciel (e-mail, fichier d'archive) s'il a changé."""
from django.core.management.base import BaseCommand

from ... import conf
from ...seal import anchor, recipients


class Command(BaseCommand):
    help = "Envoie le dernier maillon de la chaîne des écritures validées aux destinataires des sceaux."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Envoyer même si la chaîne n'a pas changé")

    def handle(self, *args, **options):
        if not recipients() and not conf.get("SEAL_ARCHIVE_DIR"):
            self.stdout.write(self.style.WARNING(
                "Aucun destinataire de sceaux ni dossier d'archive : le sceau n'est conservé nulle part hors du logiciel."))
            return
        sent = anchor(force=options["force"])
        if not sent:
            self.stdout.write("Chaîne inchangée depuis le dernier envoi.")
        for item in sent:
            status = self.style.ERROR(f"échec : {item.error}") if item.error else self.style.SUCCESS("envoyé")
            self.stdout.write(f"Sceau du maillon {item.index} ({item.get_channel_display()}, {item.destination}) : {status}")
