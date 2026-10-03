# Comptes ajoutés au plan par défaut : autoliquidation et TVA sur immobilisations, dépréciations, écarts de change.

from django.db import migrations

ACCOUNTS = [
    ("445200", "TVA due intracommunautaire et autoliquidée", "liability"),
    ("445620", "TVA déductible sur immobilisations", "asset"),
    ("445662", "TVA déductible intracommunautaire et autoliquidée", "asset"),
    ("666000", "Pertes de change financières", "expense"),
    ("681600", "Dotations aux dépréciations des immobilisations", "expense"),
    ("766000", "Gains de change financiers", "revenue"),
    ("781600", "Reprises sur dépréciations des immobilisations", "revenue"),
]


def add_accounts(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    if not Account.objects.exists():
        return
    for code, name, kind in ACCOUNTS:
        Account.objects.get_or_create(code=code, defaults={"name": name, "account_type": kind})


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0011_seal_anchors"),
    ]

    operations = [
        migrations.RunPython(add_accounts, migrations.RunPython.noop),
    ]
