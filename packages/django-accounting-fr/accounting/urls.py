from django.urls import path

from . import views

app_name = "accounting"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),

    # Écritures
    path("ecritures/", views.entry_list, name="entry_list"),
    path("ecritures/nouvelle/", views.entry_create, name="entry_create"),
    path("ecritures/<int:pk>/", views.entry_detail, name="entry_detail"),
    path("ecritures/<int:pk>/modifier/", views.entry_edit, name="entry_edit"),
    path("ecritures/<int:pk>/valider/", views.entry_validate, name="entry_validate"),
    path("ecritures/<int:pk>/contre-passer/", views.entry_reverse, name="entry_reverse"),
    path("ecritures/<int:pk>/supprimer/", views.entry_delete, name="entry_delete"),

    # Journaux
    path("journaux/", views.journals, name="journals"),
    path("journaux/nouveau/", views.journal_edit, name="journal_create"),
    path("journaux/livre-journal/", views.journal_detail, name="journal_all"),
    path("journaux/<str:code>/", views.journal_detail, name="journal_detail"),
    path("journaux/<str:code>/modifier/", views.journal_edit, name="journal_edit"),

    # Lettrage
    path("lettrage/", views.reconciliation_view, name="reconciliation"),
    path("lettrage/balance-agee/", views.aged_balance, name="aged_balance"),
    path("lettrage/<str:code>/", views.reconciliation_account, name="reconciliation_account"),

    # Rapprochement bancaire
    path("banque/", views.bank_index, name="bank_index"),
    path("banque/releves/<int:pk>/supprimer/", views.bank_statement_delete, name="bank_statement_delete"),
    path("banque/<str:code>/", views.bank_account, name="bank_account"),
    path("banque/<str:code>/etat/", views.bank_state, name="bank_state"),

    # États
    path("balance/", views.trial_balance, name="trial_balance"),
    path("grand-livre/", views.general_ledger, name="ledger_index"),
    path("grand-livre/<str:code>/", views.general_ledger, name="general_ledger"),
    path("resultat/", views.profit_loss, name="profit_loss"),
    path("bilan/", views.balance_sheet, name="balance_sheet"),
    path("comptes-annuels/", views.annual_accounts, name="annual_accounts"),
    path("analytique/", views.analytic_report, name="analytic"),
    path("analytique/sections/", views.analytic_sections, name="analytic_sections"),
    path("analytique/sections/<int:pk>/", views.analytic_sections, name="analytic_section_edit"),
    path("immobilisations/", views.fixed_assets, name="fixed_assets"),
    path("immobilisations/nouvelle/", views.fixed_asset_edit, name="fixed_asset_create"),
    path("immobilisations/<int:pk>/", views.fixed_asset_detail, name="fixed_asset_detail"),
    path("immobilisations/<int:pk>/modifier/", views.fixed_asset_edit, name="fixed_asset_edit"),
    path("tva/", views.vat_report, name="vat_report"),

    # Exports et exercices
    path("exports/", views.export_center, name="exports"),
    path("exercices/", views.periods, name="periods"),
    path("integrite/", views.integrity, name="integrity"),
    path("exercices/<int:pk>/cloturer/", views.period_close, name="period_close"),
    path("exercices/<int:pk>/a-nouveaux/", views.period_opening, name="period_opening"),
]
