"""
Calcul des montants d'une facture selon la norme EN 16931.

- base HT d'un taux = somme des lignes HT − remises HT de ce taux ;
- prix HT (factures entre professionnels) : TVA = base × taux, arrondie au centime ;
- prix TTC (ventes aux particuliers) : base = TTC / (1 + taux) arrondie au centime et
  TVA = TTC − base, comme en comptabilité. Le total à payer est exactement le prix payé ;
  l'écart éventuel d'un centime avec base × taux est admis par la règle BR-CO-17 (tolérance
  d'une unité monétaire dans le schématron officiel).
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def q(value) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def rate_of(value) -> Decimal:
    return Decimal(value).quantize(Decimal("0.0001"))


def vat_of(base: Decimal, rate: Decimal) -> Decimal:
    return q(base * rate)


def split_ttc(ttc: Decimal, rate: Decimal) -> tuple[Decimal, Decimal]:
    """(base HT, TVA) d'un montant TTC, identiques à ceux de la comptabilité."""
    ttc = q(ttc)
    base = q(ttc / (1 + rate))
    return base, ttc - base


@dataclass
class Item:
    """Ligne saisie : prix unitaire TTC ou HT selon la facture."""
    description: str
    quantity: Decimal
    unit_price: Decimal
    vat_rate: Decimal = Decimal("0.2000")
    unit: str = "C62"  # unité (code UN/ECE) : C62 = pièce
    nature: str = ""   # "goods" ou "services" ; vide : nature des opérations des paramètres

    @property
    def gross(self) -> Decimal:
        return q(Decimal(self.quantity) * Decimal(self.unit_price))


@dataclass
class Allowance:
    """Remise sur l'ensemble de la facture (code promo), pour un taux de TVA."""
    label: str
    amount: Decimal
    vat_rate: Decimal = Decimal("0.2000")


@dataclass
class Computed:
    lines: list = field(default_factory=list)       # [{description, quantity, unit, net_price, net_amount, vat_rate}]
    allowances: list = field(default_factory=list)  # [{label, amount, vat_rate}] montants HT
    vat: list = field(default_factory=list)         # [{rate, base, vat}]
    lines_total: Decimal = ZERO                      # BT-106
    allowances_total: Decimal = ZERO                 # BT-107
    total_ht: Decimal = ZERO                         # BT-109
    total_vat: Decimal = ZERO                        # BT-110
    total_ttc: Decimal = ZERO                        # BT-112
    payable: Decimal = ZERO                          # BT-115


def _distribute(total: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Répartit `total` au prorata des poids, au centime, l'écart sur la ligne la plus lourde."""
    weight = sum(weights, ZERO)
    if not weights:
        return []
    if not weight:
        shares = [ZERO] * len(weights)
        shares[0] = total
        return shares
    shares = [q(total * w / weight) for w in weights]
    shares[max(range(len(weights)), key=lambda i: weights[i])] += total - sum(shares, ZERO)
    return shares


def compute(items: list[Item], allowances: list[Allowance] = (), prices_include_tax: bool = True) -> Computed:
    by_rate = OrderedDict()
    for index, item in enumerate(items):
        by_rate.setdefault(rate_of(item.vat_rate), {"items": [], "allowances": []})["items"].append((index, item))
    for allowance in allowances:
        if q(allowance.amount):
            by_rate.setdefault(rate_of(allowance.vat_rate), {"items": [], "allowances": []})["allowances"].append(allowance)

    result = Computed()
    nets = {}
    for rate, group in sorted(by_rate.items(), reverse=True):
        gross = [item.gross for _, item in group["items"]]
        allowance_amounts = [q(a.amount) for a in group["allowances"]]
        if prices_include_tax:
            base, vat = split_ttc(sum(gross, ZERO) - sum(allowance_amounts, ZERO), rate)
            allowance_ht = [split_ttc(amount, rate)[0] for amount in allowance_amounts]
            line_nets = _distribute(base + sum(allowance_ht, ZERO), gross)
        else:
            allowance_ht, line_nets = allowance_amounts, gross
            base = sum(line_nets, ZERO) - sum(allowance_ht, ZERO)
            vat = vat_of(base, rate)
        for (index, _), net in zip(group["items"], line_nets):
            nets[index] = net
        for allowance, amount in zip(group["allowances"], allowance_ht):
            result.allowances.append({"label": allowance.label, "amount": amount, "vat_rate": rate})
        result.vat.append({"rate": rate, "base": base, "vat": vat})
        result.allowances_total += sum(allowance_ht, ZERO)
        result.total_ht += base
        result.total_vat += vat

    for index, item in enumerate(items):
        quantity = Decimal(item.quantity)
        net = nets[index]
        result.lines.append({
            "description": item.description, "quantity": quantity, "unit": item.unit, "vat_rate": rate_of(item.vat_rate),
            "nature": item.nature,
            "net_amount": net, "net_price": (net / quantity).quantize(Decimal("0.000001")) if quantity else net,
        })
        result.lines_total += net
    result.total_ttc = result.total_ht + result.total_vat
    result.payable = result.total_ttc
    return result
