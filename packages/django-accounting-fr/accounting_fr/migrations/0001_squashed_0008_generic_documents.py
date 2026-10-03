# Migration initiale autonome (regroupe 0001 à 0008, sans dépendance au projet hôte).

import django.db.models.deletion
import django.utils.timezone
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models

# Données figées à la date de la migration (ne pas importer accounting.chart, qui évolue)
# (numéro, intitulé, nature)
ACCOUNTS = [
    ("101000", "Capital", "equity"),
    ("108000", "Compte de l'exploitant", "equity"),
    ("110000", "Report à nouveau (solde créditeur)", "equity"),
    ("119000", "Report à nouveau (solde débiteur)", "equity"),
    ("120000", "Résultat de l'exercice (bénéfice)", "equity"),
    ("129000", "Résultat de l'exercice (perte)", "equity"),
    ("218300", "Matériel de bureau et informatique", "asset"),
    ("370000", "Stocks de marchandises", "asset"),
    ("401000", "Fournisseurs", "liability"),
    ("411000", "Clients", "asset"),
    ("419100", "Clients - avances et acomptes reçus", "liability"),
    ("445510", "TVA à décaisser", "liability"),
    ("445660", "TVA déductible sur autres biens et services", "asset"),
    ("445671", "Crédit de TVA à reporter", "asset"),
    ("445711", "TVA collectée 20 %", "liability"),
    ("445712", "TVA collectée 10 %", "liability"),
    ("445713", "TVA collectée 5,5 %", "liability"),
    ("445714", "TVA collectée 2,1 %", "liability"),
    ("467100", "Stripe - fonds à recevoir", "asset"),
    ("467200", "PayPal - fonds à recevoir", "asset"),
    ("512000", "Banque", "asset"),
    ("530000", "Caisse", "asset"),
    ("580000", "Virements internes", "asset"),
    ("607000", "Achats de marchandises", "expense"),
    ("603700", "Variation des stocks de marchandises", "expense"),
    ("606000", "Achats non stockés de matières et fournitures", "expense"),
    ("613200", "Locations immobilières", "expense"),
    ("622600", "Honoraires", "expense"),
    ("623000", "Publicité, publications", "expense"),
    ("624100", "Transports sur achats", "expense"),
    ("624200", "Transports sur ventes", "expense"),
    ("626000", "Frais postaux et de télécommunications", "expense"),
    ("627000", "Services bancaires et frais de paiement", "expense"),
    ("651000", "Redevances pour logiciels", "expense"),
    ("707000", "Ventes de marchandises", "revenue"),
    ("708500", "Ports et frais accessoires facturés", "revenue"),
    ("709700", "Rabais, remises et ristournes accordés", "revenue"),
]

JOURNALS = [
    ("VT", "Ventes", "sales"),
    ("BQ", "Banque et encaissements", "bank"),
    ("AC", "Achats", "purchases"),
    ("OD", "Opérations diverses", "misc"),
    ("AN", "À-nouveaux", "opening"),
]

# (taux, libellé, compte de TVA collectée)
VAT_RATES = [
    (Decimal("0.2000"), "TVA 20 %", "445711"),
    (Decimal("0.1000"), "TVA 10 %", "445712"),
    (Decimal("0.0550"), "TVA 5,5 %", "445713"),
    (Decimal("0.0210"), "TVA 2,1 %", "445714"),
    (Decimal("0.0000"), "Exonéré / non soumis", None),
]

PAYMENT_ACCOUNTS = [
    ("cod", "Paiement à la livraison", "530000"),
    ("stripe", "Stripe", "467100"),
    ("paypal", "PayPal", "467200"),
]

SETTINGS_ACCOUNTS = {
    "customer_account": "411000",
    "sales_account": "707000",
    "shipping_account": "708500",
    "bank_account": "512000",
    "fees_account": "627000",
    "profit_account": "120000",
    "loss_account": "129000",
}

def install_chart(apps, schema_editor):
    """Plan comptable, journaux, taux de TVA et paramètres par défaut (ne modifie pas l'existant)."""
    Account = apps.get_model("accounting", "Account")
    Journal = apps.get_model("accounting", "Journal")
    VATRate = apps.get_model("accounting", "VATRate")
    PaymentAccount = apps.get_model("accounting", "PaymentAccount")
    AccountingSettings = apps.get_model("accounting", "AccountingSettings")
    accounts = {}
    for code, name, kind in ACCOUNTS:
        accounts[code], _ = Account.objects.get_or_create(code=code, defaults={"name": name, "account_type": kind})
    for code, label, kind in JOURNALS:
        Journal.objects.get_or_create(code=code, defaults={"label": label, "kind": kind})
    for rate, label, account_code in VAT_RATES:
        VATRate.objects.get_or_create(rate=rate, defaults={"label": label, "collected_account": accounts.get(account_code)})
    for method, label, code in PAYMENT_ACCOUNTS:
        PaymentAccount.objects.get_or_create(method=method, defaults={"label": label, "account": accounts[code]})
    if not AccountingSettings.objects.filter(pk=1).exists():
        AccountingSettings.objects.create(pk=1, **{field: accounts[code] for field, code in SETTINGS_ACCOUNTS.items()})


class Migration(migrations.Migration):

    initial = True

    # Historique d'origine (dépendant de la boutique), conservé pour les bases déjà migrées.
    replaces = [
        ("accounting", "0001_initial"),
        ("accounting", "0002_alter_account_options_alter_fiscalperiod_options_and_more"),
        ("accounting", "0003_exporttemplate_auditlog"),
        ("accounting", "0004_transaction_tags"),
        ("accounting", "0005_journals_vat_lock"),
        ("accounting", "0006_install_chart_and_backfill"),
        ("accounting", "0007_required_journal_period"),
        ("accounting", "0008_generic_documents"),
    ]

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Journal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=10, unique=True, verbose_name='Code')),
                ('label', models.CharField(max_length=100, verbose_name='Libellé')),
                ('kind', models.CharField(choices=[('sales', 'Ventes'), ('bank', 'Banque / trésorerie'), ('purchases', 'Achats'), ('misc', 'Opérations diverses'), ('opening', 'À-nouveaux')], default='misc', max_length=20, verbose_name='Type')),
            ],
            options={
                'verbose_name': 'Journal',
                'verbose_name_plural': 'Journaux',
                'ordering': ('code',),
            },
        ),
        migrations.CreateModel(
            name='Account',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=20, unique=True, verbose_name='Numéro')),
                ('name', models.CharField(max_length=200, verbose_name='Intitulé')),
                ('account_type', models.CharField(blank=True, choices=[('asset', 'Actif'), ('liability', 'Passif'), ('equity', 'Capitaux propres'), ('revenue', 'Produits'), ('expense', 'Charges')], max_length=20, verbose_name='Nature')),
                ('is_active', models.BooleanField(default=True, verbose_name='Actif')),
                ('description', models.TextField(blank=True)),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='children', to='accounting.account')),
            ],
            options={
                'verbose_name': 'Compte',
                'verbose_name_plural': 'Plan comptable',
                'ordering': ('code',),
            },
        ),
        migrations.CreateModel(
            name='AccountingSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('auto_validate', models.BooleanField(default=True, help_text='Les écritures issues des ventes et des paiements sont numérotées et verrouillées dès leur création.', verbose_name='Valider automatiquement les écritures générées')),
                ('company_name', models.CharField(blank=True, help_text="Laisser vide si le projet la fournit (réglage ACCOUNTING['COMPANY']).", max_length=160, verbose_name='Raison sociale')),
                ('siren', models.CharField(blank=True, help_text='Utilisé pour nommer le fichier FEC.', max_length=14, verbose_name='SIREN')),
                ('bank_account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name='Banque')),
                ('customer_account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name='Clients')),
                ('fees_account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name='Frais bancaires et de paiement')),
                ('loss_account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name='Résultat (perte)')),
                ('profit_account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name='Résultat (bénéfice)')),
                ('sales_account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name='Ventes de marchandises')),
                ('shipping_account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name='Ports facturés')),
            ],
            options={
                'verbose_name': 'Paramètres comptables',
                'verbose_name_plural': 'Paramètres comptables',
            },
        ),
        migrations.CreateModel(
            name='FiscalPeriod',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, verbose_name='Nom')),
                ('date_start', models.DateField(verbose_name='Début')),
                ('date_end', models.DateField(verbose_name='Fin')),
                ('is_closed', models.BooleanField(default=False, verbose_name='Clôturé')),
                ('closed_at', models.DateTimeField(blank=True, null=True)),
                ('closed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='closed_periods', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Exercice',
                'verbose_name_plural': 'Exercices',
                'ordering': ('-date_start',),
            },
        ),
        migrations.CreateModel(
            name='PaymentAccount',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('method', models.CharField(max_length=40, unique=True, verbose_name='Code du moyen de paiement')),
                ('label', models.CharField(max_length=100, verbose_name='Libellé')),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='accounting.account', verbose_name='Compte')),
            ],
            options={
                'verbose_name': 'Compte de moyen de paiement',
                'verbose_name_plural': 'Comptes des moyens de paiement',
                'ordering': ('method',),
            },
        ),
        migrations.CreateModel(
            name='Transaction',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('number', models.CharField(blank=True, db_index=True, help_text='Attribué à la validation, séquentiel par journal et par exercice.', max_length=30, verbose_name='Numéro')),
                ('type', models.CharField(choices=[('sale', 'Vente'), ('refund', 'Avoir / remboursement'), ('payment', 'Encaissement'), ('payout', 'Décaissement'), ('fee', 'Frais'), ('expense', 'Dépense'), ('transfer', 'Virement'), ('reversal', 'Contre-passation'), ('opening', 'À-nouveaux'), ('closing', 'Clôture'), ('other', 'Autre')], max_length=20, verbose_name='Type')),
                ('date', models.DateField(db_index=True, default=django.utils.timezone.localdate, verbose_name='Date')),
                ('reference', models.CharField(blank=True, db_index=True, max_length=128, verbose_name='Pièce')),
                ('piece_date', models.DateField(blank=True, null=True, verbose_name='Date de la pièce')),
                ('description', models.CharField(blank=True, max_length=255, verbose_name='Libellé')),
                ('amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12, verbose_name='Montant')),
                ('currency', models.CharField(default='EUR', max_length=10)),
                ('document_type', models.CharField(blank=True, max_length=40, verbose_name='Type de pièce')),
                ('document_id', models.CharField(blank=True, max_length=64, verbose_name='Identifiant de pièce')),
                ('source_key', models.CharField(blank=True, help_text="Événement à l'origine d'une écriture automatique (ex. order:42:sale).", max_length=100, null=True, unique=True, verbose_name='Origine')),
                ('tags', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('is_validated', models.BooleanField(default=False, verbose_name='Validée')),
                ('validated_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('fiscal_period', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='accounting.fiscalperiod', verbose_name='Exercice')),
                ('journal', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='accounting.journal', verbose_name='Journal')),
                ('reversal_of', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='reversals', to='accounting.transaction')),
                ('validated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='validated_transactions', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Écriture',
                'verbose_name_plural': 'Écritures',
                'ordering': ('-date', '-id'),
                'permissions': [('close_period', 'Peut clôturer un exercice'), ('view_reports', 'Peut consulter les rapports comptables'), ('export_data', 'Peut exporter les données comptables'), ('validate_transaction', 'Peut valider une écriture')],
            },
        ),
        migrations.CreateModel(
            name='LedgerEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('debit', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12, verbose_name='Débit')),
                ('credit', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12, verbose_name='Crédit')),
                ('label', models.CharField(blank=True, max_length=200, verbose_name='Libellé')),
                ('vat_rate', models.DecimalField(blank=True, decimal_places=4, help_text='Renseigné sur les lignes de base HT et de TVA (récapitulatif de TVA).', max_digits=5, null=True, verbose_name='Taux de TVA')),
                ('auxiliary_code', models.CharField(blank=True, max_length=40, verbose_name='Compte auxiliaire')),
                ('auxiliary_label', models.CharField(blank=True, max_length=120, verbose_name='Libellé auxiliaire')),
                ('reconciliation_ref', models.CharField(blank=True, db_index=True, max_length=50, verbose_name='Lettrage')),
                ('is_reconciled', models.BooleanField(default=False)),
                ('reconciled_at', models.DateField(blank=True, null=True)),
                ('account', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='accounting.account', verbose_name='Compte')),
                ('transaction', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='entries', to='accounting.transaction')),
            ],
            options={
                'verbose_name': "Ligne d'écriture",
                'verbose_name_plural': "Lignes d'écriture",
                'ordering': ('transaction__date', 'id'),
            },
        ),
        migrations.CreateModel(
            name='VATRate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('rate', models.DecimalField(decimal_places=4, help_text='0.2000 pour 20 %', max_digits=5, unique=True, verbose_name='Taux')),
                ('label', models.CharField(max_length=60, verbose_name='Libellé')),
                ('is_active', models.BooleanField(default=True)),
                ('collected_account', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='accounting.account', verbose_name='Compte de TVA collectée')),
            ],
            options={
                'verbose_name': 'Taux de TVA',
                'verbose_name_plural': 'Taux de TVA',
                'ordering': ('-rate',),
            },
        ),
        migrations.CreateModel(
            name='AuditLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(db_index=True, max_length=100)),
                ('model_name', models.CharField(max_length=100)),
                ('object_id', models.IntegerField(blank=True, null=True)),
                ('details', models.TextField(blank=True)),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True)),
                ('user_agent', models.CharField(blank=True, max_length=500)),
                ('timestamp', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': "Journal d'audit",
                'verbose_name_plural': "Journal d'audit",
                'ordering': ['-timestamp'],
                'indexes': [models.Index(fields=['user', 'timestamp'], name='accounting__user_id_d31988_idx'), models.Index(fields=['model_name', 'object_id'], name='accounting__model_n_d0095a_idx'), models.Index(fields=['action', 'timestamp'], name='accounting__action_ed7447_idx')],
            },
        ),
        migrations.CreateModel(
            name='EntrySequence',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('last_number', models.PositiveIntegerField(default=0)),
                ('fiscal_period', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='accounting.fiscalperiod')),
                ('journal', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='accounting.journal')),
            ],
            options={
                'constraints': [models.UniqueConstraint(fields=('journal', 'fiscal_period'), name='unique_entry_sequence')],
            },
        ),
        migrations.AddIndex(
            model_name='transaction',
            index=models.Index(fields=['document_type', 'document_id'], name='accounting__documen_f1c067_idx'),
        ),
        migrations.AddConstraint(
            model_name='transaction',
            constraint=models.UniqueConstraint(condition=models.Q(('number', ''), _negated=True), fields=('number',), name='unique_entry_number'),
        ),
        migrations.AddConstraint(
            model_name='ledgerentry',
            constraint=models.CheckConstraint(condition=models.Q(('credit__gte', 0), ('debit__gte', 0), models.Q(models.Q(('credit__gt', 0), ('debit', 0)), models.Q(('credit', 0), ('debit__gt', 0)), _connector='OR')), name='ledger_entry_one_side'),
        ),
        migrations.RunPython(install_chart, migrations.RunPython.noop),
    ]
