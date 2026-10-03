"""
Dépôt manuel : la boutique prépare les fichiers, l'utilisateur les dépose sur le portail de sa
plateforme agréée et reporte les statuts (ou importe les factures reçues) dans la gestion.
Convient à toute plateforme, en attendant un connecteur automatique.
"""
from .base import Platform


class ManualPlatform(Platform):
    code = "manual"
    label = "Dépôt manuel"
    description = ("Téléchargez les factures (Factur-X) et l'e-reporting pour les déposer sur le portail de votre "
                   "plateforme agréée ; importez les factures de vos fournisseurs.")
    automatic = False

    def send_invoice(self, invoice):
        return ""  # à déposer par l'utilisateur : la facture reste « à transmettre » jusqu'à ce qu'il la marque déposée

    def send_payment(self, invoice, amount, day):
        return None

    def send_buyer_status(self, incoming, code, message=""):
        return None

    def send_ereport(self, report):
        return ""
