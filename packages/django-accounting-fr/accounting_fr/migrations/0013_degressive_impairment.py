# Immobilisations : amortissement dégressif et dépréciations (type de dotation).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0012_chart_complements'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='depreciationrecord',
            name='unique_depreciation_line',
        ),
        migrations.AddField(
            model_name='depreciationrecord',
            name='kind',
            field=models.CharField(choices=[('depreciation', 'Amortissement'), ('impairment', 'Dépréciation')], default='depreciation', max_length=20),
        ),
        migrations.AlterField(
            model_name='fixedasset',
            name='method',
            field=models.CharField(choices=[('linear', 'Linéaire'), ('degressive', 'Dégressif fiscal (biens neufs, 3 ans et plus)'), ('none', 'Non amortissable (terrain, fonds commercial…)')], default='linear', max_length=10, verbose_name="Mode d'amortissement"),
        ),
        migrations.AddConstraint(
            model_name='depreciationrecord',
            constraint=models.UniqueConstraint(fields=('asset', 'transaction', 'kind'), name='unique_depreciation_line'),
        ),
    ]
