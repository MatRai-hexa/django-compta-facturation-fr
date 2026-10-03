# Journal propre à chaque moyen de paiement (facultatif, BQ par défaut).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0001_squashed_0008_generic_documents'),
    ]

    operations = [
        migrations.AddField(
            model_name='paymentaccount',
            name='journal',
            field=models.ForeignKey(blank=True, help_text='Journal des encaissements, frais et virements de ce moyen (vide : BQ).', limit_choices_to={'kind': 'bank'}, null=True, on_delete=django.db.models.deletion.PROTECT, to='accounting.journal', verbose_name='Journal'),
        ),
    ]
