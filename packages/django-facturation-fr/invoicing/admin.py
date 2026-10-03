from django.contrib import admin

from .models import Invoice, InvoiceLine, InvoicingSettings


class LineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in InvoiceLine._meta.fields]


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ("number", "kind", "issue_date", "buyer_name", "total_ttc", "paid_at")
    list_filter = ("kind",)
    search_fields = ("number", "buyer_name", "buyer_email")
    inlines = [LineInline]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in Invoice._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(InvoicingSettings)
