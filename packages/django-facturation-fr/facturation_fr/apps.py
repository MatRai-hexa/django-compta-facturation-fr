from django.apps import AppConfig


class InvoicingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "facturation_fr"
    label = "invoicing"  # tables, migrations, droits et espace d'URL inchangés
    verbose_name = "Facturation"
