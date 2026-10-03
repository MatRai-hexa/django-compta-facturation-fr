# E-reporting au format officiel (flux 10) : transactions et encaissements, régime de TVA,
# matricule de la plateforme agréée, nature des lignes de facture (biens ou services).

from django.db import migrations, models

# Ancienne période choisie -> régime de TVA aux mêmes périodes de transactions
REGIMES = {"decade": "real_monthly", "month": "real_quarterly", "bimonth": "franchise"}


def forward(apps, schema_editor):
    InvoicingSettings = apps.get_model("invoicing", "InvoicingSettings")
    InvoiceLine = apps.get_model("invoicing", "InvoiceLine")
    for settings_obj in InvoicingSettings.objects.all():
        settings_obj.vat_regime = REGIMES.get(settings_obj.ereporting_period, "real_monthly")
        settings_obj.save(update_fields=["vat_regime"])
        if settings_obj.operation_category == "services":  # lignes déjà émises : nature déclarée de l'activité
            InvoiceLine.objects.update(nature="services")


class Migration(migrations.Migration):

    # Données modifiées puis tables modifiées : sous PostgreSQL, une même transaction échoue (« pending trigger
    # events », contraintes de clé étrangère différées). Chaque opération est donc validée séparément.
    atomic = False

    dependencies = [
        ('invoicing', '0002_einvoicing'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='ereport',
            options={'ordering': ['-period_start', 'kind'], 'verbose_name': 'E-reporting', 'verbose_name_plural': 'E-reporting'},
        ),
        migrations.RemoveConstraint(
            model_name='ereport',
            name='unique_ereport_period',
        ),
        migrations.AddField(
            model_name='ereport',
            name='fingerprint',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='ereport',
            name='history',
            field=models.JSONField(blank=True, default=list, verbose_name='Transmissions précédentes'),
        ),
        migrations.AddField(
            model_name='ereport',
            name='kind',
            field=models.CharField(choices=[('transactions', 'Transactions'), ('payments', 'Encaissements')], default='transactions', max_length=20, verbose_name='Données'),
        ),
        migrations.AddField(
            model_name='ereport',
            name='total_paid',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14, verbose_name='Total encaissé'),
        ),
        migrations.AddField(
            model_name='ereport',
            name='transmission_id',
            field=models.CharField(blank=True, max_length=50, verbose_name='Identifiant de transmission'),
        ),
        migrations.AddField(
            model_name='ereport',
            name='type_code',
            field=models.CharField(choices=[('IN', 'Initiale'), ('RE', 'Rectificative')], default='IN', max_length=2, verbose_name='Type de transmission'),
        ),
        migrations.AddField(
            model_name='ereport',
            name='version',
            field=models.PositiveIntegerField(default=1, verbose_name='Version'),
        ),
        migrations.AddField(
            model_name='ereport',
            name='xml',
            field=models.TextField(blank=True, verbose_name='Flux 10 (XML)'),
        ),
        migrations.AddField(
            model_name='invoiceline',
            name='nature',
            field=models.CharField(choices=[('goods', 'Livraison de biens'), ('services', 'Prestation de services')], default='goods', help_text="Les frais de port suivent la livraison des biens qu'ils accompagnent.", max_length=10, verbose_name='Nature'),
        ),
        migrations.AddField(
            model_name='invoicingsettings',
            name='platform_company',
            field=models.CharField(blank=True, max_length=150, verbose_name='Raison sociale de la plateforme agréée'),
        ),
        migrations.AddField(
            model_name='invoicingsettings',
            name='platform_registration',
            field=models.CharField(blank=True, help_text="4 caractères, attribués par l'administration à votre plateforme : émetteur du e-reporting. Inutile si le connecteur de la plateforme le fournit.", max_length=4, verbose_name='Matricule de la plateforme agréée'),
        ),
        migrations.AddField(
            model_name='invoicingsettings',
            name='vat_regime',
            field=models.CharField(choices=[('real_monthly', 'Réel normal mensuel'), ('real_quarterly', 'Réel normal trimestriel'), ('simplified', 'Régime simplifié'), ('franchise', 'Franchise en base')], default='real_monthly', help_text='Fixe les périodes du e-reporting : transactions par décade et encaissements par mois au réel normal mensuel ; par mois au réel trimestriel et au simplifié ; par bimestre civil en franchise.', max_length=20, verbose_name='Régime de TVA'),
        ),
        migrations.RunPython(forward, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='invoicingsettings',
            name='ereporting_period',
        ),
        migrations.AddConstraint(
            model_name='ereport',
            constraint=models.UniqueConstraint(fields=('kind', 'period_start', 'period_end'), name='unique_ereport_period'),
        ),
    ]
