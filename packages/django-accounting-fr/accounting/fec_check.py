"""
Contrôle d'un fichier des écritures comptables (FEC) avant sa remise à l'administration.

Applique les règles de structure de l'article A47 A-1 du LPF et du BOI-CF-IOR-60-40-20
(celles que vérifie l'outil « Test Compta Demat » de la DGFiP) à un fichier, quel que soit
le logiciel qui l'a produit. Il ne remplace pas cet outil officiel, ni la revue du
contenu par l'expert-comptable.

    from accounting.fec_check import check_fec
    report = check_fec(open("123456789FEC20261231.txt", "rb").read(), "123456789FEC20261231.txt")
    report.errors, report.warnings, report.stats
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

COLUMNS = [
    "JournalCode", "JournalLib", "EcritureNum", "EcritureDate", "CompteNum", "CompteLib", "CompAuxNum",
    "CompAuxLib", "PieceRef", "PieceDate", "EcritureLib", "Debit", "Credit", "EcritureLet", "DateLet",
    "ValidDate", "Montantdevise", "Idevise",
]
REQUIRED = ["JournalCode", "JournalLib", "EcritureNum", "EcritureDate", "CompteNum", "CompteLib", "PieceRef",
            "PieceDate", "EcritureLib", "Debit", "Credit", "ValidDate"]
FILENAME = re.compile(r"^(?P<siren>\d{9})FEC(?P<closing>\d{8})\.(txt|xml)$", re.I)
AMOUNT = re.compile(r"^-?\d+([.,]\d{1,2})?$")
MAX_REPORTED = 50  # au-delà, les anomalies d'un même type sont seulement comptées


@dataclass
class Report:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    _counts: dict = field(default_factory=lambda: defaultdict(int))

    @property
    def ok(self):
        return not self.errors

    def add(self, level, code, message):
        self._counts[(level, code)] += 1
        if self._counts[(level, code)] <= MAX_REPORTED:
            (self.errors if level == "error" else self.warnings).append(message)

    def summary(self):
        """Nombre d'anomalies par type, y compris celles non détaillées."""
        return {f"{level}:{code}": n for (level, code), n in sorted(self._counts.items())}


def _decode(content: bytes):
    for encoding in ("utf-8", "iso-8859-15"):
        try:
            return content.decode(encoding).lstrip("﻿"), encoding
        except UnicodeDecodeError:
            continue
    return content.decode("iso-8859-15", errors="replace"), "inconnu"


def _date(value):
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def _amount(value):
    if not AMOUNT.match(value or ""):
        return None
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation:
        return None


def check_fec(content: bytes, filename: str = "", period_start: date | None = None,
              period_end: date | None = None) -> Report:
    report = Report()
    match = FILENAME.match(filename or "")
    closing = None
    if filename and not match:
        report.add("error", "nom", f"Nom de fichier « {filename} » : attendu <SIREN>FEC<AAAAMMJJ>.txt (SIREN sur 9 chiffres).")
    elif match:
        closing = _date(match["closing"])
        if match["siren"] == "000000000":
            report.add("error", "siren", "SIREN absent du nom de fichier (000000000) : renseigner le SIREN de la société.")
        if closing is None:
            report.add("error", "nom", "Date de clôture du nom de fichier invalide.")
    period_end = period_end or closing

    text, encoding = _decode(content)
    report.stats["encodage"] = encoding
    lines = text.splitlines()
    if not lines:
        report.add("error", "vide", "Fichier vide.")
        return report

    header = lines[0]
    separator = "\t" if "\t" in header else "|" if "|" in header else None
    if separator is None:
        report.add("error", "separateur", "Séparateur de zones introuvable : tabulation ou barre verticale « | » attendue.")
        return report
    names = header.split(separator)
    if names != COLUMNS:
        missing = [c for c in COLUMNS if c not in names]
        report.add("error", "entete", "En-tête non conforme : les 18 zones doivent être, dans l'ordre : "
                   + ", ".join(COLUMNS) + (f" (absentes : {', '.join(missing)})" if missing else ""))
        return report

    entries = defaultdict(lambda: {"debit": Decimal("0"), "credit": Decimal("0"), "dates": set(), "lines": []})
    journals_labels, accounts_labels = defaultdict(set), defaultdict(set)
    total_debit = total_credit = Decimal("0")
    first_date = last_date = None

    for number, raw in enumerate(lines[1:], start=2):
        if not raw.strip():
            continue
        values = raw.split(separator)
        where = f"ligne {number}"
        if len(values) != len(COLUMNS):
            report.add("error", "zones", f"{where} : {len(values)} zones au lieu de 18 (séparateur dans un libellé ?).")
            continue
        row = dict(zip(COLUMNS, (v.strip() for v in values)))
        for name in REQUIRED:
            if not row[name]:
                report.add("error", f"vide:{name}", f"{where} : zone {name} vide (obligatoire).")

        ecriture_date, piece_date, valid_date = _date(row["EcritureDate"]), _date(row["PieceDate"]), _date(row["ValidDate"])
        for name, value in (("EcritureDate", ecriture_date), ("PieceDate", piece_date), ("ValidDate", valid_date)):
            if row[name] and value is None:
                report.add("error", f"date:{name}", f"{where} : {name} « {row[name]} » n'est pas une date AAAAMMJJ.")
        if ecriture_date and valid_date and valid_date < ecriture_date:
            report.add("warning", "validation", f"{where} : écriture validée ({row['ValidDate']}) avant sa date ({row['EcritureDate']}).")
        if ecriture_date:
            first_date = min(first_date or ecriture_date, ecriture_date)
            last_date = max(last_date or ecriture_date, ecriture_date)
            if period_end and ecriture_date > period_end:
                report.add("error", "periode", f"{where} : écriture du {row['EcritureDate']} après la clôture de l'exercice.")
            if period_start and ecriture_date < period_start:
                report.add("error", "periode", f"{where} : écriture du {row['EcritureDate']} avant l'ouverture de l'exercice.")

        debit, credit = _amount(row["Debit"]), _amount(row["Credit"])
        if debit is None or credit is None:
            report.add("error", "montant", f"{where} : montants « {row['Debit']} » / « {row['Credit']} » invalides "
                                           "(chiffres, virgule décimale, sans séparateur de milliers).")
            debit, credit = debit or Decimal("0"), credit or Decimal("0")
        elif debit and credit:
            report.add("warning", "sens", f"{where} : débit et crédit renseignés sur la même ligne.")
        elif not debit and not credit:
            report.add("warning", "nul", f"{where} : ligne à montant nul.")
        if debit < 0 or credit < 0:
            report.add("warning", "negatif", f"{where} : montant négatif (préférer le sens inverse).")

        if bool(row["CompAuxNum"]) != bool(row["CompAuxLib"]):
            report.add("error", "auxiliaire", f"{where} : CompAuxNum et CompAuxLib vont ensemble.")
        if bool(row["EcritureLet"]) != bool(row["DateLet"]):
            report.add("error", "lettrage", f"{where} : EcritureLet et DateLet vont ensemble.")
        if row["DateLet"] and _date(row["DateLet"]) is None:
            report.add("error", "date:DateLet", f"{where} : DateLet invalide.")
        if bool(row["Montantdevise"]) != bool(row["Idevise"]):
            report.add("error", "devise", f"{where} : Montantdevise et Idevise vont ensemble.")
        if row["CompteNum"] and not re.match(r"^[1-8]\w{2,}$", row["CompteNum"]):
            report.add("warning", "compte", f"{where} : compte « {row['CompteNum']} » hors des classes 1 à 8 du plan comptable.")

        journals_labels[row["JournalCode"]].add(row["JournalLib"])
        accounts_labels[row["CompteNum"]].add(row["CompteLib"])
        entry = entries[(row["JournalCode"], row["EcritureNum"])]
        entry["debit"] += debit
        entry["credit"] += credit
        entry["dates"].add(row["EcritureDate"])
        entry["lines"].append(number)
        total_debit += debit
        total_credit += credit

    for (journal, number), entry in entries.items():
        if entry["debit"] != entry["credit"]:
            report.add("error", "equilibre", f"Écriture {journal} {number} (lignes {entry['lines'][0]}…) déséquilibrée : "
                                             f"débit {entry['debit']} / crédit {entry['credit']}.")
        if len(entry["dates"]) > 1:
            report.add("error", "dates_ecriture", f"Écriture {journal} {number} : plusieurs dates ({', '.join(sorted(entry['dates']))}).")
        if len(entry["lines"]) < 2:
            report.add("warning", "ligne_unique", f"Écriture {journal} {number} : une seule ligne.")
        if entry["lines"] != list(range(entry["lines"][0], entry["lines"][-1] + 1)):
            report.add("warning", "dispersee", f"Écriture {journal} {number} : lignes non contiguës dans le fichier.")
    if total_debit != total_credit:
        report.add("error", "total", f"Total du fichier déséquilibré : débit {total_debit} / crédit {total_credit}.")
    for journal, labels in journals_labels.items():
        if len(labels) > 1:
            report.add("warning", "journal_lib", f"Journal {journal} : plusieurs libellés ({', '.join(sorted(labels))}).")
    for account, labels in accounts_labels.items():
        if len(labels) > 1:
            report.add("warning", "compte_lib", f"Compte {account} : plusieurs libellés ({', '.join(sorted(labels))}).")

    _check_numbering(entries, report)
    report.stats.update({
        "lignes": sum(len(e["lines"]) for e in entries.values()), "ecritures": len(entries),
        "journaux": sorted(journals_labels), "comptes": len(accounts_labels),
        "total_debit": f"{total_debit:.2f}", "total_credit": f"{total_credit:.2f}",
        "premiere_date": f"{first_date:%d/%m/%Y}" if first_date else "", "derniere_date": f"{last_date:%d/%m/%Y}" if last_date else "",
        "separateur": "tabulation" if separator == "\t" else "|",
    })
    return report


def _check_numbering(entries, report):
    """Numérotation continue et chronologique par journal (numéros de la forme PRÉFIXE-00001)."""
    by_journal = defaultdict(list)
    for (journal, number), entry in entries.items():
        match = re.search(r"(\d+)$", number)
        if match:
            by_journal[(journal, number[:match.start()])].append((int(match.group(1)), min(entry["dates"]), number))
    for (journal, _prefix), items in by_journal.items():
        items.sort()
        numbers = [n for n, _, _ in items]
        gaps = sorted(set(range(numbers[0], numbers[-1] + 1)) - set(numbers))
        if gaps:
            shown = ", ".join(str(g) for g in gaps[:10]) + ("…" if len(gaps) > 10 else "")
            report.add("error", "trou", f"Journal {journal} : numérotation discontinue (numéros manquants : {shown}).")
        for (_, day, number), (_, previous_day, previous) in zip(items[1:], items):
            if day < previous_day:
                report.add("warning", "chronologie", f"Journal {journal} : {number} daté du {day} après {previous} daté du "
                                                     f"{previous_day} (numérotation non chronologique).")
