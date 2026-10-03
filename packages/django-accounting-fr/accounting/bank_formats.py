"""
Lecture des relevés bancaires : CSV (export des banques en ligne), OFX (1.x et 2.x),
CAMT.053 (ISO 20022) et CFONB 120 (format interbancaire français).

`parse(contenu, nom_de_fichier, mapping=None)` renvoie un `Statement` : opérations (montant
positif = crédit sur le relevé, c'est-à-dire encaissement), soldes et période quand le fichier
les donne. Lève `StatementError` si le fichier est illisible ; `ColumnsError` (CSV) si les
colonnes ne sont pas reconnues : elle porte les en-têtes, pour demander la correspondance.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

CENT = Decimal("0.01")


class StatementError(ValueError):
    """Relevé illisible ou vide."""


class ColumnsError(StatementError):
    """CSV dont les colonnes date / libellé / montant ne sont pas reconnues."""

    def __init__(self, message, headers):
        super().__init__(message)
        self.headers = headers


@dataclass
class Line:
    date: date
    label: str
    amount: Decimal
    value_date: date | None = None
    reference: str = ""
    uid: str = ""  # identifiant unique donné par la banque (FITID, AcctSvcrRef…)


@dataclass
class Statement:
    file_format: str
    lines: list[Line] = field(default_factory=list)
    opening_balance: Decimal | None = None
    closing_balance: Decimal | None = None
    date_start: date | None = None
    date_end: date | None = None
    account_number: str = ""

    def finish(self):
        if not self.lines:
            raise StatementError("Aucune opération trouvée dans le fichier.")
        dates = [line.date for line in self.lines]
        self.date_start = min(self.date_start or min(dates), min(dates))
        self.date_end = max(self.date_end or max(dates), max(dates))
        return self


def _text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _norm(value: str) -> str:
    """Minuscules, sans accents ni ponctuation (comparaison des en-têtes)."""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def amount(value) -> Decimal | None:
    """« 1 234,56 », « -12.30 », « 1.234,56 € », « (12,00) », « +5 » -> Decimal ; None si illisible."""
    text = str(value or "").strip().replace(" ", "").replace(" ", "").replace(" ", "")
    text = text.replace("€", "").replace("EUR", "").replace("eur", "")
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")") or text.endswith("-")
    text = text.strip("()").rstrip("-")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        result = Decimal(text)
    except InvalidOperation:
        return None
    return (-result if negative else result).quantize(CENT)


def parse_date(value) -> date | None:
    text = str(value or "").strip()[:19]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%Y%m%d", "%Y-%m-%dT%H:%M:%S", "%d-%m-%y"):
        try:
            return datetime.strptime(text if "T" in fmt else text[:10], fmt).date()
        except ValueError:
            continue
    return None


# === CSV ===

COLUMNS = {  # rôle -> débuts d'en-têtes reconnus (normalisés)
    "value_date": ["date valeur", "date de valeur", "value date"],
    "date": ["date operation", "date de l operation", "date comptable", "date", "booking date", "jour"],
    "label": ["libelle", "description", "intitule", "label", "detail", "nature", "motif", "operation", "wording"],
    "reference": ["reference", "ref", "numero", "n operation"],
    "debit": ["debit", "montant debit", "sortie", "depense"],
    "credit": ["credit", "montant credit", "entree", "recette"],
    "amount": ["montant", "amount", "somme", "valeur eur"],
}
ROLES = ["date", "value_date", "label", "reference", "amount", "debit", "credit"]


def _detect(headers) -> dict:
    mapping, taken = {}, set()
    normalized = [_norm(h) for h in headers]
    for role in ("value_date", "date", "debit", "credit", "amount", "reference", "label"):
        for index, header in enumerate(normalized):
            if index in taken or not header:
                continue
            # « date » : en-tête exact ou suivi d'un mot (« date opération ») ; les autres : début de l'en-tête
            if any(header == key or header.startswith(key + " ") or (role != "date" and header.startswith(key))
                   for key in COLUMNS[role]):
                mapping[role] = index
                taken.add(index)
                break
    return mapping


def _usable(mapping) -> bool:
    return "date" in mapping and "label" in mapping and ("amount" in mapping or "debit" in mapping or "credit" in mapping)


def parse_csv(raw: bytes, mapping: dict | None = None) -> Statement:
    text = _text(raw)
    sample = "\n".join(text.splitlines()[:20])
    delimiter = max([";", ",", "\t", "|"], key=sample.count)
    rows = [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]
    header_index, detected = None, {}
    for index, row in enumerate(rows[:30]):
        found = _detect(row)
        if _usable(found):
            header_index, detected = index, found
            break
    if mapping:
        detected = {role: int(col) for role, col in mapping.items() if str(col).strip() != ""}
        if header_index is None:
            header_index = next((i for i, row in enumerate(rows[:30]) if len([c for c in row if c.strip()]) >= 3), 0)
    if header_index is None or not _usable(detected):
        headers = next((row for row in rows[:30] if len([c for c in row if c.strip()]) >= 3), rows[0] if rows else [])
        raise ColumnsError("Colonnes non reconnues : indiquez la date, le libellé et le montant (ou débit et crédit).",
                           headers)
    statement = Statement("csv")

    def cell(row, role):
        index = detected.get(role)
        return row[index] if index is not None and index < len(row) else ""

    for row in rows[header_index + 1:]:
        day = parse_date(cell(row, "date"))
        if day is None:
            continue
        if "amount" in detected:
            value = amount(cell(row, "amount"))
        else:
            debit, credit = amount(cell(row, "debit")), amount(cell(row, "credit"))
            if debit is None and credit is None:
                value = None
            else:
                value = (credit or Decimal("0")).copy_abs() - (debit or Decimal("0")).copy_abs()
        if value is None or not value:
            continue
        statement.lines.append(Line(day, _clean(cell(row, "label"))[:255] or "(sans libellé)", value,
                                    value_date=parse_date(cell(row, "value_date")),
                                    reference=_clean(cell(row, "reference"))[:120]))
    return statement.finish()


def csv_headers(raw: bytes) -> list[str]:
    text = _text(raw)
    delimiter = max([";", ",", "\t", "|"], key="\n".join(text.splitlines()[:20]).count)
    for row in list(csv.reader(io.StringIO(text), delimiter=delimiter))[:30]:
        if len([c for c in row if c.strip()]) >= 3:
            return row
    return []


# === OFX ===

def _ofx_value(block: str, tag: str) -> str:
    match = re.search(rf"<{tag}>([^<\r\n]*)", block, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def parse_ofx(raw: bytes) -> Statement:
    text = _text(raw)
    statement = Statement("ofx", account_number=_ofx_value(text, "ACCTID"))
    for block in re.findall(r"<STMTTRN>(.*?)</STMTTRN>", text, re.IGNORECASE | re.DOTALL):
        day = parse_date(_ofx_value(block, "DTPOSTED")[:8])
        value = amount(_ofx_value(block, "TRNAMT"))
        if day is None or value is None or not value:
            continue
        name, memo = _ofx_value(block, "NAME"), _ofx_value(block, "MEMO")
        label = name if not memo or memo in name else f"{name} {memo}".strip()
        statement.lines.append(Line(day, _clean(label)[:255] or "(sans libellé)", value,
                                    value_date=parse_date(_ofx_value(block, "DTUSER")[:8]),
                                    reference=_clean(_ofx_value(block, "CHECKNUM") or _ofx_value(block, "REFNUM"))[:120],
                                    uid=_ofx_value(block, "FITID")[:80]))
    ledger = re.search(r"<LEDGERBAL>(.*?)(</LEDGERBAL>|<AVAILBAL>|$)", text, re.IGNORECASE | re.DOTALL)
    if ledger:
        statement.closing_balance = amount(_ofx_value(ledger.group(1), "BALAMT"))
    statement.date_start = parse_date(_ofx_value(text, "DTSTART")[:8])
    statement.date_end = parse_date(_ofx_value(text, "DTEND")[:8])
    return statement.finish()


# === CAMT.053 ===

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(node, path):
    """Recherche par noms locaux (sans espaces de noms) : « Bal/Tp/CdOrPrtry/Cd »."""
    current = [node]
    for name in path.split("/"):
        current = [child for parent in current for child in parent if _local(child.tag) == name]
        if not current:
            return None
    return current[0]


def _findall(node, name):
    return [child for child in node.iter() if _local(child.tag) == name]


def _xml_text(node, path):
    found = _find(node, path)
    return (found.text or "").strip() if found is not None else ""


def parse_camt(raw: bytes) -> Statement:
    head = raw[:2000].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in raw.upper():
        raise StatementError("Fichier XML refusé (déclarations DOCTYPE ou ENTITY).")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise StatementError(f"XML illisible : {exc}") from exc
    statement = Statement("camt053")
    for stmt in _findall(root, "Stmt"):
        statement.account_number = statement.account_number or _xml_text(stmt, "Acct/Id/IBAN")
        for bal in [c for c in stmt if _local(c.tag) == "Bal"]:
            code = _xml_text(bal, "Tp/CdOrPrtry/Cd")
            value = amount(_xml_text(bal, "Amt"))
            if value is not None and _xml_text(bal, "CdtDbtInd") == "DBIT":
                value = -value
            day = parse_date(_xml_text(bal, "Dt/Dt") or _xml_text(bal, "Dt/DtTm"))
            if code in ("OPBD", "PRCD") and statement.opening_balance is None:
                statement.opening_balance, statement.date_start = value, day
            elif code == "CLBD":
                statement.closing_balance, statement.date_end = value, day
        for entry in [c for c in stmt if _local(c.tag) == "Ntry"]:
            status = _xml_text(entry, "Sts") or _xml_text(entry, "Sts/Cd")
            if status and status not in ("BOOK",):
                continue
            value = amount(_xml_text(entry, "Amt"))
            day = parse_date(_xml_text(entry, "BookgDt/Dt") or _xml_text(entry, "BookgDt/DtTm"))
            if value is None or day is None or not value:
                continue
            if _xml_text(entry, "CdtDbtInd") == "DBIT":
                value = -value
            parts = []
            for name in ("Nm",):
                for party in _findall(entry, "RltdPties"):
                    for role in ("Dbtr", "Cdtr", "Dbtr/Pty", "Cdtr/Pty"):
                        text = _xml_text(party, f"{role}/{name}")
                        if text and text not in parts:
                            parts.append(text)
            parts += [n.text.strip() for n in _findall(entry, "Ustrd") if n.text and n.text.strip()]
            info = _xml_text(entry, "AddtlNtryInf")
            if info and info not in parts:
                parts.append(info)
            statement.lines.append(Line(day, _clean(" ".join(parts))[:255] or "(sans libellé)", value,
                                        value_date=parse_date(_xml_text(entry, "ValDt/Dt")),
                                        reference=_clean(_xml_text(entry, "NtryRef")
                                                         or _xml_text(entry, "NtryDtls/TxDtls/Refs/EndToEndId"))[:120],
                                        uid=_xml_text(entry, "AcctSvcrRef")[:80]))
    return statement.finish()


# === CFONB 120 ===

_OVERPUNCH = {"{": (0, 1), "}": (0, -1), **{chr(65 + i): (i + 1, 1) for i in range(9)},
              **{chr(74 + i): (i + 1, -1) for i in range(9)}}


def _cfonb_amount(field_: str, decimals: str) -> Decimal | None:
    field_ = field_.strip()
    if not field_:
        return None
    last = field_[-1]
    if last.isdigit():
        digits, sign = field_, 1
    elif last in _OVERPUNCH:
        digit, sign = _OVERPUNCH[last]
        digits = field_[:-1] + str(digit)
    else:
        return None
    if not digits.isdigit():
        return None
    places = int(decimals) if decimals.isdigit() else 2
    return (sign * Decimal(int(digits)) / (10 ** places)).quantize(CENT)


def _cfonb_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%d%m%y").date()
    except ValueError:
        return None


def parse_cfonb(raw: bytes) -> Statement:
    text = _text(raw).replace("\r", "")
    records = [r for r in text.split("\n") if r.strip()]
    if len(records) == 1 and len(records[0]) > 120:
        records = [records[0][i:i + 120] for i in range(0, len(records[0]), 120)]
    statement = Statement("cfonb120")
    for record in records:
        record = record.ljust(120)
        code = record[:2]
        if code == "01" and statement.opening_balance is None:
            statement.opening_balance = _cfonb_amount(record[90:104], record[19])
            statement.date_start = _cfonb_date(record[34:40])
            statement.account_number = record[21:32].strip()
        elif code == "04":
            day = _cfonb_date(record[34:40])
            value = _cfonb_amount(record[90:104], record[19])
            if day is None or value is None or not value:
                continue
            statement.lines.append(Line(day, _clean(record[48:79]) or "(sans libellé)", value,
                                        value_date=_cfonb_date(record[42:48]), reference=_clean(record[104:120]),
                                        uid=f"{record[34:40]}{record[81:88].strip()}" if record[81:88].strip() else ""))
        elif code == "05" and statement.lines:
            extra = _clean(record[48:118])
            if extra:
                last = statement.lines[-1]
                last.label = f"{last.label} {extra}"[:255]
        elif code == "07":
            statement.closing_balance = _cfonb_amount(record[90:104], record[19])
            statement.date_end = _cfonb_date(record[34:40])
    return statement.finish()


# === Aiguillage ===

def detect_format(raw: bytes, filename: str = "") -> str:
    head = raw[:4000].lstrip(b"\xef\xbb\xbf").lstrip()
    upper = head.upper()
    if b"OFXHEADER" in upper or b"<OFX>" in upper:
        return "ofx"
    if head.startswith(b"<") and (b"CAMT.053" in upper or b"BKTOCSTMRSTMT" in upper):
        return "camt053"
    first = head.split(b"\n", 1)[0].rstrip(b"\r")
    if first[:2] == b"01" and (len(first) == 120 or len(first) > 240 and len(first) % 120 == 0):
        return "cfonb120"
    if filename.lower().endswith(".ofx") or filename.lower().endswith(".qfx"):
        return "ofx"
    if filename.lower().endswith(".xml"):
        return "camt053"
    return "csv"


def parse(raw: bytes, filename: str = "", mapping: dict | None = None) -> Statement:
    file_format = detect_format(raw, filename)
    parser = {"ofx": parse_ofx, "camt053": parse_camt, "cfonb120": parse_cfonb}.get(file_format)
    if parser:
        return parser(raw)
    return parse_csv(raw, mapping)
