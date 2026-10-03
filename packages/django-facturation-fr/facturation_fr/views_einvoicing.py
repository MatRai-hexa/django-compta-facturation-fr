"""Pages de la facturation électronique : plateforme, factures reçues, transmissions, e-reporting."""
import csv
import io

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from . import einvoicing, ereporting, lifecycle, platforms
from .models import EReport, IncomingInvoice, InvoicingSettings, Transmission
from .views import perm, render

MAX_UPLOAD = 10 * 1024 * 1024


@perm("view_invoice")
def dashboard(request):
    platform = platforms.current()
    settings = InvoicingSettings.get()
    return render(request, "invoicing/einvoicing.html", {
        "platform": platform,
        "pending": einvoicing.pending_invoices().count(),
        "transmissions": Transmission.objects.select_related("invoice")[:20],
        "to_deposit": Transmission.objects.filter(status__isnull=True).count(),
        "rejected": Transmission.objects.filter(status__in=[lifecycle.REJECTED, lifecycle.REFUSED]).count(),
        "incoming_open": IncomingInvoice.objects.exclude(status__in=list(lifecycle.FINAL) + [lifecycle.APPROVED]).count(),
        "reports": EReport.objects.all()[:12],
        "regime": settings.get_vat_regime_display(),
        "periods": ereporting.periods(settings),
        "ereporting_problem": _ereporting_problem(settings, platform),
    })


def _ereporting_problem(settings, platform):
    try:
        ereporting.declarant(settings, platform)
    except ereporting.EReportingError as exc:
        return str(exc)
    return ""


@require_POST
@perm("add_invoice")
def sync_now(request):
    summary = einvoicing.sync()
    messages.success(request, f"Synchronisation : {summary['deposited']} déposée(s), {summary['events']} statut(s), "
                              f"{summary['received']} reçue(s), {summary['payments']} encaissement(s).")
    return redirect("invoicing:einvoicing")


@require_POST
@perm("add_invoice")
def transmission_deposited(request, pk):
    """Dépôt manuel : la facture a été déposée sur le portail de la plateforme."""
    transmission = get_object_or_404(Transmission, pk=pk)
    einvoicing.mark_deposited(transmission, request.user, request.POST.get("external_id", "").strip()[:120])
    if request.POST.get("status"):
        code = int(request.POST["status"])
        if code in lifecycle.STATUSES and code != lifecycle.DEPOSITED:
            einvoicing.record_status(transmission, code, request.POST.get("message", "")[:300], source=request.user.email)
    messages.success(request, f"Facture {transmission.invoice.number} : statut enregistré.")
    return redirect("invoicing:einvoicing")


@perm("view_invoice")
def incoming_list(request):
    qs = IncomingInvoice.objects.all()
    status = request.GET.get("status", "")
    if status.isdigit():
        qs = qs.filter(status=int(status))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "invoicing/incoming_list.html", {
        "page": page, "statuses": [(c, lifecycle.label(c)) for c in [lifecycle.AVAILABLE, *lifecycle.BUYER_STATUSES]],
        "status": status})


@require_POST
@perm("add_invoice")
def incoming_upload(request):
    """Import de factures fournisseurs (PDF Factur-X, XML CII ou UBL), pour le dépôt manuel ou un envoi par email."""
    count = 0
    for upload in request.FILES.getlist("files"):
        if upload.size > MAX_UPLOAD:
            messages.error(request, f"{upload.name} : plus de 10 Mo.")
            continue
        try:
            incoming = einvoicing.receive(upload.read(), upload.name, "manual")
            count += 1
            if incoming.status_reason:
                messages.warning(request, f"{incoming} : {incoming.status_reason}")
        except einvoicing.EInvoicingError as exc:
            messages.error(request, f"{upload.name} : {exc}")
    if count:
        messages.success(request, f"{count} facture(s) importée(s).")
    return redirect("invoicing:incoming_list")


@perm("view_invoice")
def incoming_detail(request, pk):
    incoming = get_object_or_404(IncomingInvoice, pk=pk)
    return render(request, "invoicing/incoming_detail.html", {
        "incoming": incoming, "events": incoming.events.all(),
        "actions": [] if incoming.status in lifecycle.FINAL else [(c, lifecycle.label(c)) for c in lifecycle.BUYER_STATUSES]})


@require_POST
@perm("add_invoice")
def incoming_status(request, pk):
    incoming = get_object_or_404(IncomingInvoice, pk=pk)
    account = request.POST.get("expense_account", "").strip()
    if account != incoming.expense_account:
        IncomingInvoice.objects.filter(pk=pk).update(expense_account=account[:20])
        incoming.expense_account = account[:20]
    try:
        einvoicing.set_incoming_status(incoming, int(request.POST.get("status", 0)), request.POST.get("message", "").strip(),
                                       request.user)
        messages.success(request, f"{incoming} : {incoming.status_label.lower()}.")
    except (einvoicing.EInvoicingError, ValueError) as exc:
        messages.error(request, str(exc))
    return redirect("invoicing:incoming_detail", pk=pk)


@perm("view_invoice")
def incoming_file(request, pk):
    incoming = get_object_or_404(IncomingInvoice, pk=pk)
    if not incoming.file:
        raise Http404
    pdf = incoming.file.name.lower().endswith(".pdf")
    return FileResponse(incoming.file.open("rb"), content_type="application/pdf" if pdf else "application/xml",
                        filename=incoming.file.name.rsplit("/", 1)[-1])


@perm("view_invoice")
def ereport_download(request, pk):
    """Flux 10 (XML officiel), ou ses données en CSV (`?format=csv`) pour contrôle."""
    report = get_object_or_404(EReport, pk=pk)
    name = f"e-reporting_{report.kind}_{report.period_start}_{report.period_end}_v{report.version}"
    if request.GET.get("format") != "csv":
        if not report.xml:
            raise Http404("Flux non établi")
        response = HttpResponse(report.xml.encode("utf-8"), content_type="application/xml; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{name}.xml"'
        return response
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Bloc", "Facture", "Date", "Catégorie", "Taux de TVA", "Base HT", "TVA", "Encaissé", "Opérations"])
    for row in report.rows:
        writer.writerow([row.get("flow", ""), row.get("invoice", ""), row["date"], row.get("category", ""),
                         *(row.get(key, "").replace(".", ",") for key in ("rate", "base", "vat", "amount")),
                         row.get("count", row.get("transactions", ""))])
    response = HttpResponse(("\ufeff" + out.getvalue()).encode("utf-8"), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    return response


@require_POST
@perm("add_invoice")
def ereport_transmit(request, pk):
    report = get_object_or_404(EReport, pk=pk)
    report = einvoicing.transmit_ereport(ereporting.build(report.kind, report.period_start, report.period_end,
                                                          platform=platforms.current()), user=request.user)
    label = f"E-reporting ({report.get_kind_display().lower()}) du {report.period_start:%d/%m} au {report.period_end:%d/%m}"
    if report.transmitted_at:
        messages.success(request, f"{label} : transmis.")
    else:
        messages.error(request, f"{label} : non transmis. {report.error}")
    return redirect("invoicing:einvoicing")
