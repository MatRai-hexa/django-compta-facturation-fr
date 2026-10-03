from django.urls import path

from . import views, views_einvoicing as e

app_name = "invoicing"

urlpatterns = [
    path("", views.invoice_list, name="list"),
    path("parametres/", views.invoicing_settings, name="settings"),
    path("<int:pk>/", views.invoice_detail, name="detail"),
    path("<int:pk>/pdf/", views.invoice_pdf, name="pdf"),
    path("<int:pk>/avoir/", views.invoice_credit, name="credit"),
    path("telecharger/<str:token>/", views.invoice_public_pdf, name="public_pdf"),
    # Facturation électronique
    path("electronique/", e.dashboard, name="einvoicing"),
    path("electronique/synchroniser/", e.sync_now, name="sync"),
    path("electronique/transmissions/<int:pk>/statut/", e.transmission_deposited, name="transmission_status"),
    path("electronique/e-reporting/<int:pk>/", e.ereport_download, name="ereport_download"),
    path("electronique/e-reporting/<int:pk>/transmettre/", e.ereport_transmit, name="ereport_transmit"),
    path("recues/", e.incoming_list, name="incoming_list"),
    path("recues/importer/", e.incoming_upload, name="incoming_upload"),
    path("recues/<int:pk>/", e.incoming_detail, name="incoming_detail"),
    path("recues/<int:pk>/statut/", e.incoming_status, name="incoming_status"),
    path("recues/<int:pk>/fichier/", e.incoming_file, name="incoming_file"),
]
