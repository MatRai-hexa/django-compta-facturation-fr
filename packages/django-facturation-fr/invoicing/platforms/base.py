"""
Interface d'une plateforme agréée (PA).

Pour brancher une plateforme réelle : sous-classer `Platform`, implémenter ses appels d'API,
puis l'ajouter à `INVOICING["PLATFORMS"]` (ou au registre par défaut). Chaque méthode reçoit des
objets du module (facture, facture reçue, e-reporting) et renvoie des données simples.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class PlatformError(Exception):
    """La plateforme a refusé la demande ou ne répond pas."""


@dataclass
class Event:
    """Changement de statut signalé par la plateforme."""
    external_id: str
    code: int
    at: datetime
    message: str = ""


@dataclass
class Received:
    """Facture fournisseur mise à disposition par la plateforme."""
    external_id: str
    filename: str
    content: bytes


class Platform:
    code = ""
    label = ""
    description = ""
    automatic = True  # False : l'utilisateur dépose et reporte les statuts lui-même
    registration_id = ""  # matricule de la plateforme (4 caractères), émetteur du e-reporting ; vide : paramètres
    company_name = ""     # raison sociale de la plateforme ; vide : paramètres

    def __init__(self, options=None):
        self.options = options or {}

    # Factures émises
    def send_invoice(self, invoice) -> str:
        """Dépose la facture (PDF Factur-X) ; renvoie son identifiant chez la plateforme."""
        raise NotImplementedError

    def send_payment(self, invoice, amount, day) -> None:
        """Statut 212 « Encaissée » (montant et date de l'encaissement)."""
        raise NotImplementedError

    def fetch_events(self, transmissions) -> list[Event]:
        """Nouveaux statuts des factures émises."""
        return []

    # Factures reçues
    def fetch_incoming(self) -> list[Received]:
        return []

    def send_buyer_status(self, incoming, code: int, message: str = "") -> None:
        """Statut donné par l'acheteur à une facture reçue (prise en charge, refus, litige…)."""
        raise NotImplementedError

    # E-reporting
    def send_ereport(self, report) -> str:
        """Dépose le flux 10 (`report.xml`, transactions ou encaissements) ; renvoie son identifiant."""
        raise NotImplementedError
