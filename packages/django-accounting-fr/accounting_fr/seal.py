"""
Empreinte chaînée des écritures validées (inaltérabilité).

À sa validation, chaque écriture reçoit un rang dans une chaîne unique (1, 2, 3…) et une empreinte
SHA-256 calculée sur son contenu (numéro, journal, dates, pièce, libellé, montant, lignes) et sur
l'empreinte de l'écriture précédente. Modifier, supprimer ou insérer une écriture validée après
coup, même directement en base, casse la chaîne : `verify()` le détecte et désigne la première
écriture en cause.

À la clôture d'un exercice, l'empreinte de la dernière écriture de la chaîne devient le sceau de
l'exercice. Conservé hors de la base, ce sceau prouve que rien n'a été réécrit depuis, y compris par
quelqu'un qui recalculerait toute la chaîne. `anchor()` l'envoie chaque jour (et à chaque clôture) par
e-mail aux destinataires des paramètres comptables et le consigne dans un fichier hors de la base
(`ACCOUNTING["SEAL_ARCHIVE_DIR"]`) ; `check_anchor(rang, sceau)` contrôle un sceau ainsi conservé.

Ne font pas partie de l'empreinte les informations qui évoluent légitimement après la validation :
lettrage, pointage bancaire, lien vers une contre-passation.
"""
from __future__ import annotations

import hashlib
import json

import logging
import re
from pathlib import Path

from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

GENESIS = "0" * 64
CHUNK = 500


def _iso(value):
    return value.isoformat() if value else ""


def payload(txn) -> bytes:
    """Contenu scellé d'une écriture (accès par attributs : utilisable avec les modèles historiques)."""
    lines = [[e.account.code, str(e.debit), str(e.credit), e.label, e.auxiliary_code, e.auxiliary_label,
              "" if e.vat_rate is None else str(e.vat_rate)]
             + ([e.currency, str(e.currency_amount)] if getattr(e, "currency", "") else [])  # lignes en devise seulement
             for e in sorted(txn.entries.all(), key=lambda e: e.pk)]
    data = {
        "number": txn.number, "journal": txn.journal.code, "date": _iso(txn.date), "piece_date": _iso(txn.piece_date),
        "reference": txn.reference, "description": txn.description, "amount": str(txn.amount), "currency": txn.currency,
        "type": txn.type, "document": [txn.document_type, txn.document_id], "validated_at": _iso(txn.validated_at),
        "lines": lines,
    }
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def digest(previous: str, content: bytes) -> str:
    return hashlib.sha256(previous.encode("ascii") + content).hexdigest()


def _chain(SealChain):
    chain, _ = SealChain.objects.get_or_create(pk=1)
    return SealChain.objects.select_for_update().get(pk=chain.pk)


def seal_transaction(txn, Transaction=None, SealChain=None) -> str:
    """Rang et empreinte d'une écriture qui vient d'être validée (dans la transaction de validation)."""
    if Transaction is None:
        from .models import SealChain, Transaction
    chain = _chain(SealChain)
    txn = Transaction.objects.select_related("journal").prefetch_related("entries__account").get(pk=txn.pk)
    value = digest(chain.last_seal or GENESIS, payload(txn))
    chain.last_index += 1
    chain.last_seal = value
    chain.save(update_fields=["last_index", "last_seal"])
    Transaction.objects.filter(pk=txn.pk).update(seal_index=chain.last_index, seal=value)
    return value


def seal_period(period) -> None:
    """Sceau d'un exercice qu'on clôture : dernier maillon de la chaîne à cet instant."""
    from .models import FiscalPeriod, SealChain

    chain = _chain(SealChain)
    FiscalPeriod.objects.filter(pk=period.pk).update(closing_index=chain.last_index, closing_seal=chain.last_seal or GENESIS)


def verify() -> dict:
    """Recalcule toute la chaîne. {"ok", "count", "errors", "last_index", "last_seal", "checked_at"}."""
    from .models import FiscalPeriod, SealChain, Transaction

    errors = []
    unsealed = Transaction.objects.filter(is_validated=True, seal_index__isnull=True)
    for txn in unsealed[:20]:
        errors.append({"index": None, "number": txn.number, "message": "Écriture validée sans empreinte (ajoutée hors validation)."})
    expected_index, previous = 0, GENESIS
    seals = {}
    sealed = (Transaction.objects.filter(seal_index__isnull=False).order_by("seal_index")
              .select_related("journal").prefetch_related("entries__account"))
    count = 0
    for txn in sealed.iterator(chunk_size=CHUNK):
        count += 1
        expected_index += 1
        if txn.seal_index != expected_index:
            errors.append({"index": expected_index, "number": txn.number,
                           "message": f"Maillon {expected_index} manquant : écriture supprimée ou rang modifié."})
            expected_index = txn.seal_index
            previous, seals[txn.seal_index] = txn.seal, txn.seal  # maillon suivant le trou : invérifiable
            continue
        if not txn.is_validated:
            errors.append({"index": txn.seal_index, "number": txn.number, "message": "Écriture scellée repassée en brouillon."})
        value = digest(previous, payload(txn))
        if value != txn.seal:
            errors.append({"index": txn.seal_index, "number": txn.number,
                           "message": "Contenu modifié après validation (empreinte différente)."})
        previous = txn.seal  # la suite se vérifie par rapport à l'empreinte enregistrée
        seals[txn.seal_index] = txn.seal
        if len(errors) >= 50:
            break
    chain = SealChain.objects.filter(pk=1).first()
    if chain and (chain.last_index != expected_index or (chain.last_seal or GENESIS) != (previous if count else GENESIS)):
        errors.append({"index": chain.last_index, "number": "",
                       "message": "Fin de chaîne différente du dernier maillon enregistré : écritures supprimées en fin de chaîne."})
    from .models import SealAnchor

    for sent in SealAnchor.objects.filter(error="").exclude(index__gt=expected_index):
        recorded = seals.get(sent.index, GENESIS if sent.index == 0 else None)
        if recorded is not None and recorded != sent.seal:
            errors.append({"index": sent.index, "number": "",
                           "message": f"Sceau envoyé le {sent.sent_at:%d/%m/%Y} ({sent.get_channel_display().lower()}) "
                                      "différent de la chaîne : écritures réécrites depuis cet envoi."})
    for period in FiscalPeriod.objects.filter(closing_index__isnull=False):
        recorded = seals.get(period.closing_index, GENESIS if period.closing_index == 0 else None)
        if recorded != period.closing_seal:
            errors.append({"index": period.closing_index, "number": "",
                           "message": f"Sceau de clôture de l'exercice {period.name} non retrouvé dans la chaîne."})
    return {"ok": not errors, "count": count, "errors": errors, "last_index": chain.last_index if chain else 0,
            "last_seal": (chain.last_seal if chain else "") or GENESIS, "checked_at": timezone.now()}


@transaction.atomic
def seal_existing(Transaction, SealChain) -> int:
    """Scelle, dans l'ordre de validation, les écritures validées sans empreinte (reprise de l'existant)."""
    pending = (Transaction.objects.filter(is_validated=True, seal_index__isnull=True)
               .order_by("validated_at", "pk").values_list("pk", flat=True))
    count = 0
    for pk in list(pending):
        seal_transaction(Transaction(pk=pk), Transaction, SealChain)
        count += 1
    return count


# === Envoi hors du logiciel ===

def recipients(settings_obj=None) -> list[str]:
    from .models import AccountingSettings

    raw = (settings_obj or AccountingSettings.get()).seal_recipients
    return [address for address in re.split(r"[\s,;]+", raw or "") if "@" in address]


def anchor(reason: str = "quotidien", force: bool = False) -> list:
    """
    Envoie le dernier maillon de la chaîne hors du logiciel (e-mail, fichier d'archive), s'il a changé
    depuis le dernier envoi (ou si `force`). Renvoie les envois enregistrés ; une erreur d'envoi est
    consignée sans interrompre l'autre canal.
    """
    from . import conf
    from .models import SealAnchor, SealChain

    chain = SealChain.objects.filter(pk=1).first()
    index, value = (chain.last_index, chain.last_seal or GENESIS) if chain else (0, GENESIS)
    last = SealAnchor.objects.filter(error="").order_by("-sent_at", "-pk").first()
    if not force and last is not None and last.index == index:
        return []
    company = conf.company()
    now = timezone.localtime()
    sent = []
    targets = recipients()
    if targets:
        subject = f"[Comptabilité {company['name'] or ''}] Sceau {reason} du {now:%d/%m/%Y} : maillon {index}".replace("  ", " ")
        body = (f"Sceau de la chaîne des écritures validées ({reason}).\n\n"
                f"Société : {company['name'] or '—'} (SIREN {company['siren'] or '—'})\n"
                f"Date : {now:%d/%m/%Y %H:%M}\nMaillon : {index}\nSceau : {value}\n\n"
                "Conservez ce message : il permet de prouver, en le comparant à la chaîne (Comptabilité > Intégrité), "
                "qu'aucune écriture validée jusqu'à ce maillon n'a été modifiée, supprimée ou insérée depuis.\n")
        error = ""
        try:
            send_mail(subject, body, None, targets)
        except Exception as exc:  # noqa: BLE001 - l'échec d'envoi est consigné et signalé
            error = str(exc)[:2000]
            logger.error("Sceau %s : envoi par e-mail impossible : %s", index, exc)
        sent.append(SealAnchor.objects.create(index=index, seal=value, reason=reason, channel="email",
                                              destination=", ".join(targets)[:500], error=error))
    directory = conf.get("SEAL_ARCHIVE_DIR")
    if directory:
        path = Path(directory) / f"sceaux-{company['siren'] or 'societe'}.txt"
        error = ""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as archive:
                archive.write(f"{now.isoformat(timespec='seconds')};{reason};{index};{value}\n")
        except OSError as exc:
            error = str(exc)[:2000]
            logger.error("Sceau %s : archive %s inaccessible : %s", index, path, exc)
        sent.append(SealAnchor.objects.create(index=index, seal=value, reason=reason, channel="file",
                                              destination=str(path)[:500], error=error))
    return sent


def check_anchor(index: int, value: str) -> tuple[bool, str]:
    """Compare un sceau conservé hors du logiciel (rang, empreinte) à la chaîne actuelle."""
    from .models import Transaction

    value = (value or "").strip().lower()
    if index == 0:
        return value == GENESIS, "Chaîne vide à ce rang."
    recorded = Transaction.objects.filter(seal_index=index).values_list("seal", flat=True).first()
    if recorded is None:
        return False, f"Le maillon {index} n'existe plus dans la chaîne : écritures supprimées."
    if recorded != value:
        return False, f"Le maillon {index} ne correspond pas à ce sceau : écritures réécrites depuis."
    result = verify()
    if not result["ok"]:
        return False, "Le sceau correspond, mais la chaîne présente des anomalies (voir ci-dessous)."
    return True, f"Sceau conforme : rien n'a été modifié jusqu'au maillon {index}, et la chaîne est intacte."
