"""
Plateforme simulée (bac à sable), pour les tests et les démonstrations : aucun appel réseau.

Une facture déposée passe ensuite par 201 Émise, 202 Reçue et 203 Mise à disposition (un statut
par synchronisation). Une facture dont l'acheteur a le SIREN 000000000 est rejetée (213), pour
simuler un destinataire absent de l'annuaire.
"""
import itertools

from django.utils import timezone

from .base import Event, Platform, Received

_counter = itertools.count(1)
INBOX = []  # factures fournisseurs « mises à disposition » par la plateforme simulée
SENT_STATUSES = []  # statuts transmis (acheteur, encaissement), pour les vérifications


class SandboxPlatform(Platform):
    code = "sandbox"
    label = "Plateforme simulée (tests)"
    description = "Simule une plateforme agréée, sans rien transmettre. Pour essayer le circuit complet."
    registration_id = "0000"
    company_name = "Plateforme simulée"

    def send_invoice(self, invoice):
        return f"SBX-{invoice.number}-{next(_counter)}"

    def fetch_events(self, transmissions):
        events = []
        for transmission in transmissions:
            if transmission.status == 200 and transmission.invoice.buyer_siren == "000000000":
                events.append(Event(transmission.external_id, 213, timezone.now(), "Destinataire inconnu de l'annuaire"))
                continue
            following = {200: 201, 201: 202, 202: 203}.get(transmission.status)
            if following:
                events.append(Event(transmission.external_id, following, timezone.now()))
        return events

    def send_payment(self, invoice, amount, day):
        SENT_STATUSES.append(("212", invoice.number, str(amount), day.isoformat()))

    def fetch_incoming(self):
        received, INBOX[:] = list(INBOX), []
        return [Received(f"SBX-IN-{next(_counter)}", name, content) for name, content in received]

    def send_buyer_status(self, incoming, code, message=""):
        SENT_STATUSES.append((str(code), incoming.number, message))

    def send_ereport(self, report):
        SENT_STATUSES.append(("ereport", report.kind, report.type_code, report.transmission_id))
        return f"SBX-ER-{report.kind[0].upper()}{report.period_start:%Y%m%d}-V{report.version}"
