from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect
from django.shortcuts import render as _render
from django.views.decorators.http import require_POST

from . import api, conf
from .models import Invoice, InvoicingSettings


def render(request, template, context=None):
    return _render(request, template, {"base_template": conf.base_template(), **(context or {})})


def perm(codename):
    def decorator(view):
        return login_required(permission_required(f"invoicing.{codename}", raise_exception=True)(view))
    return decorator


def _pdf_response(invoice, inline=True):
    if not invoice.pdf:
        raise Http404("PDF indisponible")
    return FileResponse(invoice.pdf.open("rb"), content_type="application/pdf", as_attachment=not inline,
                        filename=f"{invoice.number}.pdf")


@perm("view_invoice")
def invoice_list(request):
    qs = Invoice.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(buyer_name__icontains=q) | Q(buyer_email__icontains=q)
                       | Q(buyer_reference__icontains=q))
    kind = request.GET.get("kind", "")
    if kind in Invoice.Kind.values:
        qs = qs.filter(kind=kind)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "invoicing/list.html", {"page": page, "kind": kind, "kinds": Invoice.Kind.choices})


@perm("view_invoice")
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("credited_invoice"), pk=pk)
    return render(request, "invoicing/detail.html", {
        "invoice": invoice, "lines": invoice.lines.all(), "credit_notes": invoice.credit_notes.all(),
        "document_url": conf.document_url(invoice.document_type, invoice.document_id)})


@perm("view_invoice")
def invoice_pdf(request, pk):
    return _pdf_response(get_object_or_404(Invoice, pk=pk), inline=request.GET.get("download") != "1")


def invoice_public_pdf(request, token):
    """Téléchargement par le client, avec le lien secret de sa facture (sans compte)."""
    return _pdf_response(get_object_or_404(Invoice, public_token=token))


@require_POST
@perm("add_invoice")
def invoice_credit(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk, kind=Invoice.Kind.INVOICE)
    if invoice.credit_notes.exists():
        messages.info(request, "Cette facture a déjà un avoir.")
        return redirect("invoicing:detail", pk=invoice.pk)
    try:
        credit = api.credit_invoice(f"credit:{invoice.pk}", invoice, reason=request.POST.get("reason", "").strip()[:200])
    except api.InvoicingError as exc:
        messages.error(request, str(exc))
        return redirect("invoicing:detail", pk=invoice.pk)
    messages.success(request, f"Avoir {credit.number} émis.")
    return redirect("invoicing:detail", pk=credit.pk)


class SettingsForm(forms.ModelForm):
    class Meta:
        model = InvoicingSettings
        fields = "__all__"
        widgets = {"late_penalties": forms.Textarea(attrs={"rows": 3}), "footer": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from . import platforms
        self.fields["platform"].widget = forms.Select(choices=platforms.choices())
        for field in self.fields.values():
            widget = field.widget
            css = "form-check-input" if isinstance(widget, forms.CheckboxInput) else (
                "form-select" if isinstance(widget, forms.Select) else "form-control")
            widget.attrs.setdefault("class", css)


@perm("change_invoicingsettings")
def invoicing_settings(request):
    form = SettingsForm(request.POST or None, instance=InvoicingSettings.get())
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Paramètres de facturation enregistrés.")
        return redirect("invoicing:settings")
    return render(request, "invoicing/settings.html", {"form": form, "seller": conf.seller()})
