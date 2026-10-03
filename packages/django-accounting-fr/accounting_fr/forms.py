from decimal import Decimal

from django import forms
from django.utils import timezone

from .models import Account, AnalyticSection, FiscalPeriod, FixedAsset, Journal, LedgerEntry


class StyledForm(forms.Form):
    """Ajoute les classes Bootstrap aux widgets."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "form-select form-select-sm")
            else:
                widget.attrs.setdefault("class", "form-control form-control-sm")


class PeriodFilterForm(StyledForm):
    start = forms.DateField(label="Du", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end = forms.DateField(label="Au", required=False, widget=forms.DateInput(attrs={"type": "date"}))
    validated_only = forms.BooleanField(label="Écritures validées uniquement", required=False)

    def range(self):
        """(début, fin) demandés, sinon l'exercice en cours (ou l'année civile)."""
        today = timezone.localdate()
        period = FiscalPeriod.objects.filter(date_start__lte=today, date_end__gte=today).first()
        default_start = period.date_start if period else today.replace(month=1, day=1)
        data = self.cleaned_data if self.is_valid() else {}
        return data.get("start") or default_start, data.get("end") or today


class AsOfForm(StyledForm):
    as_of = forms.DateField(label="Au", required=False,
                            widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))


class EntryForm(StyledForm):
    journal = forms.ModelChoiceField(Journal.objects.all(), label="Journal")
    date = forms.DateField(label="Date", initial=timezone.localdate, widget=forms.DateInput(attrs={"type": "date"}))
    reference = forms.CharField(label="Pièce", max_length=128, required=False,
                                help_text="Numéro de facture, de relevé…")
    description = forms.CharField(label="Libellé", max_length=255)
    validate = forms.BooleanField(label="Valider immédiatement (numérotée et verrouillée)", required=False)


class EntryLineForm(StyledForm):
    account = forms.ModelChoiceField(Account.objects.filter(is_active=True), label="Compte")
    label = forms.CharField(label="Libellé", max_length=200, required=False)
    auxiliary_code = forms.CharField(label="Tiers", max_length=40, required=False,
                                     widget=forms.TextInput(attrs={"list": "tiers-codes", "placeholder": "Compte auxiliaire"}),
                                     help_text="Client ou fournisseur (comptes 40 et 41), pour le lettrage.")
    debit = forms.DecimalField(label="Débit", max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"))
    credit = forms.DecimalField(label="Crédit", max_digits=12, decimal_places=2, required=False, min_value=Decimal("0"))
    analytic = forms.ModelChoiceField(AnalyticSection.objects.filter(is_active=True), label="Section", required=False,
                                      empty_label="—")
    currency = forms.CharField(label="Devise", max_length=3, required=False,
                               widget=forms.TextInput(attrs={"placeholder": "USD", "style": "width:4.5rem"}))
    currency_amount = forms.DecimalField(label="Montant en devise", max_digits=14, decimal_places=2, required=False,
                                         min_value=Decimal("0"))

    def clean(self):
        cleaned = super().clean()
        debit, credit = cleaned.get("debit") or 0, cleaned.get("credit") or 0
        if cleaned.get("account") and bool(debit) == bool(credit):
            raise forms.ValidationError("Saisir un montant soit au débit, soit au crédit.")
        currency = (cleaned.get("currency") or "").strip().upper()
        if bool(currency) != (cleaned.get("currency_amount") is not None) or (currency and not (len(currency) == 3 and currency.isalpha())):
            raise forms.ValidationError("Devise : code ISO à 3 lettres et montant d'origine, ensemble.")
        cleaned["currency"] = currency
        return cleaned


# Une seule ligne suffit dans un journal de banque : la contrepartie de trésorerie est ajoutée
EntryLineFormSet = forms.formset_factory(EntryLineForm, extra=4, min_num=1, validate_min=True)


class NewPeriodForm(StyledForm):
    name = forms.CharField(label="Nom", max_length=100, help_text="Ex. : 2027")
    date_start = forms.DateField(label="Début", widget=forms.DateInput(attrs={"type": "date"}))
    date_end = forms.DateField(label="Fin", widget=forms.DateInput(attrs={"type": "date"}))

    def clean(self):
        cleaned = super().clean()
        period = FiscalPeriod(name=cleaned.get("name", ""), date_start=cleaned.get("date_start"),
                              date_end=cleaned.get("date_end"))
        if period.date_start and period.date_end:
            period.clean()
        return cleaned


class JournalForm(forms.ModelForm):
    class Meta:
        model = Journal
        fields = ["code", "label", "kind", "account"]
        help_texts = {"code": "2 à 10 lettres ou chiffres (ex. ST pour Stripe, BQ2 pour une seconde banque).",
                      "kind": "Un journal « Banque / trésorerie » se rapproche avec les relevés de son compte de trésorerie."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-select form-select-sm" if name in ("kind", "account")
                                          else "form-control form-control-sm")
        self.fields["account"].queryset = Account.objects.filter(is_active=True, code__regex=r"^(4|5)")
        self.fields["account"].help_text = ("Journaux de banque uniquement : contrepartie de toutes les écritures du "
                                            "journal (512…, 530 caisse, 467 Stripe…), mouvementée dans ce seul journal.")
        if self.instance.pk and self.instance.transaction_set.exists():
            # Le code figure dans la numérotation des écritures : il ne change plus une fois utilisé ;
            # le type et le compte de trésorerie non plus (le rapprochement repose sur eux)
            for name in ("code", "kind"):
                self.fields[name].disabled = True
            self.fields["code"].help_text = "Journal utilisé : son code ne peut plus changer."
            if self.instance.account_id:
                self.fields["account"].disabled = True
                self.fields["account"].help_text = "Journal utilisé : son compte de trésorerie ne peut plus changer."

    def clean(self):
        cleaned = super().clean()
        kind, account = cleaned.get("kind"), cleaned.get("account")
        if kind == "bank" and account is None:
            self.add_error("account", "Un journal de banque a un compte de trésorerie.")
        elif kind and kind != "bank" and account is not None:
            self.add_error("account", "Seul un journal de banque a un compte de trésorerie.")
        elif account is not None and "account" in self.changed_data:
            other = Journal.objects.filter(account=account).exclude(pk=self.instance.pk).first()
            if other:
                self.add_error("account", f"Ce compte est déjà celui du journal {other.code}.")
            elif (LedgerEntry.objects.filter(account=account).exclude(transaction__journal__kind="opening")
                  .exclude(transaction__journal_id=self.instance.pk).exists()):
                self.add_error("account", "Ce compte est déjà mouvementé dans d'autres journaux : "
                                          "il ne peut pas devenir le compte de trésorerie de celui-ci.")
        return cleaned

    def clean_code(self):
        code = self.cleaned_data["code"].strip().upper()
        if not code.isalnum() or not 2 <= len(code) <= 10:
            raise forms.ValidationError("2 à 10 lettres ou chiffres, sans espace.")
        return code


class FixedAssetForm(forms.ModelForm):
    class Meta:
        model = FixedAsset
        fields = ["label", "reference", "account", "acquisition_date", "service_date", "cost", "method", "duration_months",
                  "depreciation_account", "expense_account", "note"]
        widgets = {"acquisition_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "service_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "note": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        active = Account.objects.filter(is_active=True)
        self.fields["account"].queryset = active.filter(code__startswith="2").exclude(code__regex=r"^2(8|9)")
        self.fields["depreciation_account"].queryset = active.filter(code__startswith="28")
        self.fields["expense_account"].queryset = active.filter(code__startswith="68")
        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-select form-select-sm" if isinstance(field.widget, forms.Select)
                                          else "form-control form-control-sm")
        if self.instance.pk and self.instance.depreciations.exists():
            # Amortissement commencé : le plan ne change plus (sinon les dotations passées seraient fausses)
            for name in ("account", "acquisition_date", "service_date", "cost", "method", "duration_months",
                         "depreciation_account", "expense_account"):
                self.fields[name].disabled = True


class DisposalForm(StyledForm):
    date = forms.DateField(label="Date de sortie", widget=forms.DateInput(attrs={"type": "date"}))
    price = forms.DecimalField(label="Prix de cession HT", max_digits=14, decimal_places=2, required=False,
                               min_value=Decimal("0"), help_text="Vide ou 0 : mise au rebut.")
    vat_rate = forms.ChoiceField(label="TVA sur le prix", required=False,
                                 choices=[("", "Sans TVA"), ("0.2000", "20 %"), ("0.1000", "10 %"), ("0.0550", "5,5 %")])


class ImpairmentForm(StyledForm):
    date = forms.DateField(label="Date", widget=forms.DateInput(attrs={"type": "date"}))
    amount = forms.DecimalField(label="Montant", max_digits=14, decimal_places=2,
                                help_text="Positif : dépréciation ; négatif : reprise.")


class AnalyticSectionForm(forms.ModelForm):
    class Meta:
        model = AnalyticSection
        fields = ["code", "label", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.widget.attrs.setdefault("class", "form-check-input" if name == "is_active" else "form-control form-control-sm")

    def clean_code(self):
        return self.cleaned_data["code"].strip().upper()
