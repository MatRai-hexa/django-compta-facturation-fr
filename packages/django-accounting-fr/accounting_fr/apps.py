from django.apps import AppConfig


class AccountingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounting_fr"
    label = "accounting"  # tables, migrations, droits et espace d'URL inchangés
    verbose_name = "Comptabilité"
