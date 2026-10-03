"""
API publique de la comptabilité, à utiliser par les projets qui l'intègrent.

Le projet décrit ses pièces (vente, encaissement, avoir, remboursement) ;
la comptabilité en déduit les écritures en partie double selon le plan
comptable et les paramètres (comptes clients, ventes, TVA par taux...).

Chaque pièce porte une clé unique (`key`) : la rejouer ne crée jamais de
doublon. Exemple :

    from accounting import api

    api.post_sale(
        key="invoice:F2026-001", date=day, reference="F2026-001", label="Facture F2026-001",
        customer=api.Party("C042", "Dupont SARL"),
        lines=[api.SaleLine(Decimal("120.00"), Decimal("0.20")),                   # marchandises TTC
               api.SaleLine(Decimal("6.00"), Decimal("0.20"), kind="shipping")],   # port TTC
        document=("invoice", "F2026-001"),
    )
    api.post_payment(key="invoice:F2026-001:payment", date=day, reference="VIR-123",
                     label="Règlement F2026-001", amount=Decimal("126.00"), method="transfer",
                     customer=api.Party("C042", "Dupont SARL"), document=("invoice", "F2026-001"))
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date as date_type
from decimal import Decimal

from .models import ZERO, Account, AccountingSettings, LedgerEntry, Transaction, VATRate
from .posting import AccountingError, Line, percent, post_entry, q

__all__ = ["AccountingError", "Party", "PurchaseLine", "SaleLine", "entries_for", "is_posted", "post_credit_note",
           "post_fee", "post_payment", "post_purchase", "post_refund", "post_sale", "post_transfer"]

KIND_LABELS = {"goods": "Ventes de marchandises", "shipping": "Frais de port", "services": "Prestations de services"}


@dataclass
class Party:
    """Tiers (client) : code et libellé auxiliaires (CompAuxNum / CompAuxLib du FEC)."""
    code: str
    label: str = ""

    def as_line_kwargs(self):
        return {"auxiliary_code": (self.code or "")[:40], "auxiliary_label": (self.label or self.code or "")[:120]}


def _section(code):
    if not code:
        return None
    from .models import AnalyticSection

    section = AnalyticSection.objects.filter(code=code).first()
    if section is None:
        raise AccountingError(f"Section analytique {code} inconnue.")
    return section


def _currency(currency):
    """("USD", Decimal) -> arguments de ligne en devise ; None -> aucun."""
    if not currency:
        return {}
    code, amount = currency
    return {"currency": code, "currency_amount": Decimal(amount)}


@dataclass
class SaleLine:
    """Montant d'une vente à un taux de TVA. `kind` : goods, shipping ou services."""
    amount: Decimal
    vat_rate: Decimal = Decimal("0.2000")
    kind: str = "goods"
    analytic: str = ""  # code de section analytique (facultatif)


def _vat_account(rate):
    vat = VATRate.objects.filter(rate=rate).select_related("collected_account").first()
    if vat is None or vat.collected_account is None:
        raise AccountingError(f"Taux de TVA {percent(rate)} % non paramétré (Comptabilité › Taux de TVA).")
    return vat.collected_account


def _split(total: Decimal, weights: dict) -> dict:
    """Répartit `total` au prorata des poids, au centime ; l'écart d'arrondi va au poids le plus fort."""
    weight = sum(weights.values(), ZERO)
    shares = {key: (q(total * value / weight) if weight else ZERO) for key, value in weights.items()}
    if shares:
        shares[max(weights, key=lambda key: weights[key])] += total - sum(shares.values(), ZERO)
    return shares


def _sale_lines(conf, customer: Party, lines: list[SaleLine], prices_include_tax: bool, label: str,
                currency=None) -> list[Line]:
    """
    Écriture de vente : débit client TTC ; crédit produits HT par nature et TVA collectée par taux.

    La TVA se calcule par taux, comme sur une facture conforme à la norme EN 16931 : prix TTC,
    base HT = TTC / (1 + taux) arrondie et TVA = TTC − base ; prix HT, TVA = base × taux arrondie.
    La base de chaque taux est ensuite répartie entre ventes, port et prestations. Sauf option pour la
    TVA sur les débits, la TVA des prestations de services est exigible à l'encaissement : elle reste en
    attente (4458) jusqu'au paiement (voir `_release_pending_vat`).
    """
    accounts = {"goods": conf.sales_account, "shipping": conf.shipping_account, "services": conf.sales_account}
    by_rate = defaultdict(lambda: defaultdict(lambda: ZERO))
    for line in lines:
        if line.kind not in accounts:
            raise AccountingError(f"Nature de ligne inconnue : {line.kind}")
        by_rate[Decimal(line.vat_rate).quantize(Decimal("0.0001"))][(line.kind, line.analytic or "")] += Decimal(line.amount)
    result, total = [], ZERO
    for rate, kinds in sorted(by_rate.items(), reverse=True):
        if prices_include_tax:
            ttc = q(sum(kinds.values(), ZERO))
            base = q(ttc / (1 + rate))
            vat = ttc - base
            bases = _split(base, kinds)
        else:
            bases = {kind: q(amount) for kind, amount in kinds.items()}
            base = sum(bases.values(), ZERO)
            vat = q(base * rate)
            ttc = base + vat
        total += ttc
        for (kind, section), amount in sorted(bases.items(), key=lambda item: (("goods", "services", "shipping").index(item[0][0]), item[0][1])):
            if amount:
                result.append(Line(accounts[kind], credit=amount, label=f"{KIND_LABELS[kind]} {percent(rate)} %",
                                   vat_rate=rate, analytic=_section(section)))
        if vat:
            base_services = sum((v for (k, _), v in bases.items() if k == "services"), ZERO) if not conf.vat_on_debits else ZERO
            vat_services = _split(vat, {"services": base_services, "other": base - base_services})["services"] \
                if base_services else ZERO
            if vat - vat_services:
                result.append(Line(_vat_account(rate), credit=vat - vat_services, label=f"TVA collectée {percent(rate)} %",
                                   vat_rate=rate, vat_base=base - base_services))
            if vat_services:
                result.append(Line(_pending_vat_account(conf), credit=vat_services, vat_rate=rate, vat_base=base_services,
                                   label=f"TVA sur prestations en attente d'encaissement {percent(rate)} %"))
    result.insert(0, Line(conf.customer_account, debit=total, label=label, **customer.as_line_kwargs(), **_currency(currency)))
    return result


def _pending_vat_account(conf):
    return _settings_account(conf, "pending_vat_account", "445800")


def _signed(entry, field):
    """Montant d'une ligne vu du crédit (TVA collectée) : + au crédit, − au débit."""
    value = getattr(entry, field) or ZERO
    return value if entry.credit else -value


def _release_pending_vat(conf, key, day, label, document, amount, sign, user, validate):
    """
    TVA des prestations exigible à l'encaissement : un encaissement (sign = 1) fait passer la part
    correspondante de la TVA en attente (4458) en TVA collectée ; un remboursement (sign = -1) l'inverse.
    Au prorata du montant sur le TTC de la vente, sans jamais dépasser ce qui reste en attente.
    """
    if conf.vat_on_debits or not document or not document[0]:
        return None
    pending = _pending_vat_account(conf)
    documents = Transaction.objects.filter(document_type=document[0], document_id=str(document[1]))
    sales = documents.filter(type="sale")
    initial, current = defaultdict(lambda: [ZERO, ZERO]), defaultdict(lambda: [ZERO, ZERO])
    for e in LedgerEntry.objects.filter(transaction__in=sales, account=pending):
        initial[e.vat_rate][0] += e.credit - e.debit
        initial[e.vat_rate][1] += _signed(e, "vat_base")
    if not initial:
        return None
    for e in LedgerEntry.objects.filter(transaction__in=documents, account=pending):
        current[e.vat_rate][0] += e.credit - e.debit
        current[e.vat_rate][1] += _signed(e, "vat_base")
    ttc = sum((e.debit for e in LedgerEntry.objects.filter(transaction__in=sales, account=conf.customer_account)), ZERO)
    ratio = min(Decimal("1"), Decimal(amount) / ttc) if ttc else Decimal("1")
    lines = []
    for rate, (vat_initial, base_initial) in sorted(initial.items(), key=lambda item: -(item[0] or 0)):
        vat_now, base_now = current[rate]
        target = sign * q(vat_initial * ratio)
        move = min(target, vat_now) if sign > 0 else max(target, vat_now)
        if (sign > 0 and vat_now <= 0) or (sign < 0 and vat_now >= 0) or not move:
            continue
        base = base_now if move == vat_now else q(sign * base_initial * ratio * move / target)
        text = f"TVA exigible à l'encaissement {percent(rate)} %"
        side = {"debit": abs(move)} if move > 0 else {"credit": abs(move)}
        other = {"credit": abs(move)} if move > 0 else {"debit": abs(move)}
        lines += [Line(pending, **side, vat_rate=rate, vat_base=abs(base), label=text),
                  Line(_vat_account(rate), **other, vat_rate=rate, vat_base=abs(base), label=text)]
    if not lines:
        return None
    return post_entry("OD", day, f"{label} : TVA exigible"[:255], lines, entry_type="vat_release",
                      source_key=f"{key}:tva", user=user, validate=validate, **_doc(document))


def _doc(document):
    document_type, document_id = document or ("", "")
    return {"document_type": document_type or "", "document_id": str(document_id or "")}


def post_sale(key: str, date: date_type, reference: str, label: str, customer: Party, lines: list[SaleLine],
              prices_include_tax: bool = True, document=None, user=None, validate=None, currency=None) -> Transaction:
    """
    Vente (journal VT), montants en euros. Idempotent par `key`. `currency=("USD", montant TTC en
    devise)` : facture en devise, montant d'origine porté sur la ligne du client.
    """
    conf = AccountingSettings.get()
    return post_entry("VT", date, label, _sale_lines(conf, customer, lines, prices_include_tax, label, currency),
                      entry_type="sale", reference=reference, source_key=key, user=user,
                      validate=conf.auto_validate if validate is None else validate, **_doc(document))


def post_payment(key: str, date: date_type, reference: str, label: str, amount: Decimal, method: str,
                 customer: Party, document=None, user=None, validate=None, currency=None) -> Transaction:
    """Encaissement (journal BQ) : débit compte du moyen de paiement, crédit client."""
    conf = AccountingSettings.get()
    amount = q(amount)
    lines = [Line(conf.payment_account(method), debit=amount, label=label, **_currency(currency)),
             Line(conf.customer_account, credit=amount, label=label, **customer.as_line_kwargs(), **_currency(currency))]
    validate = conf.auto_validate if validate is None else validate
    entry = post_entry(conf.payment_journal(method), date, label, lines, entry_type="payment", reference=reference,
                       source_key=key, user=user, validate=validate, **_doc(document))
    _release_pending_vat(conf, key, date, label, document, amount, 1, user, validate)
    return entry


def post_credit_note(key: str, sale_key: str, date: date_type, reference: str, label: str, user=None,
                     validate=None) -> Transaction:
    """Avoir (journal VT) : annule intégralement une vente (lignes inversées, TVA comprise)."""
    sale = Transaction.objects.filter(source_key=sale_key).first()
    if sale is None:
        raise AccountingError(f"Vente {sale_key} introuvable : avoir impossible.")
    conf = AccountingSettings.get()
    lines = [Line(e.account, debit=e.credit, credit=e.debit, label=f"Avoir : {e.label}"[:200], vat_rate=e.vat_rate,
                  vat_base=e.vat_base, currency=e.currency, currency_amount=e.currency_amount,
                  auxiliary_code=e.auxiliary_code, auxiliary_label=e.auxiliary_label) for e in sale.entries.all()]
    return post_entry("VT", date, label, lines, entry_type="refund", reference=reference, source_key=key, user=user,
                      validate=conf.auto_validate if validate is None else validate,
                      document_type=sale.document_type, document_id=sale.document_id)


def post_refund(key: str, date: date_type, reference: str, label: str, amount: Decimal, method: str,
                customer: Party, document=None, user=None, validate=None, currency=None) -> Transaction:
    """Remboursement au client (journal BQ) : débit client, crédit compte du moyen de paiement."""
    conf = AccountingSettings.get()
    amount = q(amount)
    lines = [Line(conf.customer_account, debit=amount, label=label, **customer.as_line_kwargs(), **_currency(currency)),
             Line(conf.payment_account(method), credit=amount, label=label, **_currency(currency))]
    validate = conf.auto_validate if validate is None else validate
    entry = post_entry(conf.payment_journal(method), date, label, lines, entry_type="payout", reference=reference,
                       source_key=key, user=user, validate=validate, **_doc(document))
    _release_pending_vat(conf, key, date, label, document, amount, -1, user, validate)
    return entry


def post_fee(key: str, date: date_type, reference: str, label: str, amount: Decimal, method: str,
             document=None, user=None, validate=None) -> Transaction:
    """
    Frais prélevés par un prestataire de paiement (journal BQ) : débit frais (627), crédit compte
    du moyen de paiement. Les commissions des établissements de paiement sont exonérées de TVA.
    """
    conf = AccountingSettings.get()
    amount = q(amount)
    lines = [Line(conf.fees_account, debit=amount, label=label),
             Line(conf.payment_account(method), credit=amount, label=label)]
    return post_entry(conf.payment_journal(method), date, label, lines, entry_type="fee", reference=reference, source_key=key, user=user,
                      validate=conf.auto_validate if validate is None else validate, **_doc(document))


def post_transfer(key: str, date: date_type, reference: str, label: str, amount: Decimal, method: str,
                  document=None, user=None, validate=None) -> Transaction:
    """
    Virement du prestataire vers la banque, par le compte de virements internes (580) : une écriture
    dans le journal du moyen de paiement (débit 580, crédit compte du moyen, clé `key`) et une dans
    celui de la banque (débit banque, crédit 580, clé `key:banque`), que l'on rapproche chacune avec
    son relevé. Un montant négatif (prélèvement du prestataire sur la banque) inverse les sens.
    Renvoie l'écriture du moyen de paiement.
    """
    existing = Transaction.objects.filter(source_key=key).first()
    if existing:  # y compris un virement d'avant les journaux de trésorerie, passé en une seule écriture
        return existing
    conf = AccountingSettings.get()
    amount = q(amount)
    provider_journal, bank_journal = conf.payment_mapping_journal(method), conf.bank_journal()
    if provider_journal.pk == bank_journal.pk:
        raise AccountingError(f"Le moyen de paiement « {method} » est déjà comptabilisé en banque.")
    transit = _settings_account(conf, "transfer_account", "580000")
    validate = conf.auto_validate if validate is None else validate
    common = {"entry_type": "transfer", "reference": reference, "user": user, "validate": validate, **_doc(document)}

    def side(journal, sign):  # sign > 0 : les fonds sortent de ce journal vers le 580
        own = Line(journal.account, **{"credit" if sign > 0 else "debit": abs(amount)}, label=label)
        link = Line(transit, **{"debit" if sign > 0 else "credit": abs(amount)}, label=label)
        return [link, own] if sign > 0 else [own, link]

    sign = 1 if amount >= 0 else -1
    entry = post_entry(provider_journal.code, date, label, side(provider_journal, sign), source_key=key, **common)
    post_entry(bank_journal.code, date, label, side(bank_journal, -sign), source_key=f"{key}:banque", **common)
    return entry


@dataclass
class PurchaseLine:
    """Achat à un taux de TVA : base HT, TVA (telle que facturée par le fournisseur), compte de charge."""
    base: Decimal
    vat: Decimal = ZERO
    account_code: str = ""   # vide : compte d'achat par défaut des paramètres
    label: str = ""
    vat_rate: Decimal | None = None  # obligatoire en autoliquidation (la facture est hors taxe)
    analytic: str = ""                # code de section analytique (facultatif)


# Autoliquidation par l'acheteur : la TVA est due (4452) et déductible (445662) par la même écriture
REVERSE_CHARGE = {
    "eu_goods": "Acquisition intracommunautaire de biens",
    "eu_services": "Prestation de services intracommunautaire (article 283-2 du CGI)",
    "foreign": "Achat auprès d'un assujetti non établi en France (article 283-1 du CGI)",
}


def _settings_account(conf, field, code):
    return getattr(conf, field) or Account.objects.get(code=code)


def _chart_account(code, name, kind):
    return Account.objects.get_or_create(code=code, defaults={"name": name, "account_type": kind})[0]


def post_purchase(key: str, date: date_type, reference: str, label: str, supplier: Party, lines: list[PurchaseLine],
                  credit_note: bool = False, document=None, user=None, validate=None, reverse_charge: str = "",
                  currency=None) -> Transaction:
    """
    Facture fournisseur (journal AC) : débit charges HT et TVA déductible, crédit fournisseur TTC.
    Avoir fournisseur (`credit_note=True`) : sens inverses. La TVA d'une immobilisation (classe 2) est
    déduite en 445620. `reverse_charge` (eu_goods, eu_services, foreign) : facture hors taxe, TVA
    autoliquidée au taux de chaque ligne, due en 445200 et déductible en 445662 (ou 445620).
    """
    if reverse_charge and reverse_charge not in REVERSE_CHARGE:
        raise AccountingError(f"Autoliquidation inconnue : {reverse_charge}")
    conf = AccountingSettings.get()
    supplier_account = _settings_account(conf, "supplier_account", "401000")
    default_expense = _settings_account(conf, "purchases_account", "607000")
    vat_account = _settings_account(conf, "deductible_vat_account", "445660")
    side = "credit" if credit_note else "debit"
    other = "debit" if credit_note else "credit"
    result, total = [], ZERO
    vat_lines = defaultdict(lambda: [ZERO, ZERO])  # (compte, taux, sens) -> [TVA, base]
    for line in lines:
        account = Account.objects.get(code=line.account_code) if line.account_code else default_expense
        base = q(line.base)
        fixed_asset = account.code.startswith("2")
        if base:
            result.append(Line(account, **{side: base}, label=(line.label or label)[:200], analytic=_section(line.analytic)))
        if reverse_charge:
            if line.vat_rate is None:
                raise AccountingError("Autoliquidation : le taux de TVA de chaque ligne est obligatoire.")
            rate = Decimal(line.vat_rate).quantize(Decimal("0.0001"))
            vat = q(base * rate)
            deductible = _chart_account("445620", "TVA déductible sur immobilisations", "asset") if fixed_asset else \
                _chart_account("445662", "TVA déductible intracommunautaire et autoliquidée", "asset")
            due = _chart_account("445200", "TVA due intracommunautaire et autoliquidée", "liability")
            for account_, direction in ((deductible, side), (due, other)):
                vat_lines[(account_, rate, direction)][0] += vat
                vat_lines[(account_, rate, direction)][1] += base
            total += base
        else:
            vat = q(line.vat)
            rate = Decimal(line.vat_rate).quantize(Decimal("0.0001")) if line.vat_rate is not None else None
            deductible = _chart_account("445620", "TVA déductible sur immobilisations", "asset") if fixed_asset else vat_account
            if vat:
                vat_lines[(deductible, rate, side)][0] += vat
                vat_lines[(deductible, rate, side)][1] += base
            total += base + vat
    for (account, rate, direction), (vat, base) in vat_lines.items():
        if vat:
            text = ("TVA autoliquidée" if account.code.startswith("4452") else "TVA déductible") + f" - {label}"
            result.append(Line(account, **{direction: vat}, label=text[:200], vat_rate=rate, vat_base=base))
    result.append(Line(supplier_account, **{other: total}, label=label[:200], **supplier.as_line_kwargs(), **_currency(currency)))
    return post_entry("AC", date, label, result, entry_type="refund" if credit_note else "expense", reference=reference,
                      source_key=key, user=user, validate=conf.auto_validate if validate is None else validate,
                      tags=f"autoliquidation:{reverse_charge}" if reverse_charge else "", **_doc(document))


def is_posted(key: str) -> bool:
    return Transaction.objects.filter(source_key=key).exists()


def entries_for(document_type: str, document_id) -> "list[Transaction]":
    return list(Transaction.objects.filter(document_type=document_type, document_id=str(document_id))
                .select_related("journal").order_by("date", "pk"))
