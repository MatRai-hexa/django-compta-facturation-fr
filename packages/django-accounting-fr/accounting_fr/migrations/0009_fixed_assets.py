# Registre des immobilisations et dotations aux amortissements ; comptes utiles ajoutés au plan existant.

import django.db.models.deletion
from django.db import migrations, models

ACCOUNTS = [
    ("205000", "Concessions, brevets, licences, logiciels", "asset"), ("215400", "Matériel industriel", "asset"),
    ("218200", "Matériel de transport", "asset"), ("218400", "Mobilier", "asset"),
    ("280500", "Amortissements des concessions, brevets, licences, logiciels", "asset"),
    ("281540", "Amortissements du matériel industriel", "asset"),
    ("281820", "Amortissements du matériel de transport", "asset"),
    ("281830", "Amortissements du matériel de bureau et informatique", "asset"),
    ("281840", "Amortissements du mobilier", "asset"), ("404000", "Fournisseurs d'immobilisations", "liability"),
    ("462000", "Créances sur cessions d'immobilisations", "asset"),
    ("675000", "Valeurs comptables des éléments d'actif cédés", "expense"),
    ("681110", "Dotations aux amortissements des immobilisations incorporelles", "expense"),
    ("681120", "Dotations aux amortissements des immobilisations corporelles", "expense"),
    ("775000", "Produits des cessions d'éléments d'actif", "revenue"),
]


def add_accounts(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    if not Account.objects.exists():
        return  # plan installé plus tard, complet
    for code, name, kind in ACCOUNTS:
        Account.objects.get_or_create(code=code, defaults={"name": name, "account_type": kind})


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0008_seal_chain'),
    ]

    operations = [
        migrations.AlterField(
            model_name='transaction',
            name='type',
            field=models.CharField(choices=[('sale', 'Vente'), ('refund', 'Avoir / remboursement'), ('payment', 'Encaissement'), ('payout', 'Décaissement'), ('fee', 'Frais'), ('expense', 'Dépense'), ('transfer', 'Virement'), ('reversal', 'Contre-passation'), ('opening', 'À-nouveaux'), ('closing', 'Clôture'), ('vat', 'Liquidation de TVA'), ('depreciation', 'Dotations aux amortissements'), ('disposal', "Sortie d'immobilisation"), ('other', 'Autre')], max_length=20, verbose_name='Type'),
        ),
        migrations.CreateModel(
            name='FixedAsset',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(max_length=200, verbose_name='Désignation')),
                ('reference', models.CharField(blank=True, max_length=40, verbose_name="N° d'inventaire")),
                ('acquisition_date', models.DateField(verbose_name="Date d'acquisition")),
                ('service_date', models.DateField(help_text="Point de départ de l'amortissement.", verbose_name='Mise en service')),
                ('cost', models.DecimalField(decimal_places=2, max_digits=14, verbose_name="Valeur d'origine HT")),
                ('method', models.CharField(choices=[('linear', 'Linéaire'), ('none', 'Non amortissable (terrain, fonds commercial…)')], default='linear', max_length=10, verbose_name="Mode d'amortissement")),
                ('duration_months', models.PositiveIntegerField(blank=True, help_text="Durée d'utilisation : 36 pour 3 ans.", null=True, verbose_name='Durée (mois)')),
                ('disposal_date', models.DateField(blank=True, null=True, verbose_name='Date de sortie')),
                ('disposal_price', models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name='Prix de cession HT')),
                ('note', models.TextField(blank=True, verbose_name='Note')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('account', models.ForeignKey(help_text='Classe 2 (ex. 218300 matériel informatique).', on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name="Compte d'immobilisation")),
                ('acquisition_entry', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='accounting.ledgerentry', verbose_name="Ligne d'acquisition")),
                ('depreciation_account', models.ForeignKey(blank=True, help_text="Vide : déduit du compte d'immobilisation (218300 → 281830).", null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name="Compte d'amortissement")),
                ('expense_account', models.ForeignKey(blank=True, help_text='Vide : 681110 (incorporelles) ou 681120 (corporelles).', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name='Compte de dotation')),
            ],
            options={
                'verbose_name': 'Immobilisation',
                'verbose_name_plural': 'Immobilisations',
                'ordering': ('-acquisition_date', '-pk'),
            },
        ),
        migrations.CreateModel(
            name='DepreciationRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('transaction', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.transaction')),
                ('asset', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='depreciations', to='accounting.fixedasset')),
            ],
            options={
                'ordering': ('date', 'pk'),
                'constraints': [models.UniqueConstraint(fields=('asset', 'transaction'), name='unique_depreciation_line')],
            },
        ),
        migrations.RunPython(add_accounts, migrations.RunPython.noop),
    ]
