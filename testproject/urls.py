from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("comptabilite/", include("accounting.urls")),
    path("facturation/", include("invoicing.urls")),
]
