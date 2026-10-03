# TVA des prestations de services exigible à l'encaissement : compte d'attente, option sur les débits, base HT des lignes de TVA.

import django.db.models.deletion
from django.db import migrations, models


def pending_account(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    AccountingSettings = apps.get_model("accounting", "AccountingSettings")
    settings_obj = AccountingSettings.objects.filter(pk=1).first()
    if settings_obj is None or settings_obj.pending_vat_account_id:
        return
    settings_obj.pending_vat_account, _ = Account.objects.get_or_create(
        code="445800", defaults={"name": "TVA collectée en attente d'encaissement", "account_type": "liability"})
    settings_obj.save(update_fields=["pending_vat_account"])


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0009_fixed_assets'),
    ]

    operations = [
        migrations.AddField(
            model_name='accountingsettings',
            name='pending_vat_account',
            field=models.ForeignKey(blank=True, help_text='TVA facturée sur des prestations non encore encaissées (4458).', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name="TVA en attente d'encaissement"),
        ),
        migrations.AddField(
            model_name='accountingsettings',
            name='vat_on_debits',
            field=models.BooleanField(default=False, help_text="Option pour le paiement de la TVA d'après les débits : la TVA des prestations de services est exigible à la facturation. Sinon elle l'est à l'encaissement (en attente jusque-là).", verbose_name='TVA sur les débits (prestations de services)'),
        ),
        migrations.AddField(
            model_name='ledgerentry',
            name='vat_base',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Sur une ligne de TVA : base HT imposable correspondante (déclaration de TVA).', max_digits=12, null=True, verbose_name='Base HT de cette TVA'),
        ),
        migrations.AlterField(
            model_name='transaction',
            name='type',
            field=models.CharField(choices=[('sale', 'Vente'), ('refund', 'Avoir / remboursement'), ('payment', 'Encaissement'), ('payout', 'Décaissement'), ('fee', 'Frais'), ('expense', 'Dépense'), ('transfer', 'Virement'), ('reversal', 'Contre-passation'), ('opening', 'À-nouveaux'), ('closing', 'Clôture'), ('vat', 'Liquidation de TVA'), ('vat_release', "TVA exigible à l'encaissement"), ('depreciation', 'Dotations aux amortissements'), ('disposal', "Sortie d'immobilisation"), ('other', 'Autre')], max_length=20, verbose_name='Type'),
        ),
        migrations.RunPython(pending_account, migrations.RunPython.noop),
    ]
