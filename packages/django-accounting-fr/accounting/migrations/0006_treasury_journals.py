# Un journal de trésorerie par compte de trésorerie : le compte de contrepartie de toutes ses écritures,
# rapproché avec les relevés. Les moyens de paiement pointent désormais vers un journal (le compte s'en déduit).

import django.db.models.deletion
from django.db import migrations, models

# Journaux créés pour les moyens de paiement d'origine, si leur compte n'a pas encore de journal
DEFAULT_CODES = {"stripe": ("ST", "Stripe"), "paypal": ("PP", "PayPal"), "cod": ("CA", "Caisse")}


def assign_treasury(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    Journal = apps.get_model("accounting", "Journal")
    PaymentAccount = apps.get_model("accounting", "PaymentAccount")
    AccountingSettings = apps.get_model("accounting", "AccountingSettings")
    settings_obj = AccountingSettings.objects.filter(pk=1).first()

    def journal_for(account):
        return Journal.objects.filter(account=account).first()

    def free_code(code):
        candidate, n = code, 2
        while Journal.objects.filter(code=candidate).exists():
            candidate, n = f"{code}{n}", n + 1
        return candidate

    # 1. La banque principale : journal BQ (ou le premier journal de banque sans compte)
    if settings_obj is not None and journal_for(settings_obj.bank_account) is None:
        bq = (Journal.objects.filter(code="BQ", kind="bank", account__isnull=True).first()
              or Journal.objects.filter(kind="bank", account__isnull=True).order_by("code").first())
        if bq is None:
            bq = Journal.objects.create(code=free_code("BQ"), label="Banque", kind="bank")
        bq.account = settings_obj.bank_account
        bq.save(update_fields=["account"])

    # 2. Journaux déjà choisis pour un moyen de paiement, s'ils n'ont pas encore de compte
    for mapping in PaymentAccount.objects.filter(journal__isnull=False, journal__account__isnull=True):
        if journal_for(mapping.account) is None:
            Journal.objects.filter(pk=mapping.journal_id).update(account=mapping.account)

    # 3. Chaque moyen de paiement vers le journal de son compte (créé au besoin)
    for mapping in PaymentAccount.objects.all():
        journal = journal_for(mapping.account)
        if journal is None:
            code, label = DEFAULT_CODES.get(mapping.method, (mapping.method.upper()[:8] or "TR", mapping.label))
            journal = Journal.objects.create(code=free_code(code), label=label[:100], kind="bank", account=mapping.account)
        if journal.kind != "bank":
            Journal.objects.filter(pk=journal.pk).update(kind="bank")
        mapping.journal = journal
        mapping.save(update_fields=["journal"])

    # 4. Compte de liaison des virements entre journaux de trésorerie
    if settings_obj is not None and settings_obj.transfer_account_id is None:
        account, _ = Account.objects.get_or_create(code="580000", defaults={"name": "Virements internes",
                                                                            "account_type": "asset"})
        settings_obj.transfer_account = account
        settings_obj.save(update_fields=["transfer_account"])


class Migration(migrations.Migration):

    # Données modifiées puis tables modifiées : sous PostgreSQL, une même transaction échoue (« pending trigger
    # events », contraintes de clé étrangère différées). Chaque opération est donc validée séparément.
    atomic = False

    dependencies = [
        ('accounting', '0005_bank_reconciliation'),
    ]

    operations = [
        migrations.AddField(
            model_name='journal',
            name='account',
            field=models.ForeignKey(blank=True, help_text="Journal de banque : compte de contrepartie de toutes ses écritures (512…, caisse, Stripe…). Ce compte ne se mouvemente que dans ce journal ; c'est lui qu'on rapproche avec les relevés.", null=True, on_delete=django.db.models.deletion.PROTECT, related_name='treasury_journals', to='accounting.account', verbose_name='Compte de trésorerie'),
        ),
        migrations.AddField(
            model_name='accountingsettings',
            name='transfer_account',
            field=models.ForeignKey(blank=True, help_text='Compte de liaison (580) des virements entre deux journaux de trésorerie.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='accounting.account', verbose_name='Virements internes'),
        ),
        migrations.RunPython(assign_treasury, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='journal',
            constraint=models.UniqueConstraint(condition=models.Q(('account__isnull', False)), fields=('account',), name='unique_treasury_account'),
        ),
        migrations.RemoveField(
            model_name='paymentaccount',
            name='account',
        ),
        migrations.AlterField(
            model_name='paymentaccount',
            name='journal',
            field=models.ForeignKey(help_text='Encaissements, frais et virements de ce moyen ; son compte de trésorerie est celui du journal.', limit_choices_to={'account__isnull': False, 'kind': 'bank'}, on_delete=django.db.models.deletion.PROTECT, to='accounting.journal', verbose_name='Journal de trésorerie'),
        ),
        migrations.AlterModelOptions(
            name='paymentaccount',
            options={'ordering': ('method',), 'verbose_name': "Journal d'un moyen de paiement", 'verbose_name_plural': 'Journaux des moyens de paiement'},
        ),
    ]
