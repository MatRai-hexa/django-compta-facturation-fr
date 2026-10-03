"""
Réglages d'intégration, à définir dans le projet hôte :

    ACCOUNTING = {
        # Gabarit dont héritent les pages comptables (doit définir un bloc « content »).
        "BASE_TEMPLATE": "accounting/standalone_base.html",
        # Lien vers une pièce du projet : f(document_type, document_id) -> URL ou None.
        "DOCUMENT_URL": "monprojet.accounting.document_url",
        # Identité de la société pour le FEC : f() -> {"name": ..., "siren": ...}.
        "COMPANY": "monprojet.accounting.company",
        # Encarts du tableau de bord : f(request) -> [{"label", "value", "url", "alert"}].
        "DASHBOARD_PANELS": ["monprojet.accounting.dashboard_panels"],
        # Dossier (hors de la base, idéalement sur un autre support) où consigner les sceaux de la chaîne.
        "SEAL_ARCHIVE_DIR": "/srv/archives/sceaux",
        # Comptes de tiers lettrables (préfixes des numéros de compte).
        "RECONCILABLE_PREFIXES": ["40", "41"],
    }

Tout est facultatif : sans réglage, la comptabilité fonctionne seule. Les comptes rapprochés avec
les relevés bancaires sont ceux des journaux de banque (Comptabilité › Journaux).
"""
from django.conf import settings
from django.utils.module_loading import import_string

DEFAULTS = {
    "BASE_TEMPLATE": "accounting/standalone_base.html",
    "DOCUMENT_URL": None,
    "COMPANY": None,
    "DASHBOARD_PANELS": [],
    "RECONCILABLE_PREFIXES": ["40", "41"],
    "SEAL_ARCHIVE_DIR": None,
}


def get(name):
    return getattr(settings, "ACCOUNTING", {}).get(name, DEFAULTS[name])


def _call(name, *args, default=None):
    path = get(name)
    if not path:
        return default
    return import_string(path)(*args)


def base_template():
    return get("BASE_TEMPLATE")


def document_url(document_type, document_id):
    if not document_type:
        return None
    return _call("DOCUMENT_URL", document_type, document_id)


def company():
    """{"name": ..., "siren": ...} : réglage du projet, sinon paramètres comptables."""
    from .models import AccountingSettings

    conf = AccountingSettings.get()
    info = {"name": conf.company_name, "siren": conf.siren}
    provided = _call("COMPANY", default=None) or {}
    return {key: info.get(key) or provided.get(key, "") for key in ("name", "siren")}


def dashboard_panels(request):
    panels = []
    for path in get("DASHBOARD_PANELS"):
        panels.extend(import_string(path)(request) or [])
    return panels
