# Opérations en devise : montant d'origine et devise des lignes (FEC Montantdevise / Idevise).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0013_degressive_impairment'),
    ]

    operations = [
        migrations.AddField(
            model_name='ledgerentry',
            name='currency',
            field=models.CharField(blank=True, help_text='Code ISO 4217 (USD, GBP, CHF…).', max_length=3, verbose_name='Devise'),
        ),
        migrations.AddField(
            model_name='ledgerentry',
            name='currency_amount',
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name='Montant en devise'),
        ),
    ]
