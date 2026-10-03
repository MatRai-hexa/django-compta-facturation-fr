# Comptabilité analytique : sections et ventilation des lignes de charges et de produits.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0014_currency'),
    ]

    operations = [
        migrations.CreateModel(
            name='AnalyticSection',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=20, unique=True, verbose_name='Code')),
                ('label', models.CharField(max_length=100, verbose_name='Libellé')),
                ('is_active', models.BooleanField(default=True, verbose_name='Active')),
            ],
            options={
                'verbose_name': 'Section analytique',
                'verbose_name_plural': 'Sections analytiques',
                'ordering': ('code',),
            },
        ),
        migrations.AddField(
            model_name='ledgerentry',
            name='analytic',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='entries', to='accounting.analyticsection', verbose_name='Section analytique'),
        ),
    ]
