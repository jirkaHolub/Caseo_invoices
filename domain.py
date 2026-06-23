"""Obchodní logika faktur: výpočet DPH, datumy, číslo, concepto, formátování.

Samofakturace (facturación por el destinatario): fakturu vystavuje JMÉNEM majitele
odběratel (Caseo). Vstupní částka je BRUTTO (vč. 21 % IVA).
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Optional, Tuple

TIPO_IVA = 21
TIPO_IRPF = 19  # retención IRPF (srážka daně z příjmu u zdroje, pronájem nemovitostí)
TIPOS_ID = ("NIF", "NIE", "IČ")  # typ daňového ID dodavatele (ES rezident / cizinec / české IČ)
LEYENDA = "Daňový doklad vystavený odběratelem jménem a na účet dodavatele (samofakturace)."

_CENT = Decimal("0.01")
_IVA_FACTOR = Decimal("1.21")  # 1 + 21 %
_IRPF_FACTOR = Decimal("0.19")  # 19 % ze základu daně


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def parse_amount(raw) -> Decimal:
    """Naparsuje uživatelský vstup částky (přijímá '1.234,56' i '1234.56')."""
    if isinstance(raw, Decimal):
        return _q(raw)
    s = str(raw).strip().replace(" ", "").replace(" ", "").replace("€", "")
    if not s:
        raise ValueError("Částka je povinná.")
    # Pokud obsahuje obojí, předpokládáme tečku = tisíce, čárku = desetiny (ES/CZ formát).
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise ValueError("Neplatná částka.")
    if value <= 0:
        raise ValueError("Částka musí být kladná.")
    return _q(value)


def compute_amounts(total) -> Tuple[Decimal, Decimal, Decimal]:
    """Z BRUTTO částky dopočítá (base_imponible, cuota_iva, total).

    base + cuota == total na cent (cuota se dopočítává z totalu).
    """
    total_d = total if isinstance(total, Decimal) else parse_amount(total)
    total_d = _q(total_d)
    base = _q(total_d / _IVA_FACTOR)
    cuota = _q(total_d - base)  # dopočet, ať base + IVA == total přesně
    return base, cuota, total_d


def split_amount(total, parts: int = 2) -> list:
    """Rozdělí BRUTTO částku na `parts` rovnoměrných dílů (na cent přesně).

    Součet dílů == total na cent; případný lichý cent se srovná na prvním dílu,
    takže se nic neztratí (např. 100,01 € na 2 → [50,00; 50,01])."""
    total_d = total if isinstance(total, Decimal) else parse_amount(total)
    total_d = _q(total_d)
    base_part = _q(total_d / Decimal(parts))
    shares = [base_part] * parts
    shares[0] = total_d - base_part * (parts - 1)  # dorovnání na přesný součet
    return shares


def compute_retencion(base) -> Decimal:
    """Srážka daně z příjmu (retención IRPF) = base × 19 %."""
    base_d = base if isinstance(base, Decimal) else Decimal(str(base))
    return _q(base_d * _IRPF_FACTOR)


def compute_liquido(total, base) -> Decimal:
    """Částka k úhradě = brutto total (vč. DPH) − srážka daně z příjmu."""
    total_d = total if isinstance(total, Decimal) else Decimal(str(total))
    return _q(total_d - compute_retencion(base))


def normalize_tipo_id(value) -> str:
    """Vrátí platný typ daňového ID (NIF/NIE/IČ); neznámé/prázdné → 'NIF'."""
    v = (value or "").strip()
    return v if v in TIPOS_ID else "NIF"


# ---------------------------------------------------------------- datumy

def parse_mes(mes: str) -> Tuple[int, int]:
    """'2026-06' -> (2026, 6). Vyhodí ValueError při neplatném vstupu."""
    s = (mes or "").strip()
    try:
        year_s, month_s = s.split("-")
        year, month = int(year_s), int(month_s)
    except (ValueError, AttributeError):
        raise ValueError("Měsíc nájmu musí být ve formátu RRRR-MM (např. 2026-06).")
    if not (1 <= month <= 12) or not (2000 <= year <= 2100):
        raise ValueError("Neplatný měsíc nájmu.")
    return year, month


def last_day_of_month(year: int, month: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last)


def fifteenth_next_month(year: int, month: int) -> date:
    if month == 12:
        return date(year + 1, 1, 15)
    return date(year, month + 1, 15)


def default_dates(mes: str) -> Tuple[date, date]:
    """fecha_expedicion = poslední den měsíce nájmu; fecha_vencimiento = 15. násl. měsíce."""
    year, month = parse_mes(mes)
    return last_day_of_month(year, month), fifteenth_next_month(year, month)


# ---------------------------------------------------------------- concepto

# Názvy měsíců slovně (čeština) – pro popis faktury "Nájemné za měsíc <měsíc> <rok>".
_MESICE_CS = ("leden", "únor", "březen", "duben", "květen", "červen",
              "červenec", "srpen", "září", "říjen", "listopad", "prosinec")


def default_concepto(nombre_propiedad: Optional[str], mes: str) -> str:
    """Výchozí popis: "Nájemné za měsíc <měsíc slovně> <rok>" (např. červen 2026)."""
    year, month = parse_mes(mes)
    return "Nájemné za měsíc {} {}".format(_MESICE_CS[month - 1], year)


# ---------------------------------------------------------------- číslo faktury

def format_numero(owner_kod: str, anio: int, seq: int) -> str:
    return "{}-{}-{:04d}".format(owner_kod, anio, seq)


# ---------------------------------------------------------------- formátování (ES)

def format_eur(value) -> str:
    """1234.5 -> '1.234,50 €' (španělský formát: tečka = tisíce, čárka = desetiny)."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    d = _q(d)
    neg = d < 0
    d = abs(d)
    entero, _, dec = "{:.2f}".format(d).partition(".")
    grupos = []
    while len(entero) > 3:
        grupos.insert(0, entero[-3:])
        entero = entero[:-3]
    grupos.insert(0, entero)
    miles = ".".join(grupos)
    return "{}{},{} €".format("-" if neg else "", miles, dec)


def format_number_es(value) -> str:
    """Jako format_eur, ale bez symbolu měny (pro CSV/náhled)."""
    return format_eur(value).replace(" €", "")


def format_date(value) -> str:
    """ISO 'YYYY-MM-DD' nebo date -> 'dd/mm/aaaa'."""
    if isinstance(value, date):
        d = value
    else:
        d = date.fromisoformat(str(value))
    return d.strftime("%d/%m/%Y")
