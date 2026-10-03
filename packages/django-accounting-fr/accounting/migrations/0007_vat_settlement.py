# Liquidation de la TVA : type d'écriture et comptes de TVA à décaisser / crédit de TVA à reporter.

import django.db.models.deletion
from django.db import migrations, models


def fill_accounts(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    AccountingSettings = apps.get_model("accounting", "AccountingSettings")
    settings_obj = AccountingSettings.objects.filter(pk=1).first()
    if settings_obj is None:
        return
    for field, code, name, kind in [("vat_payable_account", "445510", "TVA à décaisser", "liability"),
                                    ("vat_credit_account", "445671", "Crédit de TVA à reporter", "asset")]:
        if getattr(settings_obj, f"{field}_id") is None:
            account, _ = Account.objects.get_or_create(code=code, defaults={"name": name, "account_type": kind})
            setattr(settings_obj, field, account)
    settings_obj.save(update_fields=["vat_payable_account", "vat_credit_account"])


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0006_treasury_journals'),
    ]

    operations = [
        migrations.AddField(
            model_name='accountingsettings',
            name='vat_credit_account',
            field=models.ForeignKey(blank=True, help_text="Crédit de TVA reporté d'une déclaration sur la suivante (44567).", null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name='Crédit de TVA à reporter'),
        ),
        migrations.AddField(
            model_name='accountingsettings',
            name='vat_payable_account',
            field=models.ForeignKey(blank=True, help_text='Solde de la liquidation de TVA à payer (44551).', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name='TVA à décaisser'),
        ),
        migrations.AlterField(
            model_name='transaction',
            name='type',
            field=models.CharField(choices=[('sale', 'Vente'), ('refund', 'Avoir / remboursement'), ('payment', 'Encaissement'), ('payout', 'Décaissement'), ('fee', 'Frais'), ('expense', 'Dépense'), ('transfer', 'Virement'), ('reversal', 'Contre-passation'), ('opening', 'À-nouveaux'), ('closing', 'Clôture'), ('vat', 'Liquidation de TVA'), ('other', 'Autre')], max_length=20, verbose_name='Type'),
        ),
        migrations.RunPython(fill_accounts, migrations.RunPython.noop),
    ]
