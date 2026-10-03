"""
Statuts du cycle de vie des factures électroniques (réforme française, spécifications externes).

Quatre statuts sont obligatoires : 200 Déposée, 210 Refusée, 212 Encaissée et 213 Rejetée.
"""

STATUSES = {
    200: ("Déposée", "plateforme du vendeur", True),
    201: ("Émise", "plateforme du vendeur", False),
    202: ("Reçue", "plateforme de l'acheteur", False),
    203: ("Mise à disposition", "plateforme de l'acheteur", False),
    204: ("Prise en charge", "acheteur", False),
    205: ("Approuvée", "acheteur", False),
    206: ("Approuvée partiellement", "acheteur", False),
    207: ("En litige", "acheteur", False),
    208: ("Suspendue", "acheteur", False),
    209: ("Complétée", "vendeur", False),
    210: ("Refusée", "acheteur", True),
    211: ("Paiement transmis", "acheteur", False),
    212: ("Encaissée", "vendeur", True),
    213: ("Rejetée", "plateforme", True),
}
CHOICES = [(code, f"{code} - {label}") for code, (label, _, _) in STATUSES.items()]

DEPOSITED, ISSUED, RECEIVED, AVAILABLE, TAKEN, APPROVED = 200, 201, 202, 203, 204, 205
PARTIALLY_APPROVED, DISPUTED, SUSPENDED, COMPLETED, REFUSED, PAYMENT_SENT, CASHED, REJECTED = (
    206, 207, 208, 209, 210, 211, 212, 213)

# Statuts qu'un acheteur peut donner à une facture reçue
BUYER_STATUSES = [TAKEN, APPROVED, PARTIALLY_APPROVED, DISPUTED, SUSPENDED, REFUSED, PAYMENT_SENT]
# Après eux, plus rien ne change (la facture est sortie du circuit ou soldée)
FINAL = {REFUSED, REJECTED, CASHED}


def label(code):
    return STATUSES.get(code, (f"Statut {code}",))[0]
