"""
Comptabilité en partie double (plan comptable général français).

- Une écriture (`Transaction`) appartient à un journal et à un exercice, et
  regroupe des lignes (`LedgerEntry`) dont les débits égalent les crédits.
- Une écriture validée reçoit un numéro séquentiel par journal et par exercice
  et ne peut plus être modifiée ni supprimée : on la corrige par contre-passation.
- Les écritures automatiques portent une clé de source unique (`source_key`)
  qui empêche de comptabiliser deux fois le même événement.

Toute création passe par `accounting.posting` (jamais directement par les modèles).
"""
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Sum
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def to_cents(value):
    """Somme SQL -> Decimal au centime (SQLite renvoie des décimales parasites)."""
    return (value or ZERO).quantize(CENT)


class Account(models.Model):
    """Compte du plan comptable (ex. 411000 Clients, 707000 Ventes de marchandises)."""

    ACCOUNT_TYPES = (
        ("asset", "Actif"),
        ("liability", "Passif"),
        ("equity", "Capitaux propres"),
        ("revenue", "Produits"),
        ("expense", "Charges"),
    )

    code = models.CharField("Numéro", max_length=20, unique=True)
    name = models.CharField("Intitulé", max_length=200)
    account_type = models.CharField("Nature", max_length=20, choices=ACCOUNT_TYPES, blank=True)
    is_active = models.BooleanField("Actif", default=True)
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="children")
    description = models.TextField(blank=True)

    class Meta:
        ordering = ("code",)
        verbose_name = "Compte"
        verbose_name_plural = "Plan comptable"

    def __str__(self):
        return f"{self.code} - {self.name}"

    @property
    def pcg_class(self):
        return self.code[:1]

    def get_balance(self, start_date=None, end_date=None, validated_only=False):
        """Solde débiteur (positif) ou créditeur (négatif) sur une période."""
        entries = self.ledgerentry_set.all()
        if start_date:
            entries = entries.filter(transaction__date__gte=start_date)
        if end_date:
            entries = entries.filter(transaction__date__lte=end_date)
        if validated_only:
            entries = entries.filter(transaction__is_validated=True)
        totals = entries.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        return to_cents(totals["debit"]) - to_cents(totals["credit"])


class Journal(models.Model):
    KINDS = [
        ("sales", "Ventes"),
        ("bank", "Banque / trésorerie"),
        ("purchases", "Achats"),
        ("misc", "Opérations diverses"),
        ("opening", "À-nouveaux"),
    ]

    code = models.CharField("Code", max_length=10, unique=True)
    label = models.CharField("Libellé", max_length=100)
    kind = models.CharField("Type", max_length=20, choices=KINDS, default="misc")
    account = models.ForeignKey(
        "Account", verbose_name="Compte de trésorerie", null=True, blank=True, on_delete=models.PROTECT,
        related_name="treasury_journals",
        help_text="Journal de banque : compte de contrepartie de toutes ses écritures (512…, caisse, Stripe…). "
                  "Ce compte ne se mouvemente que dans ce journal ; c'est lui qu'on rapproche avec les relevés.")

    class Meta:
        ordering = ("code",)
        verbose_name = "Journal"
        verbose_name_plural = "Journaux"
        constraints = [models.UniqueConstraint(fields=["account"], condition=Q(account__isnull=False),
                                               name="unique_treasury_account")]

    def __str__(self):
        return f"{self.code} - {self.label}"

    @property
    def is_treasury(self):
        return self.kind == "bank" and self.account_id is not None


class FiscalPeriod(models.Model):
    """Exercice comptable."""

    name = models.CharField("Nom", max_length=100)  # ex. "2026"
    date_start = models.DateField("Début")
    date_end = models.DateField("Fin")
    is_closed = models.BooleanField("Clôturé", default=False)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="closed_periods")
    closing_index = models.PositiveBigIntegerField("Rang du sceau de clôture", null=True, blank=True)
    closing_seal = models.CharField("Sceau de clôture", max_length=64, blank=True,
                                    help_text="Empreinte de la chaîne des écritures à la clôture : à conserver hors du logiciel.")

    class Meta:
        ordering = ("-date_start",)
        verbose_name = "Exercice"
        verbose_name_plural = "Exercices"

    def __str__(self):
        return f"{self.name}{' (clôturé)' if self.is_closed else ''}"

    def clean(self):
        if self.date_start and self.date_end and self.date_start >= self.date_end:
            raise ValidationError("La date de fin doit être postérieure à la date de début.")
        overlap = FiscalPeriod.objects.filter(date_start__lte=self.date_end, date_end__gte=self.date_start)
        if self.pk:
            overlap = overlap.exclude(pk=self.pk)
        if self.date_start and self.date_end and overlap.exists():
            raise ValidationError("Cet exercice chevauche un exercice existant.")

    def contains(self, day):
        return self.date_start <= day <= self.date_end


class SealChain(models.Model):
    """Dernier maillon de la chaîne des empreintes (ligne unique, verrouillée à chaque validation)."""

    last_index = models.PositiveBigIntegerField(default=0)
    last_seal = models.CharField(max_length=64, blank=True)

    class Meta:
        verbose_name = "Chaîne des empreintes"
        verbose_name_plural = "Chaîne des empreintes"


class SealAnchor(models.Model):
    """Sceau envoyé hors du logiciel (e-mail, fichier d'archive) : la copie externe fait foi."""

    CHANNELS = [("email", "E-mail"), ("file", "Fichier d'archive")]

    index = models.PositiveBigIntegerField("Maillon")
    seal = models.CharField("Sceau", max_length=64)
    reason = models.CharField("Motif", max_length=60, default="quotidien")
    channel = models.CharField("Canal", max_length=10, choices=CHANNELS)
    destination = models.CharField("Destinataires ou fichier", max_length=500)
    sent_at = models.DateTimeField(auto_now_add=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ("-sent_at", "-pk")
        verbose_name = "Envoi de sceau"
        verbose_name_plural = "Envois de sceaux"


class EntrySequence(models.Model):
    """Dernier numéro attribué par journal et par exercice."""

    journal = models.ForeignKey(Journal, on_delete=models.CASCADE)
    fiscal_period = models.ForeignKey(FiscalPeriod, on_delete=models.CASCADE)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["journal", "fiscal_period"], name="unique_entry_sequence")]


class VATRate(models.Model):
    """Taux de TVA et compte de TVA collectée associé."""

    rate = models.DecimalField("Taux", max_digits=5, decimal_places=4, unique=True, help_text="0.2000 pour 20 %")
    label = models.CharField("Libellé", max_length=60)
    collected_account = models.ForeignKey(Account, verbose_name="Compte de TVA collectée",
                                          null=True, blank=True, on_delete=models.PROTECT)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("-rate",)
        verbose_name = "Taux de TVA"
        verbose_name_plural = "Taux de TVA"

    def __str__(self):
        return self.label

    @property
    def percent(self):
        return (self.rate * 100).normalize()


class Transaction(models.Model):
    """Écriture comptable (en-tête). Les lignes sont des `LedgerEntry`."""

    TRANSACTION_TYPES = (
        ("sale", "Vente"),
        ("refund", "Avoir / remboursement"),
        ("payment", "Encaissement"),
        ("payout", "Décaissement"),
        ("fee", "Frais"),
        ("expense", "Dépense"),
        ("transfer", "Virement"),
        ("reversal", "Contre-passation"),
        ("opening", "À-nouveaux"),
        ("closing", "Clôture"),
        ("vat", "Liquidation de TVA"),
        ("vat_release", "TVA exigible à l'encaissement"),
        ("depreciation", "Dotations aux amortissements"),
        ("disposal", "Sortie d'immobilisation"),
        ("other", "Autre"),
    )

    journal = models.ForeignKey(Journal, verbose_name="Journal", on_delete=models.PROTECT)
    number = models.CharField("Numéro", max_length=30, blank=True, db_index=True,
                              help_text="Attribué à la validation, séquentiel par journal et par exercice.")
    type = models.CharField("Type", max_length=20, choices=TRANSACTION_TYPES)
    date = models.DateField("Date", default=timezone.localdate, db_index=True)
    reference = models.CharField("Pièce", max_length=128, blank=True, db_index=True)
    piece_date = models.DateField("Date de la pièce", null=True, blank=True)
    description = models.CharField("Libellé", max_length=255, blank=True)
    amount = models.DecimalField("Montant", max_digits=12, decimal_places=2, default=ZERO)
    currency = models.CharField(max_length=10, default="EUR")
    fiscal_period = models.ForeignKey(FiscalPeriod, verbose_name="Exercice", on_delete=models.PROTECT)
    # Pièce d'origine dans le projet hôte (ex. ("order", "42")) : lien générique, sans dépendance
    document_type = models.CharField("Type de pièce", max_length=40, blank=True)
    document_id = models.CharField("Identifiant de pièce", max_length=64, blank=True)
    source_key = models.CharField("Origine", max_length=100, null=True, blank=True, unique=True,
                                  help_text="Événement à l'origine d'une écriture automatique (ex. order:42:sale).")
    reversal_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversals")
    tags = models.CharField(max_length=255, blank=True, default="")

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_validated = models.BooleanField("Validée", default=False)
    validated_at = models.DateTimeField(null=True, blank=True)
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="validated_transactions")
    seal_index = models.PositiveBigIntegerField("Rang dans la chaîne", null=True, blank=True, unique=True)
    seal = models.CharField("Empreinte", max_length=64, blank=True,
                            help_text="SHA-256 du contenu de l'écriture et de l'empreinte précédente (voir accounting.seal).")

    class Meta:
        ordering = ("-date", "-id")
        indexes = [models.Index(fields=["document_type", "document_id"])]
        verbose_name = "Écriture"
        verbose_name_plural = "Écritures"
        constraints = [
            models.UniqueConstraint(fields=["number"], condition=~Q(number=""), name="unique_entry_number"),
        ]
        permissions = [
            ("close_period", "Peut clôturer un exercice"),
            ("view_reports", "Peut consulter les rapports comptables"),
            ("export_data", "Peut exporter les données comptables"),
            ("validate_transaction", "Peut valider une écriture"),
            ("reconcile_entries", "Peut lettrer les comptes de tiers"),
            ("reconcile_bank", "Peut importer les relevés et rapprocher les comptes bancaires"),
        ]

    def __str__(self):
        return f"{self.number or 'Brouillon'} · {self.description or self.get_type_display()} · {self.amount} €"

    def save(self, *args, **kwargs):
        if self.pk and Transaction.objects.filter(pk=self.pk, is_validated=True).exists():
            raise ValidationError("Écriture validée : elle ne peut plus être modifiée (passer une contre-passation).")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_validated:
            raise ValidationError("Écriture validée : suppression interdite (passer une contre-passation).")
        return super().delete(*args, **kwargs)

    def totals(self):
        totals = self.entries.aggregate(debit=Sum("debit"), credit=Sum("credit"))
        return to_cents(totals["debit"]), to_cents(totals["credit"])

    def is_balanced(self):
        debit, credit = self.totals()
        return debit > 0 and debit == credit


class AnalyticSection(models.Model):
    """Section analytique (activité, magasin, projet…) : ventile charges et produits pour le pilotage."""

    code = models.CharField("Code", max_length=20, unique=True)
    label = models.CharField("Libellé", max_length=100)
    is_active = models.BooleanField("Active", default=True)

    class Meta:
        ordering = ("code",)
        verbose_name = "Section analytique"
        verbose_name_plural = "Sections analytiques"

    def __str__(self):
        return f"{self.code} - {self.label}"


class LedgerEntry(models.Model):
    """Ligne d'écriture : un compte, un débit ou un crédit."""

    transaction = models.ForeignKey(Transaction, related_name="entries", on_delete=models.CASCADE)
    account = models.ForeignKey(Account, verbose_name="Compte", on_delete=models.PROTECT)
    debit = models.DecimalField("Débit", max_digits=12, decimal_places=2, default=ZERO)
    credit = models.DecimalField("Crédit", max_digits=12, decimal_places=2, default=ZERO)
    label = models.CharField("Libellé", max_length=200, blank=True)
    vat_rate = models.DecimalField("Taux de TVA", max_digits=5, decimal_places=4, null=True, blank=True,
                                   help_text="Renseigné sur les lignes de base HT et de TVA (récapitulatif de TVA).")
    vat_base = models.DecimalField("Base HT de cette TVA", max_digits=12, decimal_places=2, null=True, blank=True,
                                   help_text="Sur une ligne de TVA : base HT imposable correspondante (déclaration de TVA).")
    auxiliary_code = models.CharField("Compte auxiliaire", max_length=40, blank=True)
    auxiliary_label = models.CharField("Libellé auxiliaire", max_length=120, blank=True)
    # Opération en devise : montant d'origine (la comptabilité est tenue en euros) ; Montantdevise / Idevise du FEC
    currency = models.CharField("Devise", max_length=3, blank=True, help_text="Code ISO 4217 (USD, GBP, CHF…).")
    currency_amount = models.DecimalField("Montant en devise", max_digits=14, decimal_places=2, null=True, blank=True)
    # Analytique : modifiable après validation (hors empreinte), comme le lettrage
    analytic = models.ForeignKey(AnalyticSection, verbose_name="Section analytique", null=True, blank=True,
                                 on_delete=models.PROTECT, related_name="entries")

    # Lettrage (rapprochement)
    reconciliation_ref = models.CharField("Lettrage", max_length=50, blank=True, db_index=True)
    is_reconciled = models.BooleanField(default=False)
    reconciled_at = models.DateField(null=True, blank=True)
    # Pointage avec le relevé bancaire (comptes de banque)
    bank_match = models.ForeignKey("BankMatch", verbose_name="Pointage", null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="entries")

    class Meta:
        ordering = ("transaction__date", "id")
        verbose_name = "Ligne d'écriture"
        verbose_name_plural = "Lignes d'écriture"
        constraints = [
            models.CheckConstraint(
                condition=Q(debit__gte=0, credit__gte=0) & (Q(debit=0, credit__gt=0) | Q(credit=0, debit__gt=0)),
                name="ledger_entry_one_side",
            ),
        ]

    def __str__(self):
        return f"{self.account} D:{self.debit} C:{self.credit}"

    def save(self, *args, **kwargs):
        if self.transaction_id and Transaction.objects.filter(pk=self.transaction_id, is_validated=True).exists() \
                and not getattr(self, "_reconciling", False):
            raise ValidationError("Écriture validée : ses lignes ne peuvent plus être modifiées.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if Transaction.objects.filter(pk=self.transaction_id, is_validated=True).exists():
            raise ValidationError("Écriture validée : suppression interdite.")
        return super().delete(*args, **kwargs)

    def clean(self):
        if self.debit < 0 or self.credit < 0:
            raise ValidationError("Les montants doivent être positifs.")
        if (self.debit > 0) == (self.credit > 0):
            raise ValidationError("Une ligne porte soit un débit, soit un crédit.")


@receiver(pre_delete, sender=Transaction)
def _protect_validated_entry(sender, instance, **kwargs):
    if instance.is_validated:
        raise ValidationError("Écriture validée : suppression interdite (passer une contre-passation).")


@receiver(pre_delete, sender=LedgerEntry)
def _protect_validated_lines(sender, instance, **kwargs):
    if Transaction.objects.filter(pk=instance.transaction_id, is_validated=True).exists():
        raise ValidationError("Écriture validée : suppression interdite.")


class AccountingSettings(models.Model):
    """Paramètres comptables de la boutique (ligne unique) : comptes utilisés par les écritures automatiques."""

    auto_validate = models.BooleanField(
        "Valider automatiquement les écritures générées", default=True,
        help_text="Les écritures issues des ventes et des paiements sont numérotées et verrouillées dès leur création.")
    company_name = models.CharField("Raison sociale", max_length=160, blank=True,
                                    help_text="Laisser vide si le projet la fournit (réglage ACCOUNTING['COMPANY']).")
    siren = models.CharField("SIREN", max_length=14, blank=True, help_text="Utilisé pour nommer le fichier FEC.")
    customer_account = models.ForeignKey(Account, verbose_name="Clients", on_delete=models.PROTECT, related_name="+")
    sales_account = models.ForeignKey(Account, verbose_name="Ventes de marchandises", on_delete=models.PROTECT, related_name="+")
    shipping_account = models.ForeignKey(Account, verbose_name="Ports facturés", on_delete=models.PROTECT, related_name="+")
    bank_account = models.ForeignKey(Account, verbose_name="Banque", on_delete=models.PROTECT, related_name="+")
    fees_account = models.ForeignKey(Account, verbose_name="Frais bancaires et de paiement", on_delete=models.PROTECT, related_name="+")
    profit_account = models.ForeignKey(Account, verbose_name="Résultat (bénéfice)", on_delete=models.PROTECT, related_name="+")
    loss_account = models.ForeignKey(Account, verbose_name="Résultat (perte)", on_delete=models.PROTECT, related_name="+")
    supplier_account = models.ForeignKey(Account, verbose_name="Fournisseurs", null=True, blank=True,
                                         on_delete=models.PROTECT, related_name="+")
    purchases_account = models.ForeignKey(Account, verbose_name="Achats (compte par défaut)", null=True, blank=True,
                                          on_delete=models.PROTECT, related_name="+",
                                          help_text="Charge des factures fournisseurs reçues, modifiable écriture par écriture.")
    deductible_vat_account = models.ForeignKey(Account, verbose_name="TVA déductible", null=True, blank=True,
                                               on_delete=models.PROTECT, related_name="+")
    vat_payable_account = models.ForeignKey(Account, verbose_name="TVA à décaisser", null=True, blank=True,
                                            on_delete=models.PROTECT, related_name="+",
                                            help_text="Solde de la liquidation de TVA à payer (44551).")
    vat_credit_account = models.ForeignKey(Account, verbose_name="Crédit de TVA à reporter", null=True, blank=True,
                                           on_delete=models.PROTECT, related_name="+",
                                           help_text="Crédit de TVA reporté d'une déclaration sur la suivante (44567).")
    seal_recipients = models.TextField(
        "Destinataires des sceaux", blank=True,
        help_text="Adresses e-mail (une par ligne ou séparées par des virgules) qui reçoivent chaque jour le sceau de la "
                  "chaîne des écritures et le sceau de chaque clôture : expert-comptable, dirigeant. À conserver.")
    vat_on_debits = models.BooleanField(
        "TVA sur les débits (prestations de services)", default=False,
        help_text="Option pour le paiement de la TVA d'après les débits : la TVA des prestations de services est "
                  "exigible à la facturation. Sinon elle l'est à l'encaissement (en attente jusque-là).")
    pending_vat_account = models.ForeignKey(Account, verbose_name="TVA en attente d'encaissement", null=True, blank=True,
                                            on_delete=models.PROTECT, related_name="+",
                                            help_text="TVA facturée sur des prestations non encore encaissées (4458).")
    transfer_account = models.ForeignKey(Account, verbose_name="Virements internes", null=True, blank=True,
                                         on_delete=models.PROTECT, related_name="+",
                                         help_text="Compte de liaison (580) des virements entre deux journaux de trésorerie.")

    class Meta:
        verbose_name = "Paramètres comptables"
        verbose_name_plural = "Paramètres comptables"

    def __str__(self):
        return "Paramètres comptables"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls):
        obj = cls.objects.filter(pk=1).first()
        if obj is None:
            from .chart import ensure_chart_of_accounts
            obj = ensure_chart_of_accounts()
        return obj

    def bank_journal(self):
        """Journal de trésorerie du compte de banque principal."""
        journal = Journal.objects.filter(account=self.bank_account, kind="bank").first()
        if journal is None:
            from .posting import AccountingError
            raise AccountingError(f"Aucun journal de banque n'a pour compte de trésorerie {self.bank_account.code} "
                                  "(Comptabilité › Journaux).")
        return journal

    def payment_mapping_journal(self, method):
        """Journal de trésorerie d'un moyen de paiement (table PaymentAccount), sinon celui de la banque."""
        mapping = PaymentAccount.objects.filter(method=method).select_related("journal__account").first()
        return mapping.journal if mapping else self.bank_journal()

    def payment_account(self, method):
        """Compte de trésorerie d'un moyen de paiement : celui de son journal."""
        return self.payment_mapping_journal(method).account

    def payment_journal(self, method):
        """Code du journal des mouvements d'un moyen de paiement."""
        return self.payment_mapping_journal(method).code


class PaymentAccount(models.Model):
    """Journal de trésorerie d'un moyen de paiement (ex. stripe -> journal ST, compte 467100)."""

    method = models.CharField("Code du moyen de paiement", max_length=40, unique=True)
    label = models.CharField("Libellé", max_length=100)
    journal = models.ForeignKey(Journal, verbose_name="Journal de trésorerie", on_delete=models.PROTECT,
                                limit_choices_to={"kind": "bank", "account__isnull": False},
                                help_text="Encaissements, frais et virements de ce moyen ; son compte de trésorerie "
                                          "est celui du journal.")

    class Meta:
        ordering = ("method",)
        verbose_name = "Journal d'un moyen de paiement"
        verbose_name_plural = "Journaux des moyens de paiement"

    def __str__(self):
        return f"{self.label} → {self.journal.code}"

    @property
    def account(self):
        return self.journal.account


class AuditLog(models.Model):
    """Trace des actions sensibles (validation, clôture, export, remboursement...)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100, db_index=True)
    model_name = models.CharField(max_length=100)
    object_id = models.IntegerField(null=True, blank=True)
    details = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        verbose_name = "Journal d'audit"
        verbose_name_plural = "Journal d'audit"
        indexes = [
            models.Index(fields=["user", "timestamp"]),
            models.Index(fields=["model_name", "object_id"]),
            models.Index(fields=["action", "timestamp"]),
        ]

    def __str__(self):
        return f"{self.timestamp:%d/%m/%Y %H:%M} {self.action}"


# === Rapprochement bancaire ===

class BankStatement(models.Model):
    """Relevé bancaire importé (CSV, OFX, CAMT.053, CFONB 120) pour un compte de banque (512…)."""

    FORMATS = [("csv", "CSV"), ("ofx", "OFX"), ("camt053", "CAMT.053 (ISO 20022)"), ("cfonb120", "CFONB 120")]

    account = models.ForeignKey(Account, verbose_name="Compte de banque", on_delete=models.PROTECT,
                                related_name="bank_statements")
    filename = models.CharField("Fichier", max_length=255)
    file_format = models.CharField("Format", max_length=20, choices=FORMATS)
    file_sha256 = models.CharField(max_length=64)
    date_start = models.DateField("Du", null=True, blank=True)
    date_end = models.DateField("Au", null=True, blank=True)
    opening_balance = models.DecimalField("Solde initial", max_digits=14, decimal_places=2, null=True, blank=True)
    closing_balance = models.DecimalField("Solde final", max_digits=14, decimal_places=2, null=True, blank=True)
    imported_at = models.DateTimeField(auto_now_add=True)
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")

    class Meta:
        ordering = ("-date_end", "-pk")
        verbose_name = "Relevé bancaire"
        verbose_name_plural = "Relevés bancaires"
        constraints = [models.UniqueConstraint(fields=["account", "file_sha256"], name="unique_bank_statement_file")]

    def __str__(self):
        return f"{self.account.code} · {self.filename}"


class BankMatch(models.Model):
    """Pointage : lignes de relevé et lignes d'écriture du compte de banque, de même montant total."""

    METHODS = [("auto", "Automatique"), ("manual", "Manuel"), ("entry", "Écriture créée"),
               ("prior", "Antérieure au premier relevé")]

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="+")
    method = models.CharField(max_length=10, choices=METHODS, default="manual")
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        verbose_name = "Pointage bancaire"
        verbose_name_plural = "Pointages bancaires"


class BankLine(models.Model):
    """Opération d'un relevé. Montant positif : crédit sur le relevé (encaissement, débit du 512)."""

    statement = models.ForeignKey(BankStatement, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="bank_lines")
    date = models.DateField("Date", db_index=True)
    value_date = models.DateField("Date de valeur", null=True, blank=True)
    label = models.CharField("Libellé", max_length=255)
    reference = models.CharField("Référence", max_length=120, blank=True)
    amount = models.DecimalField("Montant", max_digits=14, decimal_places=2)
    fingerprint = models.CharField(max_length=64)
    match = models.ForeignKey(BankMatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="lines")

    class Meta:
        ordering = ("date", "pk")
        verbose_name = "Opération bancaire"
        verbose_name_plural = "Opérations bancaires"
        constraints = [models.UniqueConstraint(fields=["account", "fingerprint"], name="unique_bank_line")]

    def __str__(self):
        return f"{self.date:%d/%m/%Y} {self.label} {self.amount}"


# === Immobilisations ===

class FixedAsset(models.Model):
    """Immobilisation du registre : valeur d'origine, plan d'amortissement, sortie (cession ou mise au rebut)."""

    METHODS = [("linear", "Linéaire"), ("degressive", "Dégressif fiscal (biens neufs, 3 ans et plus)"),
               ("none", "Non amortissable (terrain, fonds commercial…)")]

    label = models.CharField("Désignation", max_length=200)
    reference = models.CharField("N° d'inventaire", max_length=40, blank=True)
    account = models.ForeignKey(Account, verbose_name="Compte d'immobilisation", on_delete=models.PROTECT,
                                related_name="+", help_text="Classe 2 (ex. 218300 matériel informatique).")
    depreciation_account = models.ForeignKey(Account, verbose_name="Compte d'amortissement", null=True, blank=True,
                                             on_delete=models.PROTECT, related_name="+",
                                             help_text="Vide : déduit du compte d'immobilisation (218300 → 281830).")
    expense_account = models.ForeignKey(Account, verbose_name="Compte de dotation", null=True, blank=True,
                                        on_delete=models.PROTECT, related_name="+",
                                        help_text="Vide : 681110 (incorporelles) ou 681120 (corporelles).")
    acquisition_date = models.DateField("Date d'acquisition")
    service_date = models.DateField("Mise en service", help_text="Point de départ de l'amortissement.")
    cost = models.DecimalField("Valeur d'origine HT", max_digits=14, decimal_places=2)
    method = models.CharField("Mode d'amortissement", max_length=10, choices=METHODS, default="linear")
    duration_months = models.PositiveIntegerField("Durée (mois)", null=True, blank=True,
                                                  help_text="Durée d'utilisation : 36 pour 3 ans.")
    acquisition_entry = models.ForeignKey(LedgerEntry, verbose_name="Ligne d'acquisition", null=True, blank=True,
                                          on_delete=models.SET_NULL, related_name="+")
    disposal_date = models.DateField("Date de sortie", null=True, blank=True)
    disposal_price = models.DecimalField("Prix de cession HT", max_digits=14, decimal_places=2, null=True, blank=True)
    note = models.TextField("Note", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-acquisition_date", "-pk")
        verbose_name = "Immobilisation"
        verbose_name_plural = "Immobilisations"

    def __str__(self):
        return f"{self.reference + ' · ' if self.reference else ''}{self.label}"

    def clean(self):
        errors = {}
        if self.account_id and (self.account.code[:1] != "2" or self.account.code[:2] in ("28", "29")):
            errors["account"] = "Un compte d'immobilisation de la classe 2 (hors amortissements 28 et dépréciations 29)."
        if self.cost is not None and self.cost <= 0:
            errors["cost"] = "La valeur d'origine est positive."
        if self.acquisition_date and self.service_date and self.service_date < self.acquisition_date:
            errors["service_date"] = "La mise en service ne précède pas l'acquisition."
        if self.method in ("linear", "degressive") and not self.duration_months:
            errors["duration_months"] = "Durée d'amortissement à renseigner."
        elif self.method == "degressive" and self.duration_months < 36:
            errors["duration_months"] = "L'amortissement dégressif suppose une durée d'au moins 3 ans (36 mois)."
        if errors:
            raise ValidationError(errors)

    @property
    def is_disposed(self):
        return self.disposal_date is not None


class DepreciationRecord(models.Model):
    """Dotation passée pour une immobilisation (amortissement, ou dépréciation : positive en dotation, négative en reprise)."""

    KINDS = [("depreciation", "Amortissement"), ("impairment", "Dépréciation")]

    asset = models.ForeignKey(FixedAsset, related_name="depreciations", on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=KINDS, default="depreciation")
    transaction = models.ForeignKey(Transaction, related_name="+", on_delete=models.PROTECT)
    date = models.DateField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)

    class Meta:
        ordering = ("date", "pk")
        constraints = [models.UniqueConstraint(fields=["asset", "transaction", "kind"], name="unique_depreciation_line")]
