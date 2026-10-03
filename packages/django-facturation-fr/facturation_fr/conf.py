"""
Réglages d'intégration, à définir dans le projet hôte (tous facultatifs) :

    INVOICING = {
        # Gabarit des pages de la facturation (doit définir un bloc « content »).
        "BASE_TEMPLATE": "monprojet/base.html",
        # Identité du vendeur : f() -> {"name", "address", "postal_code", "city", "siren", "siret", "vat_number", ...}.
        # Les paramètres de facturation saisis priment sur ces valeurs.
        "SELLER": "monprojet.invoicing.seller",
        # Logo et couleur du PDF : f() -> {"logo_path": chemin ou None, "color": "#RRGGBB"}.
        "BRANDING": "monprojet.invoicing.branding",
        # Lien vers la pièce source : f(document_type, document_id) -> URL ou None.
        "DOCUMENT_URL": "monprojet.invoicing.document_url",
        # Appelés après chaque émission : f(invoice) (ex. comptabilisation, envoi par email).
        "ON_ISSUED": ["monprojet.invoicing.on_issued"],
        # Appelés quand une facture fournisseur change de statut : f(incoming, code) (ex. écriture d'achat).
        "ON_RECEIVED_STATUS": ["monprojet.invoicing.on_received_status"],
        # Plateformes agréées supplémentaires (classes héritant de invoicing.platforms.Platform) et leurs options.
        "PLATFORMS": ["monprojet.plateformes.MaPlateforme"],
        "PLATFORM_OPTIONS": {"ma-plateforme": {"api_key": "...", "url": "https://..."}},
    }
"""
from django.conf import settings
from django.utils.module_loading import import_string

DEFAULTS = {"BASE_TEMPLATE": "invoicing/standalone_base.html", "SELLER": None, "BRANDING": None,
            "DOCUMENT_URL": None, "ON_ISSUED": [], "ON_RECEIVED_STATUS": [], "PLATFORMS": [], "PLATFORM_OPTIONS": {}}

SELLER_FIELDS = ["name", "legal_form", "capital", "address", "postal_code", "city", "country", "siren", "siret",
                 "vat_number", "rcs", "email", "phone", "iban"]


def get(name):
    return getattr(settings, "INVOICING", {}).get(name, DEFAULTS[name])


def _call(name, *args, default=None):
    path = get(name)
    return import_string(path)(*args) if path else default


def base_template():
    return get("BASE_TEMPLATE")


def seller() -> dict:
    """Vendeur : paramètres de facturation, complétés par le projet ; SIREN déduit du SIRET au besoin."""
    from .models import InvoicingSettings

    conf = InvoicingSettings.get()
    provided = _call("SELLER", default=None) or {}

    def siren_of(source_siren, source_siret):
        digits = "".join(c for c in (source_siret or "") if c.isdigit())
        return source_siren or (digits[:9] if len(digits) == 14 else "")

    data = {key: getattr(conf, f"seller_{key}", "") or provided.get(key, "") for key in SELLER_FIELDS}
    data["country"] = data["country"] or "FR"
    # Le SIREN suit le SIRET de la même source : un SIRET saisi dans les paramètres l'emporte sur le projet
    data["siren"] = (siren_of(conf.seller_siren, conf.seller_siret)
                     or siren_of(provided.get("siren", ""), provided.get("siret", "")))
    return data


def branding() -> dict:
    return {"logo_path": None, "color": "#1f2937", **(_call("BRANDING", default=None) or {})}


def document_url(document_type, document_id):
    return _call("DOCUMENT_URL", document_type, document_id) if document_type else None


def on_issued(invoice):
    for path in get("ON_ISSUED"):
        import_string(path)(invoice)


def on_received_status(incoming, code):
    for path in get("ON_RECEIVED_STATUS"):
        import_string(path)(incoming, code)
