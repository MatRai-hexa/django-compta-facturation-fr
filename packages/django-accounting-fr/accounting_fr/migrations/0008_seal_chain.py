# Empreinte chaînée des écritures validées et sceau de clôture des exercices ; scellement de l'existant.

from django.db import migrations, models
from django.db.models import Max


def seal_existing(apps, schema_editor):
    from accounting_fr.seal import GENESIS, seal_existing as seal_all

    Transaction = apps.get_model("accounting", "Transaction")
    SealChain = apps.get_model("accounting", "SealChain")
    FiscalPeriod = apps.get_model("accounting", "FiscalPeriod")
    seal_all(Transaction, SealChain)
    for period in FiscalPeriod.objects.filter(is_closed=True, closing_index__isnull=True):
        index = (Transaction.objects.filter(fiscal_period__date_end__lte=period.date_end, seal_index__isnull=False)
                 .aggregate(m=Max("seal_index"))["m"]) or 0
        seal = Transaction.objects.filter(seal_index=index).values_list("seal", flat=True).first() or GENESIS
        FiscalPeriod.objects.filter(pk=period.pk).update(closing_index=index, closing_seal=seal)


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0007_vat_settlement'),
    ]

    operations = [
        migrations.CreateModel(
            name='SealChain',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('last_index', models.PositiveBigIntegerField(default=0)),
                ('last_seal', models.CharField(blank=True, max_length=64)),
            ],
            options={
                'verbose_name': 'Chaîne des empreintes',
                'verbose_name_plural': 'Chaîne des empreintes',
            },
        ),
        migrations.AddField(
            model_name='fiscalperiod',
            name='closing_index',
            field=models.PositiveBigIntegerField(blank=True, null=True, verbose_name='Rang du sceau de clôture'),
        ),
        migrations.AddField(
            model_name='fiscalperiod',
            name='closing_seal',
            field=models.CharField(blank=True, help_text='Empreinte de la chaîne des écritures à la clôture : à conserver hors du logiciel.', max_length=64, verbose_name='Sceau de clôture'),
        ),
        migrations.AddField(
            model_name='transaction',
            name='seal',
            field=models.CharField(blank=True, help_text="SHA-256 du contenu de l'écriture et de l'empreinte précédente (voir accounting.seal).", max_length=64, verbose_name='Empreinte'),
        ),
        migrations.AddField(
            model_name='transaction',
            name='seal_index',
            field=models.PositiveBigIntegerField(blank=True, null=True, unique=True, verbose_name='Rang dans la chaîne'),
        ),
        migrations.RunPython(seal_existing, migrations.RunPython.noop),
    ]
