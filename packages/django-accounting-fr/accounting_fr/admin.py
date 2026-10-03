from django.contrib import admin, messages
from django.shortcuts import redirect
from django.urls import reverse

from .models import (
    Account, AccountingSettings, AnalyticSection, AuditLog, FiscalPeriod, FixedAsset, Journal, LedgerEntry, PaymentAccount, Transaction, VATRate,
)
from .posting import AccountingError, reverse_entry, validate_entry


@admin.register(Account)
class AccountAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "account_type", "is_active"]
    list_filter = ["account_type", "is_active"]
    search_fields = ["code", "name"]
    list_per_page = 100


@admin.register(Journal)
class JournalAdmin(admin.ModelAdmin):
    list_display = ["code", "label", "kind", "account"]
    autocomplete_fields = ["account"]


@admin.register(VATRate)
class VATRateAdmin(admin.ModelAdmin):
    list_display = ["label", "rate", "collected_account", "is_active"]
    autocomplete_fields = ["collected_account"]


@admin.register(PaymentAccount)
class PaymentAccountAdmin(admin.ModelAdmin):
    list_display = ["method", "label", "journal"]


@admin.register(FiscalPeriod)
class FiscalPeriodAdmin(admin.ModelAdmin):
    list_display = ["name", "date_start", "date_end", "is_closed", "closed_at"]
    readonly_fields = ["is_closed", "closed_at", "closed_by"]

    def has_delete_permission(self, request, obj=None):
        return obj is None or not obj.transaction_set.exists()


class LedgerEntryInline(admin.TabularInline):
    model = LedgerEntry
    extra = 2
    fields = ["account", "label", "debit", "credit", "vat_rate", "auxiliary_code"]
    autocomplete_fields = ["account"]

    def _locked(self, obj):
        return obj is not None and obj.is_validated

    def get_readonly_fields(self, request, obj=None):
        return self.fields if self._locked(obj) else []

    def has_add_permission(self, request, obj=None):
        return not self._locked(obj) and super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return not self._locked(obj) and super().has_delete_permission(request, obj)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ["number_display", "date", "journal", "description", "amount", "is_validated", "document"]
    list_filter = ["journal", "is_validated", "type", "fiscal_period"]
    search_fields = ["number", "reference", "description", "document_id"]
    date_hierarchy = "date"
    inlines = [LedgerEntryInline]
    actions = ["validate_selected", "reverse_selected"]
    fields = ["journal", "type", "date", "reference", "piece_date", "description", "fiscal_period",
              "document_type", "document_id", "number", "amount", "source_key", "reversal_of", "is_validated",
              "validated_at", "validated_by"]
    raw_id_fields = ["reversal_of"]

    def get_readonly_fields(self, request, obj=None):
        always = ["number", "amount", "source_key", "reversal_of", "is_validated", "validated_at", "validated_by"]
        return self.fields if obj is not None and obj.is_validated else always

    def has_delete_permission(self, request, obj=None):
        return (obj is None or not obj.is_validated) and super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        locked = queryset.filter(is_validated=True).count()
        if locked:
            self.message_user(request, f"{locked} écriture(s) validée(s) ignorée(s) : suppression interdite.", messages.ERROR)
        queryset.filter(is_validated=False).delete()

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        txn = form.instance
        debit, credit = txn.totals()
        Transaction.objects.filter(pk=txn.pk).update(amount=debit)
        if debit != credit:
            self.message_user(request, f"Brouillon déséquilibré (débit {debit} / crédit {credit}) : "
                                       "il ne pourra pas être validé en l'état.", messages.WARNING)

    @admin.display(description="Numéro", ordering="number")
    def number_display(self, obj):
        return obj.number or "brouillon"

    @admin.display(description="Pièce")
    def document(self, obj):
        return f"{obj.document_type} {obj.document_id}".strip()

    @admin.action(description="Valider (numéroter et verrouiller)", permissions=["validate"])
    def validate_selected(self, request, queryset):
        done, errors = 0, []
        for txn in queryset.filter(is_validated=False).order_by("date", "id"):
            try:
                validate_entry(txn, request.user)
                done += 1
            except AccountingError as exc:
                errors.append(f"#{txn.pk} : {exc}")
        self.message_user(request, f"{done} écriture(s) validée(s).")
        for error in errors:
            self.message_user(request, error, messages.ERROR)

    @admin.action(description="Contre-passer (annuler par écriture inverse)", permissions=["validate"])
    def reverse_selected(self, request, queryset):
        for txn in queryset.filter(is_validated=True):
            try:
                reversal = reverse_entry(txn, user=request.user)
                self.message_user(request, f"{txn.number} contre-passée par {reversal.number}.")
            except AccountingError as exc:
                self.message_user(request, f"{txn.number} : {exc}", messages.ERROR)

    def has_validate_permission(self, request):
        return request.user.has_perm("accounting.validate_transaction")


@admin.register(AccountingSettings)
class AccountingSettingsAdmin(admin.ModelAdmin):
    autocomplete_fields = ["customer_account", "sales_account", "shipping_account", "bank_account",
                           "fees_account", "profit_account", "loss_account"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return redirect(reverse("admin:accounting_accountingsettings_change", args=[AccountingSettings.get().pk]))


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["timestamp", "user", "action", "model_name", "object_id", "details"]
    list_filter = ["action", "model_name"]
    search_fields = ["details", "user__email"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FixedAsset)
class FixedAssetAdmin(admin.ModelAdmin):
    list_display = ["label", "reference", "account", "service_date", "cost", "duration_months", "disposal_date"]
    readonly_fields = ["disposal_date", "disposal_price"]


@admin.register(AnalyticSection)
class AnalyticSectionAdmin(admin.ModelAdmin):
    list_display = ["code", "label", "is_active"]
