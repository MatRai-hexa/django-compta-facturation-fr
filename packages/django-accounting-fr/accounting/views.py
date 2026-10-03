import base64
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render as _render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .fec_check import check_fec
from . import annual, assets, bank, bank_formats, conf, exports, reconciliation, reports, seal, vat
from .forms import (
    AnalyticSectionForm, AsOfForm, DisposalForm, EntryForm, EntryLineFormSet, FixedAssetForm, ImpairmentForm, JournalForm,
    NewPeriodForm,
    PeriodFilterForm,
)
from .models import (
    Account, AccountingSettings, BankLine, BankStatement, FiscalPeriod, FixedAsset, Journal, LedgerEntry, PaymentAccount, Transaction,
)
from .posting import (
    AccountingError, Line, audit, close_period, delete_draft, generate_opening_entries, post_entry, reverse_entry,
    with_treasury_counterpart,
    validate_entry,
)


def render(request, template, context=None):
    """Rendu avec le gabarit de base choisi par le projet (réglage ACCOUNTING["BASE_TEMPLATE"])."""
    return _render(request, template, {"base_template": conf.base_template(), **(context or {})})


def perm(codename):
    """Connexion obligatoire + permission comptable (403 sinon)."""
    def decorator(view):
        return login_required(permission_required(f"accounting.{codename}", raise_exception=True)(view))
    return decorator


def _period_filter(request):
    form = PeriodFilterForm(request.GET or None)
    start, end = form.range()
    validated_only = form.is_valid() and form.cleaned_data.get("validated_only")
    return form, start, end, bool(validated_only)


def _download(content, filename, content_type):
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# === Tableau de bord ===

@perm("view_transaction")
def dashboard(request):
    return render(request, "accounting/dashboard.html", {"stats": reports.dashboard(),
                                                          "panels": conf.dashboard_panels(request)})


# === Écritures ===

@perm("view_transaction")
def entry_list(request):
    qs = Transaction.objects.select_related("journal").order_by("-date", "-id")
    journal = request.GET.get("journal")
    status = request.GET.get("status")
    search = request.GET.get("q", "").strip()
    if journal:
        qs = qs.filter(journal__code=journal)
    if status == "draft":
        qs = qs.filter(is_validated=False)
    elif status == "validated":
        qs = qs.filter(is_validated=True)
    if search:
        qs = qs.filter(Q(number__icontains=search) | Q(description__icontains=search) | Q(reference__icontains=search))
    return render(request, "accounting/entry_list.html", {
        "page_obj": Paginator(qs, 50).get_page(request.GET.get("page")),
        "journals": Journal.objects.all(), "journal": journal, "status": status, "q": search,
    })


@perm("view_transaction")
def entry_detail(request, pk):
    from .models import AnalyticSection

    txn = get_object_or_404(Transaction.objects.select_related("journal", "fiscal_period", "reversal_of"), pk=pk)
    if request.method == "POST" and request.POST.get("action") == "analytic":
        # Ventilation analytique : modifiable même après validation (hors empreinte et hors FEC)
        if not request.user.has_perm("accounting.change_ledgerentry"):
            return HttpResponse(status=403)
        sections = AnalyticSection.objects.in_bulk()
        changed = 0
        for entry in txn.entries.filter(account__code__regex=r"^(6|7)"):
            value = request.POST.get(f"analytic_{entry.pk}", "")
            section_id = int(value) if value.isdigit() and int(value) in sections else None
            if section_id != entry.analytic_id:
                LedgerEntry.objects.filter(pk=entry.pk).update(analytic_id=section_id)
                changed += 1
        audit(request.user, "analytic_assigned", txn, f"{changed} ligne(s)")
        messages.success(request, f"Ventilation analytique enregistrée ({changed} ligne{'s' if changed > 1 else ''}).")
        return redirect("accounting:entry_detail", pk=txn.pk)
    debit, credit = txn.totals()
    return render(request, "accounting/entry_detail.html", {
        "txn": txn, "entries": txn.entries.select_related("account", "analytic"), "debit": debit, "credit": credit,
        "reversals": txn.reversals.all(), "document_url": conf.document_url(txn.document_type, txn.document_id),
        "sections": AnalyticSection.objects.filter(is_active=True),
    })


@require_POST
@perm("validate_transaction")
def entry_validate(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    try:
        txn = validate_entry(txn, request.user)
        messages.success(request, f"Écriture validée sous le numéro {txn.number}.")
    except AccountingError as exc:
        messages.error(request, str(exc))
    return redirect("accounting:entry_detail", pk=pk)


@require_POST
@perm("validate_transaction")
def entry_reverse(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    try:
        reversal = reverse_entry(txn, user=request.user)
        messages.success(request, f"Contre-passation enregistrée : {reversal.number}.")
        return redirect("accounting:entry_detail", pk=reversal.pk)
    except AccountingError as exc:
        messages.error(request, str(exc))
        return redirect("accounting:entry_detail", pk=pk)


@require_POST
@perm("delete_transaction")
def entry_delete(request, pk):
    txn = get_object_or_404(Transaction, pk=pk)
    try:
        delete_draft(txn, request.user)
        messages.success(request, "Brouillon supprimé.")
        return redirect("accounting:entry_list")
    except (AccountingError, ValidationError) as exc:
        messages.error(request, str(exc))
        return redirect("accounting:entry_detail", pk=pk)


def _known_tiers(limit=1000):
    """Comptes auxiliaires déjà utilisés (suggestions de saisie) : {code: libellé}."""
    tiers = {}
    for code, label in (LedgerEntry.objects.exclude(auxiliary_code="").order_by("-pk")
                        .values_list("auxiliary_code", "auxiliary_label")[:limit * 5]):
        tiers.setdefault(code, label)
        if len(tiers) >= limit:
            break
    return tiers


def _form_lines(formset):
    known = None
    lines = []
    for f in formset:
        data = f.cleaned_data
        if not data.get("account") or data.get("DELETE"):
            continue
        code = (data.get("auxiliary_code") or "").strip()
        label = ""
        if code:
            known = known if known is not None else {}
            if code not in known:
                known[code] = (LedgerEntry.objects.filter(auxiliary_code=code).exclude(auxiliary_label="")
                               .values_list("auxiliary_label", flat=True).first()) or code
            label = known[code]
        lines.append(Line(data["account"], debit=data.get("debit") or 0, credit=data.get("credit") or 0,
                          label=data.get("label", ""), auxiliary_code=code, auxiliary_label=label,
                          currency=data.get("currency", ""), currency_amount=data.get("currency_amount"),
                          analytic=data.get("analytic")))
    return lines


@perm("add_transaction")
def entry_create(request):
    """Saisie manuelle d'une écriture (achats, frais, virements Stripe vers la banque...)."""
    form = EntryForm(request.POST or None, initial={"journal": Journal.objects.filter(code="OD").first()})
    formset = EntryLineFormSet(request.POST or None, prefix="lines")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        lines = with_treasury_counterpart(form.cleaned_data["journal"], _form_lines(formset), form.cleaned_data["description"])
        wants_validation = form.cleaned_data["validate"] and request.user.has_perm("accounting.validate_transaction")
        try:
            txn = post_entry(form.cleaned_data["journal"].code, form.cleaned_data["date"], form.cleaned_data["description"],
                             lines, entry_type="other", reference=form.cleaned_data["reference"], user=request.user,
                             validate=wants_validation)
            audit(request.user, "entry_created", txn, txn.description)
            messages.success(request, "Écriture enregistrée.")
            return redirect("accounting:entry_detail", pk=txn.pk)
        except AccountingError as exc:
            messages.error(request, str(exc))
    return render(request, "accounting/entry_form.html", {"form": form, "formset": formset, "tiers": _known_tiers()})


@perm("change_transaction")
def entry_edit(request, pk):
    """Modification d'un brouillon (une écriture validée ne se modifie pas : contre-passation)."""
    txn = get_object_or_404(Transaction, pk=pk)
    if txn.is_validated:
        messages.error(request, "Écriture validée : elle ne peut plus être modifiée.")
        return redirect("accounting:entry_detail", pk=pk)
    initial = {"journal": txn.journal, "date": txn.date, "reference": txn.reference, "description": txn.description}
    lines = [{"account": e.account, "label": e.label, "auxiliary_code": e.auxiliary_code, "debit": e.debit or None,
              "credit": e.credit or None, "currency": e.currency, "currency_amount": e.currency_amount, "analytic": e.analytic}
             for e in txn.entries.all()]
    form = EntryForm(request.POST or None, initial=initial)
    formset = EntryLineFormSet(request.POST or None, prefix="lines", initial=lines)
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        new_lines = with_treasury_counterpart(form.cleaned_data["journal"], _form_lines(formset),
                                              form.cleaned_data["description"])
        try:
            with transaction.atomic():
                source_key, entry_type, document = txn.source_key, txn.type, (txn.document_type, txn.document_id)
                txn.delete()
                txn = post_entry(form.cleaned_data["journal"].code, form.cleaned_data["date"],
                                 form.cleaned_data["description"], new_lines, entry_type=entry_type,
                                 reference=form.cleaned_data["reference"], source_key=source_key,
                                 document_type=document[0], document_id=document[1],
                                 user=request.user,
                                 validate=form.cleaned_data["validate"] and request.user.has_perm("accounting.validate_transaction"))
            audit(request.user, "draft_edited", txn, txn.description)
            messages.success(request, "Brouillon mis à jour.")
            return redirect("accounting:entry_detail", pk=txn.pk)
        except AccountingError as exc:
            messages.error(request, str(exc))
    return render(request, "accounting/entry_form.html", {"form": form, "formset": formset, "editing": txn,
                                                           "tiers": _known_tiers()})



# === Rapports ===

@perm("view_reports")
def trial_balance(request):
    form, start, end, validated_only = _period_filter(request)
    balance = reports.trial_balance(start, end, validated_only)
    if request.GET.get("format") == "csv":
        if not request.user.has_perm("accounting.export_data"):
            return HttpResponse(status=403)
        audit(request.user, "export_trial_balance", details=f"{start} → {end}")
        return _download(exports.trial_balance_csv(balance), f"balance_{start}_{end}.csv", "text/csv; charset=utf-8")
    return render(request, "accounting/trial_balance.html", {"form": form, "start": start, "end": end, "balance": balance})


@perm("view_reports")
def general_ledger(request, code=None):
    form, start, end, validated_only = _period_filter(request)
    if code is None:
        used = LedgerEntry.objects.values("account_id")
        return render(request, "accounting/ledger_index.html", {
            "accounts": Account.objects.filter(pk__in=used), "form": form})
    account = get_object_or_404(Account, code=code)
    return render(request, "accounting/general_ledger.html", {
        "form": form, "start": start, "end": end, "ledger": reports.general_ledger(account, start, end, validated_only),
        "reconcilable": reconciliation.is_reconcilable(account)})


@perm("view_reports")
def profit_loss(request):
    form, start, end, validated_only = _period_filter(request)
    return render(request, "accounting/profit_loss.html", {
        "form": form, "statement": reports.profit_and_loss(start, end, validated_only)})


@perm("view_reports")
def balance_sheet(request):
    form = AsOfForm(request.GET if "as_of" in request.GET else None)
    day = (form.cleaned_data.get("as_of") if form.is_valid() else None) or timezone.localdate()
    if not form.is_bound:
        form = AsOfForm(initial={"as_of": day})
    validated_only = request.GET.get("validated_only") == "on"
    sheet = reports.balance_sheet(day, validated_only)
    if request.GET.get("format") == "csv":
        if not request.user.has_perm("accounting.export_data"):
            return HttpResponse(status=403)
        audit(request.user, "export_balance_sheet", details=f"au {day}")
        return _download(exports.balance_sheet_csv(sheet), f"bilan_{day}.csv", "text/csv; charset=utf-8")
    return render(request, "accounting/balance_sheet.html", {"form": form, "sheet": sheet, "validated_only": validated_only})


@perm("view_reports")
def annual_accounts(request):
    """Comptes annuels de l'exercice (bilan détaillé et compte de résultat, avec l'exercice précédent)."""
    periods = FiscalPeriod.objects.order_by("-date_start")
    today = timezone.localdate()
    period = (periods.filter(pk=request.GET.get("period")).first() if request.GET.get("period", "").isdigit() else None) \
        or periods.filter(date_start__lte=today, date_end__gte=today).first() or periods.first()
    if period is None:
        messages.info(request, "Aucun exercice pour le moment.")
        return redirect("accounting:periods")
    data = annual.annual_accounts(period)
    if request.GET.get("format") == "csv":
        if not request.user.has_perm("accounting.export_data"):
            return HttpResponse(status=403)
        audit(request.user, "export_annual_accounts", period, period.name)
        return _download(exports.annual_accounts_csv(data), f"comptes_annuels_{period.name}.csv", "text/csv; charset=utf-8")
    return render(request, "accounting/annual_accounts.html", {"data": data, "periods": periods})


@perm("view_reports")
def vat_report(request):
    form, start, end, validated_only = _period_filter(request)
    if request.method == "POST":
        if not (request.user.has_perm("accounting.add_transaction") and request.user.has_perm("accounting.validate_transaction")):
            return HttpResponse(status=403)
        try:
            txn = vat.settle(start, end, request.user)
            audit(request.user, "vat_settled", txn, f"{start} → {end}")
            messages.success(request, f"Liquidation de la TVA passée : écriture {txn.number}.")
        except AccountingError as exc:
            messages.error(request, str(exc))
        return redirect(request.get_full_path())
    declaration = reports.ca3(start, end, validated_only)
    if request.GET.get("format") == "ca3":
        if not request.user.has_perm("accounting.export_data"):
            return HttpResponse(status=403)
        audit(request.user, "export_ca3", details=f"{start} → {end}")
        return _download(exports.ca3_csv(declaration), f"ca3_{start}_{end}.csv", "text/csv; charset=utf-8")
    return render(request, "accounting/vat_report.html", {
        "form": form, "vat": reports.vat_summary(start, end, validated_only), "settlement": vat.preview(start, end),
        "ca3": declaration})



# === Journaux ===

JOURNAL_PAGE = 100


@perm("view_reports")
def journals(request):
    """Liste des journaux et journal centralisateur (totaux par journal et par mois)."""
    form, start, end, validated_only = _period_filter(request)
    data = reports.centralizer(start, end, validated_only)
    if request.GET.get("format") == "csv":
        if not request.user.has_perm("accounting.export_data"):
            return HttpResponse(status=403)
        audit(request.user, "export_centralizer", details=f"{start} → {end}")
        return _download(exports.centralizer_csv(data), f"centralisateur_{start}_{end}.csv", "text/csv; charset=utf-8")
    used = {group["journal"].pk for group in data["journals"]}
    return render(request, "accounting/journals.html", {
        "form": form, "start": start, "end": end, "centralizer": data,
        "unused": Journal.objects.exclude(pk__in=used)})


@perm("view_reports")
def journal_detail(request, code=None):
    """Édition d'un journal (ou du livre-journal complet) : écritures, lignes et totaux."""
    form, start, end, validated_only = _period_filter(request)
    journal = get_object_or_404(Journal, code=code) if code else None
    transactions = reports.journal_entries(journal, start, end, validated_only)
    name = f"journal_{journal.code if journal else 'general'}_{start}_{end}"
    download = request.GET.get("format")
    if download in ("csv", "xlsx"):
        if not request.user.has_perm("accounting.export_data"):
            return HttpResponse(status=403)
        audit(request.user, f"export_journal_{download}", details=f"{journal.code if journal else 'tous'} {start} → {end}")
        if download == "csv":
            return _download(exports.journal_csv(transactions), f"{name}.csv", "text/csv; charset=utf-8")
        return _download(exports.journal_xlsx(transactions, journal.label if journal else "Livre-journal"), f"{name}.xlsx",
                         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    printing = request.GET.get("print") == "1"
    page = Paginator(transactions, 10_000 if printing else JOURNAL_PAGE).get_page(request.GET.get("page"))
    return render(request, "accounting/journal.html", {
        "form": form, "start": start, "end": end, "journal": journal, "page": page, "printing": printing,
        "book": reports.journal_book(page.object_list), "totals": reports.journal_totals(journal, start, end, validated_only),
        "journals": Journal.objects.all()})


@perm("change_journal")
def journal_edit(request, code=None):
    journal = get_object_or_404(Journal, code=code) if code else None
    if journal is None and not request.user.has_perm("accounting.add_journal"):
        return HttpResponse(status=403)
    form = JournalForm(request.POST or None, instance=journal)
    if request.method == "POST":
        if "delete" in request.POST and journal is not None:
            if journal.transaction_set.exists() or PaymentAccount.objects.filter(journal=journal).exists():
                messages.error(request, "Journal utilisé par des écritures ou un moyen de paiement : suppression impossible.")
            else:
                audit(request.user, "journal_deleted", journal, journal.code)
                journal.delete()
                messages.success(request, "Journal supprimé.")
            return redirect("accounting:journals")
        if form.is_valid():
            saved = form.save()
            audit(request.user, "journal_saved", saved, saved.code)
            messages.success(request, f"Journal {saved.code} enregistré.")
            return redirect("accounting:journals")
    return render(request, "accounting/journal_form.html", {
        "form": form, "journal": journal,
        "deletable": journal is not None and not journal.transaction_set.exists()})


# === Lettrage ===

def _reconcilable(code):
    account = get_object_or_404(Account, code=code)
    if not reconciliation.is_reconcilable(account):
        raise Http404("Compte non lettrable")
    return account


def _default_account():
    settings_ = AccountingSettings.get()
    return settings_.customer_account if reconciliation.is_reconcilable(settings_.customer_account) else         reconciliation.reconcilable_accounts().first()


@perm("view_reports")
def reconciliation_view(request):
    """Comptes de tiers : soldes non lettrés par client ou fournisseur, lettrage automatique."""
    accounts = list(reconciliation.reconcilable_accounts().filter(ledgerentry__isnull=False).distinct())
    code = request.GET.get("account")
    account = _reconcilable(code) if code else _default_account()
    if request.method == "POST":
        if not request.user.has_perm("accounting.reconcile_entries"):
            return HttpResponse(status=403)
        count = reconciliation.auto_reconcile(account if request.POST.get("scope") == "account" else None, request.user)
        messages.success(request, f"Lettrage automatique : {count} lettrage(s).")
        return redirect(f"{request.path}?account={account.code}")
    search = request.GET.get("q", "").strip()
    rows = reconciliation.tiers(account, search) if account else []
    if request.GET.get("all") != "1":
        rows = [r for r in rows if r["balance"]]
    return render(request, "accounting/reconciliation.html", {
        "accounts": accounts, "account": account, "q": search, "show_all": request.GET.get("all") == "1",
        "page": Paginator(rows, 100).get_page(request.GET.get("page")),
        "balanced": sum(1 for r in reconciliation.tiers(account) if not r["balance"]) if account else 0,
        "payable": account is not None and reconciliation.is_payable(account),
        "closed_without_opening": _closed_without_opening()})


def _closed_without_opening():
    """Dernier exercice clôturé dont les à-nouveaux ne sont pas générés (ses soldes de tiers manquent)."""
    last = FiscalPeriod.objects.filter(is_closed=True).order_by("-date_end").first()
    if last and FiscalPeriod.objects.filter(date_start__gt=last.date_end).exists() and             not Transaction.objects.filter(source_key__startswith="opening:", fiscal_period__date_start__gt=last.date_end).exists():
        return last
    return None


@perm("view_reports")
def reconciliation_account(request, code):
    """Lettrage manuel des lignes d'un compte, par tiers."""
    account = _reconcilable(code)
    auxiliary = request.GET.get("aux")
    if request.method == "POST":
        if not request.user.has_perm("accounting.reconcile_entries"):
            return HttpResponse(status=403)
        action = request.POST.get("action")
        try:
            if request.POST.get("unreconcile"):
                letter = request.POST["unreconcile"]
                count = reconciliation.unreconcile(account, letter, request.user)
                messages.success(request, f"Lettrage {letter} défait ({count} lignes).")
            else:
                ids = [int(pk) for pk in request.POST.getlist("lines") if pk.isdigit()]
                if action in ("write_off", "write_off_exchange"):
                    if not request.user.has_perm("accounting.validate_transaction"):
                        return HttpResponse(status=403)
                    letter = reconciliation.write_off(ids, request.user, exchange=action == "write_off_exchange")
                else:
                    letter = reconciliation.reconcile(ids, request.user)
                messages.success(request, f"Lignes lettrées : {letter}.")
        except AccountingError as exc:
            messages.error(request, str(exc))
        return redirect(request.get_full_path())
    show_all = request.GET.get("all") == "1"
    qs = (reconciliation.lines(account, auxiliary) if show_all else reconciliation.open_lines(account, auxiliary))
    qs = qs.select_related("transaction", "transaction__journal").order_by("auxiliary_code", "transaction__date", "pk")
    page = Paginator(qs, 200).get_page(request.GET.get("page"))
    label = ""
    if auxiliary is not None:
        label = (LedgerEntry.objects.filter(account=account, auxiliary_code=auxiliary).exclude(auxiliary_label="")
                 .values_list("auxiliary_label", flat=True).first()) or auxiliary
    return render(request, "accounting/reconciliation_account.html", {
        "account": account, "auxiliary": auxiliary, "auxiliary_label": label, "page": page, "show_all": show_all,
        "gap_limit": reconciliation.GAP_LIMIT})


@perm("view_reports")
def aged_balance(request):
    """Balance âgée des clients ou des fournisseurs, à une date."""
    code = request.GET.get("account")
    account = _reconcilable(code) if code else _default_account()
    form = AsOfForm(request.GET if "as_of" in request.GET else None)
    as_of = form.cleaned_data.get("as_of") if form.is_valid() else None
    data = reconciliation.aged_balance(account, as_of)
    if not form.is_bound:
        form = AsOfForm(initial={"as_of": data["as_of"]})
    if request.GET.get("format") == "csv":
        if not request.user.has_perm("accounting.export_data"):
            return HttpResponse(status=403)
        audit(request.user, "export_aged_balance", account, f"{account.code} au {data['as_of']}")
        return _download(exports.aged_balance_csv(data), f"balance_agee_{account.code}_{data['as_of']}.csv",
                         "text/csv; charset=utf-8")
    return render(request, "accounting/aged_balance.html", {
        "form": form, "data": data, "accounts": reconciliation.reconcilable_accounts().filter(ledgerentry__isnull=False).distinct(),
        "payable": reconciliation.is_payable(account)})


# === Rapprochement bancaire ===

MAX_STATEMENT = 5 * 1024 * 1024
MAPPING_ROLES = [("date", "Date de l'opération", True), ("label", "Libellé", True), ("amount", "Montant (signé)", False),
                 ("debit", "Débit", False), ("credit", "Crédit", False), ("value_date", "Date de valeur", False),
                 ("reference", "Référence", False)]


def _bank(code):
    """Compte de trésorerie du journal de banque `code`."""
    journal = get_object_or_404(bank.treasury_journals(), code=code)
    return journal.account


@perm("view_reports")
def bank_index(request):
    """Journaux de banque, import des relevés, relevés importés."""
    journals = list(bank.treasury_journals())
    if request.method == "POST":
        if not request.user.has_perm("accounting.reconcile_bank"):
            return HttpResponse(status=403)
        return _bank_import(request)
    return render(request, "accounting/bank_index.html", {
        "summaries": [bank.summary(j.account) for j in journals], "journals": journals,
        "statements": Paginator(BankStatement.objects.select_related("account", "imported_by")
                                .prefetch_related("account__treasury_journals"), 30)
        .get_page(request.GET.get("page"))})


def _bank_import(request):
    account = get_object_or_404(bank.treasury_journals(), pk=request.POST.get("journal") or 0).account
    closing = bank_formats.amount(request.POST.get("closing_balance", ""))
    if "content" in request.POST:  # second passage : correspondance des colonnes d'un CSV
        try:
            raw = base64.b64decode(request.POST["content"], validate=True)
        except ValueError:
            return HttpResponse(status=400)
        filename = request.POST.get("filename", "releve.csv")
        mapping = {role: request.POST.get(f"map_{role}", "") for role in bank_formats.ROLES}
    else:
        upload = request.FILES.get("file")
        if upload is None:
            messages.error(request, "Choisissez un fichier de relevé.")
            return redirect("accounting:bank_index")
        if upload.size > MAX_STATEMENT:
            messages.error(request, "Fichier trop volumineux (5 Mo au plus).")
            return redirect("accounting:bank_index")
        raw, filename, mapping = upload.read(), upload.name, None
    if len(raw) > MAX_STATEMENT:
        return HttpResponse(status=400)
    try:
        result = bank.import_statement(account, raw, filename, request.user, mapping=mapping, closing_balance=closing)
    except bank_formats.ColumnsError as exc:
        if mapping:
            messages.error(request, str(exc))
        return render(request, "accounting/bank_mapping.html", {
            "account": account, "journal": bank.journal_of(account), "filename": filename,
            "content": base64.b64encode(raw).decode(),
            "headers": list(enumerate(exc.headers)), "closing_balance": request.POST.get("closing_balance", ""),
            "roles": MAPPING_ROLES, "mapping": mapping or {}})
    except AccountingError as exc:
        messages.error(request, str(exc))
        return redirect("accounting:bank_index")
    duplicates = f", {result.duplicates} déjà présente(s)" if result.duplicates else ""
    messages.success(request, f"{filename} : {result.created} opération(s) importée(s){duplicates}, "
                              f"{result.matched} pointée(s) automatiquement.")
    return redirect("accounting:bank_account", code=bank.journal_of(account).code)


@require_POST
@perm("reconcile_bank")
def bank_statement_delete(request, pk):
    statement = get_object_or_404(BankStatement, pk=pk)
    try:
        bank.delete_statement(statement, request.user)
        messages.success(request, f"Relevé {statement.filename} supprimé.")
    except AccountingError as exc:
        messages.error(request, str(exc))
    return redirect("accounting:bank_index")


def _ids(request, name):
    return [int(pk) for pk in request.POST.getlist(name) if pk.isdigit()]


def _bank_action(request, account):
    action = "unmatch" if request.POST.get("unmatch") else request.POST.get("action")
    if action == "auto":
        messages.success(request, f"Pointage automatique : {bank.auto_match(account, request.user)} pointage(s).")
    elif action == "match":
        bank.match(account, _ids(request, "lines"), _ids(request, "entries"), request.user)
        messages.success(request, "Opérations pointées.")
    elif action == "unmatch":
        match_id = request.POST.get("unmatch", "")
        bank.unmatch(account, int(match_id) if match_id.isdigit() else 0, request.user)
        messages.success(request, "Pointage défait.")
    elif action == "prior":
        until = bank_formats.parse_date(request.POST.get("until", ""))
        if until is None:
            raise bank.BankError("Date invalide.")
        messages.success(request, f"{bank.mark_prior(account, until, request.user)} écriture(s) antérieure(s) pointée(s).")
    elif action == "entry":
        if not (request.user.has_perm("accounting.add_transaction")
                and request.user.has_perm("accounting.validate_transaction")):
            return HttpResponse(status=403)
        line = get_object_or_404(BankLine, pk=request.POST.get("line") or 0, account=account)
        counterpart = get_object_or_404(Account, pk=request.POST.get("counterpart") or 0)
        code = request.POST.get("auxiliary_code", "").strip()
        label = ""
        if code:
            label = (LedgerEntry.objects.filter(auxiliary_code=code).exclude(auxiliary_label="")
                     .values_list("auxiliary_label", flat=True).first()) or code
        txn = bank.create_entry(line, counterpart, request.POST.get("label", "").strip(), code, label, request.user)
        messages.success(request, f"Écriture {txn.number} passée et pointée.")
    return None


@perm("view_reports")
def bank_account(request, code):
    """Pointage : opérations du relevé et lignes d'écriture du compte de banque."""
    account = _bank(code)
    if request.method == "POST":
        if not request.user.has_perm("accounting.reconcile_bank"):
            return HttpResponse(status=403)
        try:
            response = _bank_action(request, account)
            if response is not None:
                return response
        except AccountingError as exc:
            messages.error(request, str(exc))
        return redirect(request.get_full_path())
    show_all = request.GET.get("all") == "1"
    lines = BankLine.objects.filter(account=account).order_by("date", "pk")
    ledger = (bank.entries(account).select_related("transaction", "transaction__journal")
              .order_by("transaction__date", "pk"))
    if not show_all:
        lines, ledger = lines.filter(match__isnull=True), ledger.filter(bank_match__isnull=True)
    first = BankStatement.objects.filter(account=account, date_start__isnull=False).order_by("date_start").first()
    prior_until = first.date_start - timedelta(days=1) if first else None
    prior = (bank.entries(account).filter(bank_match__isnull=True, transaction__date__lte=prior_until).count()
             if prior_until else 0)
    return render(request, "accounting/bank_account.html", {
        "prior": prior, "prior_until": prior_until,
        "account": account, "journal": bank.journal_of(account), "show_all": show_all, "summary": bank.summary(account),
        "lines": Paginator(lines, 200).get_page(request.GET.get("page")),
        "ledger": Paginator(ledger, 200).get_page(request.GET.get("page")),
        "counterparts": Account.objects.filter(is_active=True, treasury_journals__isnull=True),
        "default_counterpart": bank.default_counterpart(), "tiers": _known_tiers()})


@perm("view_reports")
def bank_state(request, code):
    """État de rapprochement à une date."""
    account = _bank(code)
    form = AsOfForm(request.GET if "as_of" in request.GET else None)
    day = (form.cleaned_data.get("as_of") if form.is_valid() else None) or timezone.localdate()
    if not form.is_bound:
        form = AsOfForm(initial={"as_of": day})
    data = bank.state(account, day)
    if request.GET.get("format") == "csv":
        if not request.user.has_perm("accounting.export_data"):
            return HttpResponse(status=403)
        audit(request.user, "export_bank_state", account, f"{account.code} au {day}")
        return _download(exports.bank_state_csv(data), f"rapprochement_{code}_{day}.csv",
                         "text/csv; charset=utf-8")
    return render(request, "accounting/bank_state.html", {"form": form, "data": data})


# === Exports ===

@perm("export_data")
def export_center(request):
    form, start, end, _ = _period_filter(request)
    kind = request.GET.get("download")
    if kind:
        lines = LedgerEntry.objects.filter(transaction__date__gte=start, transaction__date__lte=end)
        audit(request.user, f"export_{kind}", details=f"{start} → {end}")
        if kind == "fec":
            filename, content = exports.fec_file(start, end, conf.company()["siren"])
            return _download(content, filename, "text/plain; charset=iso-8859-15")
        if kind == "csv":
            return _download(exports.entries_csv(lines), f"ecritures_{start}_{end}.csv", "text/csv; charset=utf-8")
        if kind == "xlsx":
            return _download(exports.entries_xlsx(lines), f"ecritures_{start}_{end}.xlsx",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    fec_report = None
    if request.GET.get("check") == "fec":
        filename, content = exports.fec_file(start, end, conf.company()["siren"])
        fec_report = check_fec(content, filename, period_start=start, period_end=end)
        audit(request.user, "fec_checked", details=f"{filename} : {len(fec_report.errors)} erreur(s)")
    return render(request, "accounting/exports.html", {
        "form": form, "start": start, "end": end, "siren": conf.company()["siren"], "fec_report": fec_report,
        "drafts": Transaction.objects.filter(is_validated=False, date__gte=start, date__lte=end).count()})


# === Analytique ===

@perm("view_reports")
def analytic_report(request):
    """Charges, produits et résultat par section analytique sur la période."""
    form, start, end, validated_only = _period_filter(request)
    data = reports.analytic_summary(start, end, validated_only)
    if request.GET.get("format") == "csv":
        if not request.user.has_perm("accounting.export_data"):
            return HttpResponse(status=403)
        audit(request.user, "export_analytic", details=f"{start} → {end}")
        return _download(exports.analytic_csv(data), f"analytique_{start}_{end}.csv", "text/csv; charset=utf-8")
    return render(request, "accounting/analytic.html", {"form": form, "data": data, "start": start, "end": end})


@perm("view_analyticsection")
def analytic_sections(request, pk=None):
    from .models import AnalyticSection

    section = get_object_or_404(AnalyticSection, pk=pk) if pk else None
    form = AnalyticSectionForm(request.POST or None, instance=section)
    if request.method == "POST":
        codename = "change_analyticsection" if section else "add_analyticsection"
        if not request.user.has_perm(f"accounting.{codename}"):
            return HttpResponse(status=403)
        if form.is_valid():
            saved = form.save()
            audit(request.user, "analytic_section_saved", saved, saved.code)
            messages.success(request, f"Section {saved.code} enregistrée.")
            return redirect("accounting:analytic_sections")
    return render(request, "accounting/analytic_sections.html", {
        "form": form, "section": section, "sections": AnalyticSection.objects.annotate(lines=Count("entries"))})


# === Immobilisations ===

def _can_post(user):
    return user.has_perm("accounting.add_transaction") and user.has_perm("accounting.validate_transaction")


@perm("view_fixedasset")
def fixed_assets(request):
    """Registre des immobilisations, dotations de l'exercice, écarts avec la comptabilité."""
    open_periods = FiscalPeriod.objects.filter(is_closed=False).order_by("date_start")
    if request.method == "POST":
        if not _can_post(request.user):
            return HttpResponse(status=403)
        period = get_object_or_404(open_periods, pk=request.POST.get("period") or 0)
        try:
            txn = assets.post_depreciations(period, request.user)
            audit(request.user, "depreciation_posted", txn, period.name)
            messages.success(request, f"Dotations de l'exercice {period.name} passées : écriture {txn.number}.")
        except AccountingError as exc:
            messages.error(request, str(exc))
        return redirect("accounting:fixed_assets")
    rows, totals = [], {"cost": Decimal("0"), "depreciation": Decimal("0"), "net": Decimal("0")}
    for asset in FixedAsset.objects.select_related("account"):
        done = assets.posted(asset) + assets.posted(asset, kind="impairment")
        net = Decimal("0") if asset.is_disposed else asset.cost - done
        rows.append({"asset": asset, "depreciation": done, "net": net})
        if not asset.is_disposed:
            totals["cost"] += asset.cost
            totals["depreciation"] += done
            totals["net"] += net
    current = open_periods.first()
    return render(request, "accounting/fixed_assets.html", {
        "rows": rows, "totals": totals, "open_periods": open_periods, "gaps": assets.register_gaps(),
        "pending": [(p, sum((a for _, a in assets.pending_depreciations(p)), Decimal("0"))) for p in open_periods],
        "current": current})


@perm("change_fixedasset")
def fixed_asset_edit(request, pk=None):
    asset = get_object_or_404(FixedAsset, pk=pk) if pk else None
    if asset is None and not request.user.has_perm("accounting.add_fixedasset"):
        return HttpResponse(status=403)
    if asset is not None and asset.is_disposed:
        messages.error(request, "Immobilisation sortie de l'actif : elle ne se modifie plus.")
        return redirect("accounting:fixed_asset_detail", pk=asset.pk)
    form = FixedAssetForm(request.POST or None, instance=asset)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        audit(request.user, "fixed_asset_saved", saved, str(saved))
        messages.success(request, f"Immobilisation « {saved.label} » enregistrée.")
        return redirect("accounting:fixed_asset_detail", pk=saved.pk)
    return render(request, "accounting/fixed_asset_form.html", {"form": form, "asset": asset})


@perm("view_fixedasset")
def fixed_asset_detail(request, pk):
    asset = get_object_or_404(FixedAsset.objects.select_related("account", "depreciation_account", "expense_account"), pk=pk)
    action = request.POST.get("action", "dispose")
    form = DisposalForm(request.POST if action == "dispose" else None)
    impairment_form = ImpairmentForm(request.POST if action == "impair" else None)
    if request.method == "POST":
        if not (_can_post(request.user) and request.user.has_perm("accounting.change_fixedasset")):
            return HttpResponse(status=403)
        if action == "impair" and impairment_form.is_valid():
            try:
                txn = assets.impair(asset, impairment_form.cleaned_data["date"], impairment_form.cleaned_data["amount"], request.user)
                audit(request.user, "fixed_asset_impaired", txn, str(asset))
                messages.success(request, f"Écriture {txn.number} passée.")
                return redirect("accounting:fixed_asset_detail", pk=asset.pk)
            except AccountingError as exc:
                messages.error(request, str(exc))
        if action == "dispose" and form.is_valid():
            try:
                txn = assets.dispose(asset, form.cleaned_data["date"], form.cleaned_data["price"], request.user,
                                     vat_rate=Decimal(form.cleaned_data["vat_rate"]) if form.cleaned_data["vat_rate"] else None)
                audit(request.user, "fixed_asset_disposed", txn, str(asset))
                messages.success(request, f"Sortie de l'actif passée : écriture {txn.number}.")
                return redirect("accounting:fixed_asset_detail", pk=asset.pk)
            except AccountingError as exc:
                messages.error(request, str(exc))
    return render(request, "accounting/fixed_asset_detail.html", {
        "asset": asset, "schedule": assets.schedule(asset), "records": asset.depreciations.select_related("transaction"),
        "impairment": assets.posted(asset, kind="impairment"), "impairment_form": impairment_form,
        "posted": assets.posted(asset), "net": asset.cost - assets.posted(asset) - assets.posted(asset, kind="impairment"),
        "depreciation_account": assets.depreciation_account(asset) if asset.method != "none" else None,
        "expense_account": assets.expense_account(asset) if asset.method != "none" else None,
        "disposal": Transaction.objects.filter(source_key=f"disposal:{asset.pk}").first(), "form": form})


# === Exercices ===

@perm("view_reports")
def integrity(request):
    """Vérification de l'empreinte chaînée et sceaux de clôture."""
    from .models import SealAnchor

    result, check = None, None
    if request.GET.get("verifier"):
        result = seal.verify()
        audit(request.user, "chain_verified", details=f"{result['count']} écriture(s), {len(result['errors'])} anomalie(s)")
    if request.method == "POST":
        index = request.POST.get("index", "").strip()
        if index.isdigit():
            ok, message = seal.check_anchor(int(index), request.POST.get("seal", ""))
            check = {"ok": ok, "message": message, "index": index}
            audit(request.user, "seal_checked", details=f"maillon {index} : {'conforme' if ok else 'NON CONFORME'}")
    return render(request, "accounting/integrity.html", {
        "result": result, "check": check, "periods": FiscalPeriod.objects.filter(closing_index__isnull=False),
        "anchors": SealAnchor.objects.all()[:15], "channels": bool(seal.recipients() or conf.get("SEAL_ARCHIVE_DIR"))})


@perm("view_transaction")
def periods(request):
    form = NewPeriodForm(request.POST or None)
    if request.method == "POST":
        if not request.user.has_perm("accounting.close_period"):
            return HttpResponse(status=403)
        if form.is_valid():
            period = FiscalPeriod.objects.create(**form.cleaned_data)
            audit(request.user, "period_created", period, period.name)
            messages.success(request, f"Exercice {period.name} créé.")
            return redirect("accounting:periods")
    return render(request, "accounting/periods.html", {
        "periods": FiscalPeriod.objects.all(), "form": form})


@require_POST
@perm("close_period")
def period_close(request, pk):
    period = get_object_or_404(FiscalPeriod, pk=pk)
    try:
        close_period(period, request.user)
        messages.success(request, f"Exercice {period.name} clôturé.")
    except AccountingError as exc:
        messages.error(request, str(exc))
    return redirect("accounting:periods")


@require_POST
@perm("close_period")
def period_opening(request, pk):
    """Génère dans l'exercice `pk` les à-nouveaux de l'exercice précédent (clôturé)."""
    period = get_object_or_404(FiscalPeriod, pk=pk)
    previous = FiscalPeriod.objects.filter(date_end__lt=period.date_start).order_by("-date_end").first()
    if previous is None:
        messages.error(request, "Aucun exercice précédent.")
    else:
        try:
            entry = generate_opening_entries(previous, period, request.user)
            messages.success(request, f"À-nouveaux générés : {entry.number}." if entry else "Aucun solde à reprendre.")
        except AccountingError as exc:
            messages.error(request, str(exc))
    return redirect("accounting:periods")
