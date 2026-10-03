"""
Factures et avoirs.

Une facture est émise d'un bloc (numéro, vendeur figé, montants, PDF Factur-X) et ne se
modifie plus : une erreur se corrige par un avoir. Numérotation continue par série et par
année (F2026-00001, AV2026-00001), attribuée sous verrou à l'émission.
"""
from decimal import Decimal
import secrets

from django.core.exceptions import ValidationError
from django.db import models


class InvoicingSettings(models.Model):
    """Identité du vendeur et mentions des factures (ligne unique). Champ vide : valeur fournie par le projet."""

    seller_name = models.CharField("Raison sociale", max_length=160, blank=True)
    seller_legal_form = models.CharField("Forme juridique", max_length=60, blank=True, help_text="SAS, SARL, EI…")
    seller_capital = models.CharField("Capital social", max_length=40, blank=True)
    seller_address = models.CharField("Adresse", max_length=200, blank=True)
    seller_postal_code = models.CharField("Code postal", max_length=12, blank=True)
    seller_city = models.CharField("Ville", max_length=100, blank=True)
    seller_country = models.CharField("Pays (code ISO)", max_length=2, default="FR")
    seller_siren = models.CharField("SIREN", max_length=9, blank=True)
    seller_siret = models.CharField("SIRET", max_length=14, blank=True)
    seller_vat_number = models.CharField("N° de TVA intracommunautaire", max_length=20, blank=True)
    seller_rcs = models.CharField("RCS / RM", max_length=80, blank=True)
    seller_email = models.EmailField("Email", blank=True)
    seller_phone = models.CharField("Téléphone", max_length=30, blank=True)
    seller_iban = models.CharField("IBAN (paiement par virement)", max_length=34, blank=True)

    invoice_prefix = models.CharField("Préfixe des factures", max_length=10, default="F")
    credit_note_prefix = models.CharField("Préfixe des avoirs", max_length=10, default="AV")
    operation_category = models.CharField(
        "Nature des opérations", max_length=20, default="goods",
        choices=[("goods", "Livraisons de biens"), ("services", "Prestations de services"), ("both", "Biens et services")],
        help_text="Mention obligatoire des factures électroniques.")
    vat_on_debits = models.BooleanField("Option pour le paiement de la TVA d'après les débits", default=False)
    vat_exemption = models.CharField(
        "Mention d'exonération de TVA", max_length=200, blank=True,
        help_text="Ex. : « TVA non applicable, art. 293 B du CGI » (franchise en base). Imprimée si la TVA est nulle.")
    platform = models.CharField(
        "Plateforme agréée", max_length=40, default="manual",
        help_text="Plateforme qui transmet les factures électroniques, reçoit celles des fournisseurs et "
                  "le e-reporting. « Dépôt manuel » : fichiers à déposer soi-même sur le portail de la plateforme.")
    vat_regime = models.CharField(
        "Régime de TVA", max_length=20, default="real_monthly",
        choices=[("real_monthly", "Réel normal mensuel"), ("real_quarterly", "Réel normal trimestriel"),
                 ("simplified", "Régime simplifié"), ("franchise", "Franchise en base")],
        help_text="Fixe les périodes du e-reporting : transactions par décade et encaissements par mois au réel "
                  "normal mensuel ; par mois au réel trimestriel et au simplifié ; par bimestre civil en franchise.")
    platform_registration = models.CharField(
        "Matricule de la plateforme agréée", max_length=4, blank=True,
        help_text="4 caractères, attribués par l'administration à votre plateforme : émetteur du e-reporting. "
                  "Inutile si le connecteur de la plateforme le fournit.")
    platform_company = models.CharField("Raison sociale de la plateforme agréée", max_length=150, blank=True)
    late_penalties = models.TextField(
        "Pénalités de retard (clients professionnels)", blank=True,
        default="En cas de retard de paiement, pénalités au taux de trois fois le taux d'intérêt légal "
                "(art. L441-10 du code de commerce).",
        help_text="Imprimées pour les clients professionnels, avec l'indemnité forfaitaire de 40 € et l'absence d'escompte.")
    footer = models.TextField("Pied de page", blank=True)

    class Meta:
        verbose_name = "Paramètres de facturation"
        verbose_name_plural = "Paramètres de facturation"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class InvoiceSequence(models.Model):
    prefix = models.CharField(max_length=10)
    year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["prefix", "year"], name="unique_invoice_sequence")]


def new_token():
    return secrets.token_urlsafe(24)


class Invoice(models.Model):
    class Kind(models.TextChoices):
        INVOICE = "invoice", "Facture"
        CREDIT_NOTE = "credit_note", "Avoir"

    key = models.CharField("Clé", max_length=120, unique=True, help_text="Identifiant de l'événement source (idempotence).")
    kind = models.CharField("Type", max_length=20, choices=Kind.choices, default=Kind.INVOICE)
    number = models.CharField("Numéro", max_length=30, unique=True)
    issue_date = models.DateField("Date d'émission")
    sale_date = models.DateField("Date de la vente ou de la livraison", null=True, blank=True)
    due_date = models.DateField("Échéance", null=True, blank=True)
    currency = models.CharField("Devise", max_length=3, default="EUR")

    seller = models.JSONField("Vendeur (à la date d'émission)", default=dict)
    buyer_name = models.CharField("Client", max_length=160)
    buyer_address = models.CharField("Adresse", max_length=200, blank=True)
    buyer_postal_code = models.CharField("Code postal", max_length=12, blank=True)
    buyer_city = models.CharField("Ville", max_length=100, blank=True)
    buyer_country = models.CharField("Pays", max_length=2, default="FR")
    buyer_email = models.EmailField("Email", blank=True)
    buyer_siren = models.CharField("SIREN du client", max_length=9, blank=True)
    buyer_vat_number = models.CharField("N° de TVA du client", max_length=20, blank=True)
    buyer_reference = models.CharField("Référence client / commande", max_length=60, blank=True)
    delivery_address = models.CharField("Adresse de livraison", max_length=300, blank=True,
                                        help_text="Si elle diffère de l'adresse de facturation.")

    prices_include_tax = models.BooleanField("Prix saisis TTC", default=True)
    lines_total = models.DecimalField("Total des lignes HT", max_digits=12, decimal_places=2, default=Decimal("0"))
    allowances = models.JSONField("Remises", default=list, blank=True)
    allowances_total = models.DecimalField("Remises HT", max_digits=12, decimal_places=2, default=Decimal("0"))
    vat_breakdown = models.JSONField("TVA par taux", default=list)
    total_ht = models.DecimalField("Total HT", max_digits=12, decimal_places=2)
    total_vat = models.DecimalField("Total TVA", max_digits=12, decimal_places=2)
    total_ttc = models.DecimalField("Total TTC", max_digits=12, decimal_places=2)

    payment_terms = models.CharField("Conditions de paiement", max_length=200, blank=True)
    paid_at = models.DateField("Acquittée le", null=True, blank=True)
    payment_method = models.CharField("Moyen de paiement", max_length=60, blank=True)
    credited_invoice = models.ForeignKey("self", verbose_name="Facture annulée", null=True, blank=True,
                                         on_delete=models.PROTECT, related_name="credit_notes")
    note = models.TextField("Mention", blank=True)
    document_type = models.CharField("Type de pièce source", max_length=40, blank=True)
    document_id = models.CharField("Identifiant de la pièce source", max_length=64, blank=True)

    pdf = models.FileField("PDF Factur-X", upload_to="factures/%Y/", blank=True)
    pdf_sha256 = models.CharField("Empreinte SHA-256 du PDF", max_length=64, blank=True)
    public_token = models.CharField(max_length=40, unique=True, default=new_token, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    # Champs qui peuvent évoluer après l'émission (rien de ce qui est imprimé)
    MUTABLE_AFTER_ISSUE = {"pdf", "pdf_sha256"}

    class Meta:
        ordering = ["-issue_date", "-pk"]
        verbose_name = "Facture"
        verbose_name_plural = "Factures"
        indexes = [models.Index(fields=["document_type", "document_id"])]

    def __str__(self):
        return f"{self.get_kind_display()} {self.number}"

    @property
    def is_credit_note(self):
        return self.kind == self.Kind.CREDIT_NOTE

    @property
    def is_business(self):
        return bool(self.buyer_siren or self.buyer_vat_number)

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if self.pk and (update_fields is None or not set(update_fields) <= self.MUTABLE_AFTER_ISSUE):
            raise ValidationError("Une facture émise ne se modifie pas : émettre un avoir.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Une facture émise ne se supprime pas.")


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, related_name="lines", on_delete=models.CASCADE)
    position = models.PositiveIntegerField()
    description = models.CharField("Désignation", max_length=300)
    quantity = models.DecimalField("Quantité", max_digits=12, decimal_places=3)
    unit = models.CharField("Unité", max_length=3, default="C62")
    net_price = models.DecimalField("Prix unitaire HT", max_digits=14, decimal_places=6)
    net_amount = models.DecimalField("Montant HT", max_digits=12, decimal_places=2)
    vat_rate = models.DecimalField("Taux de TVA", max_digits=5, decimal_places=4)
    nature = models.CharField("Nature", max_length=10, default="goods",
                              choices=[("goods", "Livraison de biens"), ("services", "Prestation de services")],
                              help_text="Les frais de port suivent la livraison des biens qu'ils accompagnent.")

    class Meta:
        ordering = ["position"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Une ligne de facture émise ne se modifie pas.")
        super().save(*args, **kwargs)



# === Facturation électronique : transmissions, factures reçues, e-reporting ===

class Transmission(models.Model):
    """Envoi d'une facture émise à la plateforme agréée, et son cycle de vie."""

    invoice = models.OneToOneField(Invoice, related_name="transmission", on_delete=models.PROTECT)
    platform = models.CharField("Plateforme", max_length=40)
    external_id = models.CharField("Identifiant chez la plateforme", max_length=120, blank=True)
    status = models.PositiveIntegerField("Statut", null=True, blank=True)
    error = models.TextField("Dernière erreur", blank=True)
    payment_reported = models.BooleanField("Encaissement transmis", default=False)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Transmission"
        verbose_name_plural = "Transmissions"

    @property
    def status_label(self):
        from .lifecycle import label
        return label(self.status) if self.status else "À transmettre"


class IncomingInvoice(models.Model):
    """Facture (ou avoir) d'un fournisseur reçue par la plateforme, ou déposée à la main."""

    platform = models.CharField("Plateforme", max_length=40)
    external_id = models.CharField("Identifiant chez la plateforme", max_length=120, blank=True)
    file = models.FileField("Fichier reçu", upload_to="factures-recues/%Y/")
    file_sha256 = models.CharField(max_length=64, unique=True)
    flavor = models.CharField("Format", max_length=30, blank=True)
    is_credit_note = models.BooleanField("Avoir", default=False)
    number = models.CharField("Numéro", max_length=60)
    issue_date = models.DateField("Date")
    due_date = models.DateField("Échéance", null=True, blank=True)
    seller_name = models.CharField("Fournisseur", max_length=200)
    seller_siren = models.CharField("SIREN du fournisseur", max_length=20, blank=True)
    seller_vat_number = models.CharField("N° de TVA du fournisseur", max_length=30, blank=True)
    buyer_siren = models.CharField("SIREN de l'acheteur", max_length=20, blank=True)
    currency = models.CharField("Devise", max_length=3, default="EUR")
    total_ht = models.DecimalField("Total HT", max_digits=12, decimal_places=2)
    total_vat = models.DecimalField("Total TVA", max_digits=12, decimal_places=2)
    total_ttc = models.DecimalField("Total TTC", max_digits=12, decimal_places=2)
    amount_due = models.DecimalField("Reste à payer", max_digits=12, decimal_places=2)
    vat_breakdown = models.JSONField("TVA par taux", default=list)
    lines = models.JSONField("Lignes", default=list)
    data = models.JSONField("Données de la facture (EN 16931)", default=dict)
    status = models.PositiveIntegerField("Statut", default=203)
    status_reason = models.CharField("Motif", max_length=300, blank=True)
    expense_account = models.CharField("Compte de charge", max_length=20, blank=True,
                                       help_text="Vide : compte d'achat par défaut de la comptabilité.")
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-received_at"]
        verbose_name = "Facture reçue"
        verbose_name_plural = "Factures reçues"

    def __str__(self):
        return f"{self.seller_name} {self.number}"

    @property
    def status_label(self):
        from .lifecycle import label
        return label(self.status)

    @property
    def vat_rows(self):
        """TVA par taux, pour l'affichage (taux en pourcentage)."""
        return [{**row, "percent": f"{(Decimal(row['rate']) * 100).normalize():f}".replace(".", ","),
                 "base": Decimal(row["base"]), "vat": Decimal(row["vat"])} for row in self.vat_breakdown]

    @property
    def line_rows(self):
        return [{**line, "amount": Decimal(line["amount"] or 0)} for line in self.lines]


class LifecycleEvent(models.Model):
    """Historique des statuts, pour une facture émise (transmission) ou reçue."""

    transmission = models.ForeignKey(Transmission, null=True, blank=True, related_name="events", on_delete=models.CASCADE)
    incoming = models.ForeignKey(IncomingInvoice, null=True, blank=True, related_name="events", on_delete=models.CASCADE)
    code = models.PositiveIntegerField("Statut")
    message = models.CharField("Précision", max_length=300, blank=True)
    source = models.CharField("Origine", max_length=40, blank=True, help_text="plateforme, utilisateur…")
    sent = models.BooleanField("Transmis à la plateforme", default=False)
    at = models.DateTimeField("Date")

    class Meta:
        ordering = ["at", "pk"]

    @property
    def label(self):
        from .lifecycle import label
        return label(self.code)


class EReport(models.Model):
    """
    E-reporting (flux 10) d'une période : transactions (ventes aux particuliers par jour, factures aux
    professionnels étrangers) ou encaissements (prestations de services). Une période transmise puis
    modifiée est retransmise en rectificative (RE), qui annule et remplace la précédente.
    """

    TRANSACTIONS, PAYMENTS = "transactions", "payments"
    KINDS = [(TRANSACTIONS, "Transactions"), (PAYMENTS, "Encaissements")]

    kind = models.CharField("Données", max_length=20, choices=KINDS, default=TRANSACTIONS)
    period_start = models.DateField("Du")
    period_end = models.DateField("Au")
    rows = models.JSONField("Données", default=list)
    fingerprint = models.CharField(max_length=64, blank=True)
    total_ht = models.DecimalField("Total HT", max_digits=14, decimal_places=2, default=0)
    total_vat = models.DecimalField("Total TVA", max_digits=14, decimal_places=2, default=0)
    total_paid = models.DecimalField("Total encaissé", max_digits=14, decimal_places=2, default=0)
    version = models.PositiveIntegerField("Version", default=1)
    type_code = models.CharField("Type de transmission", max_length=2, default="IN",
                                 choices=[("IN", "Initiale"), ("RE", "Rectificative")])
    transmission_id = models.CharField("Identifiant de transmission", max_length=50, blank=True)
    xml = models.TextField("Flux 10 (XML)", blank=True)
    history = models.JSONField("Transmissions précédentes", default=list, blank=True)
    platform = models.CharField("Plateforme", max_length=40)
    external_id = models.CharField(max_length=120, blank=True)
    transmitted_at = models.DateTimeField("Transmis le", null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start", "kind"]
        constraints = [models.UniqueConstraint(fields=["kind", "period_start", "period_end"], name="unique_ereport_period")]
        verbose_name = "E-reporting"
        verbose_name_plural = "E-reporting"
