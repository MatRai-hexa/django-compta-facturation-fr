"""
Immobilisations : plan d'amortissement, dotations, sortie de l'actif.

- Amortissement linéaire prorata temporis, en jours sur la base d'une année de 360 jours (mois de
  30 jours), à compter de la mise en service ; le cumul ne dépasse jamais la valeur d'origine et le
  dernier exercice absorbe les arrondis.
- Amortissement dégressif fiscal (article 39 A du CGI) : taux linéaire multiplié par 1,25 (3 et 4 ans),
  1,75 (5 et 6 ans) ou 2,25 (au-delà), appliqué à la valeur nette, prorata en mois à compter du premier
  jour du mois de mise en service ; bascule en linéaire sur la durée restante dès qu'il est plus fort.
- `impair(immobilisation, date, montant)` : dépréciation (6816 / 29) ou, montant négatif, reprise
  (29 / 7816), dans la limite de la valeur nette ; elle ne modifie pas le plan d'amortissement.
- `post_depreciations(exercice)` passe, au dernier jour de l'exercice, une écriture de dotations (OD,
  validée) : débit 6811, crédit 28 pour chaque immobilisation, à hauteur de ce qui reste dû à cette
  date (une dotation oubliée d'un exercice précédent est donc rattrapée).
- `dispose(immobilisation, date, prix, taux de TVA)` : dotation complémentaire jusqu'à la sortie, puis
  écriture de sortie (débit 28 et 675 pour la valeur nette, crédit 2 ; dépréciation reprise en 29 / 7816 ;
  prix de cession en 462 / 775, et TVA collectée sur le prix si un taux est donné).

Les régularisations de TVA déduite (biens immobiliers, article 207 de l'annexe II au CGI) ne sont pas traitées.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Sum

from .models import ZERO, Account, DepreciationRecord, FiscalPeriod, FixedAsset, LedgerEntry, Transaction, to_cents
from .posting import AccountingError, Line, get_period, post_entry

CENT = Decimal("0.01")


def q(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


# === Comptes ===

def depreciation_account(asset: FixedAsset) -> Account:
    """Compte d'amortissement : celui de l'immobilisation, sinon 28 + compte (218300 → 281830), créé au besoin."""
    if asset.depreciation_account_id:
        return asset.depreciation_account
    code = ("28" + asset.account.code[1:5]).ljust(6, "0")
    account, _ = Account.objects.get_or_create(code=code, defaults={
        "name": f"Amortissements – {asset.account.name}"[:200], "account_type": "asset"})
    return account


def expense_account(asset: FixedAsset) -> Account:
    if asset.expense_account_id:
        return asset.expense_account
    code = "681110" if asset.account.code.startswith("20") else "681120"
    account, _ = Account.objects.get_or_create(code=code, defaults={
        "name": "Dotations aux amortissements des immobilisations " + ("incorporelles" if code == "681110" else "corporelles"),
        "account_type": "expense"})
    return account


def _account(code, name, kind):
    return Account.objects.get_or_create(code=code, defaults={"name": name, "account_type": kind})[0]


# === Plan d'amortissement ===

def days360(start: date, end: date) -> int:
    """Jours d'amortissement du `start` au `end` inclus, mois de 30 jours (fin de mois comptée 30)."""
    d1 = min(start.day, 30)
    d2 = 30 if end.day == monthrange(end.year, end.month)[1] else min(end.day, 30)
    return (end.year - start.year) * 360 + (end.month - start.month) * 30 + (d2 - d1) + 1


def degressive_coefficient(duration_months: int) -> Decimal:
    years = Decimal(duration_months) / 12
    return Decimal("1.25") if years <= 4 else Decimal("1.75") if years <= 6 else Decimal("2.25")


def _months(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month + 1


def _degressive_table(asset: FixedAsset) -> list[tuple[date, date, int, Decimal, Decimal]]:
    """[(début, fin, mois, dotation, cumul en fin d'exercice)] du plan dégressif, par exercice."""
    start = asset.service_date.replace(day=1)
    rate = Decimal(12) / asset.duration_months * degressive_coefficient(asset.duration_months)
    net, remaining, cumulative, table = asset.cost, asset.duration_months, ZERO, []
    for _, year_start, year_end in _years(asset):
        if year_end < start:
            continue
        begin = max(year_start, start)
        months = min(_months(begin, year_end), remaining)
        degressive = net * rate * months / 12
        linear = net * months / remaining  # linéaire sur la durée restante
        amount = net if months >= remaining else min(net, q(max(degressive, linear)))
        cumulative += amount
        net -= amount
        remaining -= months
        table.append((begin, year_end, months, amount, cumulative))
        if not net or remaining <= 0:
            break
    return table


def accrued(asset: FixedAsset, day: date) -> Decimal:
    """Amortissements cumulés dus au `day` inclus."""
    if asset.disposal_date and day > asset.disposal_date:
        day = asset.disposal_date
    if asset.method == "degressive":
        if day < asset.service_date.replace(day=1):
            return ZERO
        before = ZERO
        for begin, end, months, amount, cumulative in _degressive_table(asset):
            if day >= end:
                before = cumulative
                continue
            if day >= begin:
                return before + q(amount * min(_months(begin, day), months) / months)
            break
        return before
    if asset.method != "linear" or day < asset.service_date:
        return ZERO
    total = asset.duration_months * 30
    elapsed = days360(asset.service_date, day)
    return asset.cost if elapsed >= total else q(asset.cost * elapsed / total)


def posted(asset: FixedAsset, until: date | None = None, kind: str = "depreciation") -> Decimal:
    """Amortissements (ou, `kind="impairment"`, dépréciations nettes des reprises) passés."""
    records = asset.depreciations.filter(kind=kind)
    if until:
        records = records.filter(date__lte=until)
    return to_cents(records.aggregate(s=Sum("amount"))["s"])


def _years(asset: FixedAsset):
    """Exercices connus couvrant l'amortissement, prolongés d'années de 12 mois au-delà du dernier."""
    periods = list(FiscalPeriod.objects.filter(date_end__gte=asset.service_date).order_by("date_start"))
    if periods:
        for period in periods:
            yield period.name, period.date_start, period.date_end
        start = periods[-1].date_end + timedelta(days=1)
    else:
        start = date(asset.service_date.year, 1, 1)
    for _ in range(100):
        end = date(start.year + 1, start.month, start.day) - timedelta(days=1)
        yield (str(start.year) if start.month == 1 and start.day == 1 else f"{start:%m/%Y}–{end:%m/%Y}"), start, end
        start = end + timedelta(days=1)


def schedule(asset: FixedAsset) -> list[dict]:
    """Plan d'amortissement par exercice : dotation, cumul, valeur nette, dotations déjà passées."""
    rows = []
    if asset.method not in ("linear", "degressive"):
        return rows
    for name, start, end in _years(asset):
        if end < asset.service_date:
            continue
        before = accrued(asset, start - timedelta(days=1))
        cumulative = accrued(asset, end)
        if cumulative == before and before == asset.cost:
            break
        rows.append({"name": name, "start": start, "end": end, "amount": cumulative - before, "cumulative": cumulative,
                     "net": asset.cost - cumulative,
                     "posted": to_cents(asset.depreciations.filter(kind="depreciation", date__gte=start, date__lte=end)
                                        .aggregate(s=Sum("amount"))["s"])})
        if asset.disposal_date and asset.disposal_date <= end:
            break
    return rows


# === Écritures ===

def pending_depreciations(period: FiscalPeriod) -> list[tuple[FixedAsset, Decimal]]:
    """Dotations restant dues à la fin de l'exercice, par immobilisation en service et non sortie."""
    assets = FixedAsset.objects.filter(method__in=("linear", "degressive"), service_date__lte=period.date_end,
                                       disposal_date__isnull=True) \
        .select_related("account", "depreciation_account", "expense_account")
    result = []
    for asset in assets:
        amount = accrued(asset, period.date_end) - posted(asset)
        if amount > 0:
            result.append((asset, amount))
    return result


def _depreciation_entry(day, label, items, key, user):
    lines = []
    for asset, amount in items:
        text = f"Dotation {asset}"[:200]
        lines += [Line(expense_account(asset), debit=amount, label=text), Line(depreciation_account(asset), credit=amount, label=text)]
    txn = post_entry("OD", day, label, lines, entry_type="depreciation", source_key=key, user=user, validate=True)
    DepreciationRecord.objects.bulk_create([DepreciationRecord(asset=asset, transaction=txn, date=day, amount=amount)
                                            for asset, amount in items])
    return txn


@transaction.atomic
def post_depreciations(period: FiscalPeriod, user=None) -> Transaction:
    """Écriture des dotations de l'exercice (au dernier jour) ; une nouvelle passe ne prend que ce qui reste dû."""
    period = FiscalPeriod.objects.select_for_update().get(pk=period.pk)
    if period.is_closed:
        raise AccountingError(f"L'exercice {period.name} est clôturé.")
    items = pending_depreciations(period)
    if not items:
        raise AccountingError(f"Aucune dotation à passer pour l'exercice {period.name}.")
    runs = Transaction.objects.filter(source_key__startswith=f"depreciation:{period.pk}:").count()
    return _depreciation_entry(period.date_end, f"Dotations aux amortissements – exercice {period.name}", items,
                               f"depreciation:{period.pk}:{runs + 1}", user)


def impairment_account(asset: FixedAsset) -> Account:
    code = ("29" + asset.account.code[1:5]).ljust(6, "0")
    return Account.objects.get_or_create(code=code, defaults={
        "name": f"Dépréciations – {asset.account.name}"[:200], "account_type": "asset"})[0]


@transaction.atomic
def impair(asset: FixedAsset, day: date, amount: Decimal, user=None) -> Transaction:
    """Dépréciation (montant positif : 6816 / 29) ou reprise (montant négatif : 29 / 7816), validée."""
    asset = FixedAsset.objects.select_for_update().select_related("account").get(pk=asset.pk)
    amount = q(amount)
    if asset.is_disposed:
        raise AccountingError("Immobilisation sortie de l'actif.")
    if not amount:
        raise AccountingError("Montant nul.")
    current = posted(asset, kind="impairment")
    net = asset.cost - accrued(asset, day) - current
    if amount > net:
        raise AccountingError(f"La dépréciation dépasse la valeur nette comptable ({net} €).")
    if -amount > current:
        raise AccountingError(f"La reprise dépasse la dépréciation constatée ({current} €).")
    get_period(day)
    label = (f"Dépréciation {asset}" if amount > 0 else f"Reprise de dépréciation {asset}")[:200]
    reserve = impairment_account(asset)
    if amount > 0:
        lines = [Line(_account("681600", "Dotations aux dépréciations des immobilisations", "expense"), debit=amount, label=label),
                 Line(reserve, credit=amount, label=label)]
    else:
        lines = [Line(reserve, debit=-amount, label=label),
                 Line(_account("781600", "Reprises sur dépréciations des immobilisations", "revenue"), credit=-amount, label=label)]
    runs = DepreciationRecord.objects.filter(asset=asset, kind="impairment").count()
    txn = post_entry("OD", day, label, lines, entry_type="depreciation", source_key=f"impairment:{asset.pk}:{runs + 1}",
                     user=user, validate=True, document_type="fixed_asset", document_id=str(asset.pk))
    DepreciationRecord.objects.create(asset=asset, transaction=txn, date=day, amount=amount, kind="impairment")
    return txn


@transaction.atomic
def dispose(asset: FixedAsset, day: date, price: Decimal | None = None, user=None, vat_rate: Decimal | None = None) -> Transaction:
    """Cession (prix > 0) ou mise au rebut : dotation complémentaire jusqu'à la sortie, puis écriture de sortie."""
    asset = FixedAsset.objects.select_for_update().select_related("account").get(pk=asset.pk)
    if asset.is_disposed:
        raise AccountingError("Immobilisation déjà sortie de l'actif.")
    if day < asset.acquisition_date:
        raise AccountingError("La sortie ne précède pas l'acquisition.")
    if asset.depreciations.filter(date__gt=day).exists():
        raise AccountingError("Des dotations sont passées après cette date : sortie impossible à cette date.")
    get_period(day)  # exercice ouvert
    price = q(price or 0)
    complement = accrued(asset, day) - posted(asset)
    if complement > 0:
        _depreciation_entry(day, f"Dotation complémentaire avant sortie – {asset}"[:255], [(asset, complement)],
                            f"depreciation:asset:{asset.pk}:disposal", user)
    cumulative = posted(asset)
    net = asset.cost - cumulative
    label = (f"Cession {asset}" if price else f"Mise au rebut {asset}")[:200]
    lines = [Line(asset.account, credit=asset.cost, label=label)]
    if cumulative:
        lines.append(Line(depreciation_account(asset), debit=cumulative, label=label))
    if net:
        lines.append(Line(_account("675000", "Valeurs comptables des éléments d'actif cédés", "expense"), debit=net, label=label))
    reserve = posted(asset, kind="impairment")
    if reserve:  # la dépréciation est reprise à la sortie
        lines += [Line(impairment_account(asset), debit=reserve, label=label),
                  Line(_account("781600", "Reprises sur dépréciations des immobilisations", "revenue"), credit=reserve, label=label)]
    if price:
        vat = ZERO
        if vat_rate:
            from .api import _vat_account

            rate = Decimal(vat_rate).quantize(Decimal("0.0001"))
            vat = q(price * rate)
            lines.append(Line(_vat_account(rate), credit=vat, label=f"TVA sur cession - {label}"[:200], vat_rate=rate, vat_base=price))
        lines += [Line(_account("462000", "Créances sur cessions d'immobilisations", "asset"), debit=price + vat, label=label),
                  Line(_account("775000", "Produits des cessions d'éléments d'actif", "revenue"), credit=price, label=label)]
    txn = post_entry("OD", day, label, lines, entry_type="disposal", source_key=f"disposal:{asset.pk}", user=user,
                     validate=True, document_type="fixed_asset", document_id=str(asset.pk))
    FixedAsset.objects.filter(pk=asset.pk).update(disposal_date=day, disposal_price=price or None)
    return txn


def register_gaps() -> list[dict]:
    """Écarts entre le registre (immobilisations non sorties) et le solde des comptes de la classe 2."""
    register = {}
    for asset in FixedAsset.objects.filter(disposal_date__isnull=True).select_related("account"):
        register[asset.account.code] = register.get(asset.account.code, ZERO) + asset.cost
    books = {}
    rows = (LedgerEntry.objects.filter(account__code__startswith="2", transaction__is_validated=True)
            .exclude(account__code__regex=r"^2(8|9)").values("account__code")
            .annotate(d=Sum("debit"), c=Sum("credit")))
    for r in rows:
        books[r["account__code"]] = to_cents(r["d"]) - to_cents(r["c"])
    return [{"account": code, "register": register.get(code, ZERO), "books": books.get(code, ZERO),
             "gap": books.get(code, ZERO) - register.get(code, ZERO)}
            for code in sorted(set(register) | set(books)) if books.get(code, ZERO) != register.get(code, ZERO)]
