"""Vrstva pro přístup k databázi.

Duální backend:
  - lokálně (bez DATABASE_URL) → SQLite soubor `data.db`
  - v cloudu (DATABASE_URL nastaveno) → PostgreSQL / Supabase

Tabulky: owners, settings, invoices, counters.
Faktury jsou immutable (po vystavení se needitují). Čísla se přidělují atomicky.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Optional

# Connection string: vlastní DATABASE_URL, jinak POSTGRES_URL (Supabase↔Vercel
# integrace ho nastavuje automaticky – pooler, vhodný pro serverless).
DATABASE_URL = (os.environ.get("DATABASE_URL")
                or os.environ.get("POSTGRES_URL") or "").strip()
USE_PG = bool(DATABASE_URL)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")

if USE_PG:
    import psycopg
    from psycopg.rows import dict_row


class DuplicateKod(Exception):
    """Majitel s daným kódem už existuje (porušení UNIQUE) – nezávisle na backendu."""


def _is_unique_violation(exc) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return "unique" in str(exc).lower()
    if USE_PG and isinstance(exc, psycopg.errors.UniqueViolation):
        return True
    return False


def get_conn():
    """Otevře nové připojení (per request). U obou backendů řádky podporují row["col"]."""
    if USE_PG:
        url = DATABASE_URL
        if "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        # prepare_threshold=None kvůli kompatibilitě s pgbouncer (Supabase pooler).
        return psycopg.connect(url, prepare_threshold=None, row_factory=dict_row)
    # Na serverless (Vercel/Lambda) je disk read-only → SQLite nelze použít.
    if os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        raise RuntimeError(
            "Chybí DATABASE_URL (ani POSTGRES_URL) – serverless běh nemůže použít "
            "SQLite. Nastav DATABASE_URL v Environment Variables na Vercelu (pro "
            "Production) a spusť nový Deploy.")
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ex(conn, sql, params=()):
    """Provede dotaz. SQL píšeme s `?`; pro Postgres se převede na `%s`."""
    if USE_PG:
        sql = sql.replace("?", "%s")
    return conn.execute(sql, params)


# ---------------------------------------------------------------- schéma

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS owners (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kod               TEXT NOT NULL UNIQUE,
    nombre            TEXT NOT NULL,
    nif               TEXT NOT NULL,
    domicilio         TEXT NOT NULL,
    email             TEXT,
    nombre_propiedad  TEXT,
    variabilni_symbol TEXT,
    tipo_id           TEXT NOT NULL DEFAULT 'NIF',
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    razon     TEXT NOT NULL DEFAULT '',
    nif       TEXT NOT NULL DEFAULT '',
    domicilio TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invoices (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    numero            TEXT NOT NULL UNIQUE,
    owner_kod         TEXT NOT NULL,
    fecha_expedicion  TEXT NOT NULL,
    fecha_vencimiento TEXT NOT NULL,
    mes_najmu         TEXT NOT NULL,
    concepto          TEXT NOT NULL,
    base_imponible    REAL NOT NULL,
    tipo_iva          INTEGER NOT NULL,
    cuota_iva         REAL NOT NULL,
    total             REAL NOT NULL,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (owner_kod) REFERENCES owners(kod)
);
CREATE TABLE IF NOT EXISTS counters (
    owner_kod TEXT NOT NULL,
    anio      INTEGER NOT NULL,
    last_seq  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner_kod, anio)
);
"""

# Stejné schéma pro PostgreSQL (viz též schema.sql pro ruční spuštění v Supabase).
_PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS owners (
        id                SERIAL PRIMARY KEY,
        kod               TEXT NOT NULL UNIQUE,
        nombre            TEXT NOT NULL,
        nif               TEXT NOT NULL,
        domicilio         TEXT NOT NULL,
        email             TEXT,
        nombre_propiedad  TEXT,
        variabilni_symbol TEXT,
        tipo_id           TEXT NOT NULL DEFAULT 'NIF',
        created_at        TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS settings (
        id        INTEGER PRIMARY KEY CHECK (id = 1),
        razon     TEXT NOT NULL DEFAULT '',
        nif       TEXT NOT NULL DEFAULT '',
        domicilio TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS invoices (
        id                SERIAL PRIMARY KEY,
        numero            TEXT NOT NULL UNIQUE,
        owner_kod         TEXT NOT NULL REFERENCES owners(kod),
        fecha_expedicion  TEXT NOT NULL,
        fecha_vencimiento TEXT NOT NULL,
        mes_najmu         TEXT NOT NULL,
        concepto          TEXT NOT NULL,
        base_imponible    NUMERIC(12,2) NOT NULL,
        tipo_iva          INTEGER NOT NULL,
        cuota_iva         NUMERIC(12,2) NOT NULL,
        total             NUMERIC(12,2) NOT NULL,
        created_at        TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS counters (
        owner_kod TEXT NOT NULL,
        anio      INTEGER NOT NULL,
        last_seq  INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (owner_kod, anio)
    )""",
    "ALTER TABLE owners ADD COLUMN IF NOT EXISTS variabilni_symbol TEXT",
    "ALTER TABLE owners ADD COLUMN IF NOT EXISTS tipo_id TEXT NOT NULL DEFAULT 'NIF'",
]


def init_db() -> None:
    conn = get_conn()
    try:
        if USE_PG:
            for stmt in _PG_SCHEMA:
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO settings (id, razon, nif, domicilio) "
                "VALUES (1, 'Caseo', '', '') ON CONFLICT (id) DO NOTHING"
            )
        else:
            conn.executescript(_SQLITE_SCHEMA)
            # Migrace pro starší SQLite databáze.
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(owners)").fetchall()]
            if "variabilni_symbol" not in cols:
                conn.execute("ALTER TABLE owners ADD COLUMN variabilni_symbol TEXT")
            if "tipo_id" not in cols:
                conn.execute("ALTER TABLE owners ADD COLUMN tipo_id TEXT NOT NULL DEFAULT 'NIF'")
            conn.execute(
                "INSERT OR IGNORE INTO settings (id, razon, nif, domicilio) "
                "VALUES (1, 'Caseo', '', '')"
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- owners

def list_owners() -> list:
    conn = get_conn()
    try:
        return _ex(conn, "SELECT * FROM owners ORDER BY kod").fetchall()
    finally:
        conn.close()


def get_owner(kod: str):
    conn = get_conn()
    try:
        return _ex(conn, "SELECT * FROM owners WHERE kod = ?", (kod,)).fetchone()
    finally:
        conn.close()


def create_owner(kod, nombre, nif, domicilio, email, nombre_propiedad,
                 variabilni_symbol, tipo_id="NIF") -> None:
    conn = get_conn()
    try:
        _ex(conn,
            "INSERT INTO owners (kod, nombre, nif, domicilio, email, nombre_propiedad, "
            "variabilni_symbol, tipo_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kod, nombre, nif, domicilio, email or None, nombre_propiedad or None,
             variabilni_symbol or None, tipo_id,
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        if _is_unique_violation(exc):
            raise DuplicateKod(kod)
        raise
    finally:
        conn.close()


def update_owner(kod, nombre, nif, domicilio, email, nombre_propiedad,
                 variabilni_symbol, tipo_id="NIF") -> None:
    """Aktualizuje fiskální údaje majitele. Kód (kod) se nemění – je klíčem ve fakturách."""
    conn = get_conn()
    try:
        _ex(conn,
            "UPDATE owners SET nombre = ?, nif = ?, domicilio = ?, email = ?, "
            "nombre_propiedad = ?, variabilni_symbol = ?, tipo_id = ? WHERE kod = ?",
            (nombre, nif, domicilio, email or None, nombre_propiedad or None,
             variabilni_symbol or None, tipo_id, kod))
        conn.commit()
    finally:
        conn.close()


def delete_owner(kod: str) -> None:
    conn = get_conn()
    try:
        _ex(conn, "DELETE FROM owners WHERE kod = ?", (kod,))
        conn.commit()
    finally:
        conn.close()


def owner_invoice_count(kod: str) -> int:
    conn = get_conn()
    try:
        row = _ex(conn, "SELECT COUNT(*) AS c FROM invoices WHERE owner_kod = ?", (kod,)).fetchone()
        return row["c"]
    finally:
        conn.close()


# ---------------------------------------------------------------- settings

def get_settings():
    conn = get_conn()
    try:
        return _ex(conn, "SELECT * FROM settings WHERE id = 1").fetchone()
    finally:
        conn.close()


def update_settings(razon, nif, domicilio) -> None:
    conn = get_conn()
    try:
        _ex(conn, "UPDATE settings SET razon = ?, nif = ?, domicilio = ? WHERE id = 1",
            (razon, nif, domicilio))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- invoices

def get_invoice(numero: str):
    conn = get_conn()
    try:
        return _ex(conn,
            "SELECT i.*, o.nombre AS owner_nombre, o.nif AS owner_nif, "
            "o.domicilio AS owner_domicilio, o.nombre_propiedad AS owner_propiedad, "
            "o.email AS owner_email, o.variabilni_symbol AS owner_vs, "
            "o.tipo_id AS owner_tipo_id "
            "FROM invoices i LEFT JOIN owners o ON o.kod = i.owner_kod "
            "WHERE i.numero = ?", (numero,)).fetchone()
    finally:
        conn.close()


def list_invoices(owner_kod: Optional[str] = None, anio: Optional[int] = None) -> list:
    sql = (
        "SELECT i.*, o.nombre AS owner_nombre "
        "FROM invoices i LEFT JOIN owners o ON o.kod = i.owner_kod WHERE 1 = 1"
    )
    params: list = []
    if owner_kod:
        sql += " AND i.owner_kod = ?"
        params.append(owner_kod)
    if anio:
        sql += " AND substr(i.fecha_expedicion, 1, 4) = ?"
        params.append(str(anio))
    sql += " ORDER BY i.fecha_expedicion DESC, i.numero DESC"
    conn = get_conn()
    try:
        return _ex(conn, sql, params).fetchall()
    finally:
        conn.close()


def invoice_exists_for_month(owner_kod: str, mes_najmu: str):
    """Vrátí existující fakturu pro daného majitele a měsíc nájmu, nebo None."""
    conn = get_conn()
    try:
        return _ex(conn,
            "SELECT * FROM invoices WHERE owner_kod = ? AND mes_najmu = ? "
            "ORDER BY numero LIMIT 1", (owner_kod, mes_najmu)).fetchone()
    finally:
        conn.close()


def peek_next_seq(owner_kod: str, anio: int) -> int:
    """Předběžné pořadové číslo (NESPOTŘEBUJE counter) – jen pro náhled."""
    conn = get_conn()
    try:
        row = _ex(conn, "SELECT last_seq FROM counters WHERE owner_kod = ? AND anio = ?",
                  (owner_kod, anio)).fetchone()
        return (row["last_seq"] if row else 0) + 1
    finally:
        conn.close()


def create_invoice_atomic(
    owner_kod, anio, fecha_expedicion, fecha_vencimiento, mes_najmu,
    concepto, base_imponible, tipo_iva, cuota_iva, total,
) -> str:
    """Atomicky přidělí pořadové číslo a vloží fakturu. Vrací přidělené `numero`.

    Souběžné generování je serializováno (PG: ON CONFLICT … RETURNING zamkne řádek
    counteru; SQLite: BEGIN IMMEDIATE) → žádné díry ani kolize čísel.
    """
    conn = get_conn()
    try:
        if USE_PG:
            row = _ex(conn,
                "INSERT INTO counters (owner_kod, anio, last_seq) VALUES (?, ?, 1) "
                "ON CONFLICT (owner_kod, anio) DO UPDATE SET last_seq = counters.last_seq + 1 "
                "RETURNING last_seq", (owner_kod, anio)).fetchone()
            seq = row["last_seq"]
        else:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT last_seq FROM counters WHERE owner_kod = ? AND anio = ?",
                (owner_kod, anio)).fetchone()
            last = row["last_seq"] if row else 0
            seq = last + 1
            if row:
                conn.execute("UPDATE counters SET last_seq = ? WHERE owner_kod = ? AND anio = ?",
                             (seq, owner_kod, anio))
            else:
                conn.execute("INSERT INTO counters (owner_kod, anio, last_seq) VALUES (?, ?, ?)",
                             (owner_kod, anio, seq))
        numero = "{}-{}-{:04d}".format(owner_kod, anio, seq)
        _ex(conn,
            "INSERT INTO invoices (numero, owner_kod, fecha_expedicion, fecha_vencimiento, "
            "mes_najmu, concepto, base_imponible, tipo_iva, cuota_iva, total, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (numero, owner_kod, fecha_expedicion, fecha_vencimiento, mes_najmu, concepto,
             base_imponible, tipo_iva, cuota_iva, total,
             datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        return numero
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _seq_from_numero(numero, kod, anio):
    """Vytáhne pořadové číslo z 'KOD-ROK-NNNN'. None, pokud neodpovídá vzoru."""
    prefix = "{}-{}-".format(kod, anio)
    if numero and numero.startswith(prefix):
        tail = numero[len(prefix):]
        if tail.isdigit():
            return int(tail)
    return None


def _resync_counter(conn, owner_kod, anio) -> None:
    """Srovná counter na nejvyšší pořadové číslo mezi zbývajícími fakturami
    daného majitele a roku (0, pokud žádná). Volá se po smazání i ruční změně čísla
    → po vymazání všech začne řada znovu od 01."""
    rows = _ex(conn, "SELECT numero FROM invoices WHERE owner_kod = ? "
               "AND substr(fecha_expedicion, 1, 4) = ?", (owner_kod, str(anio))).fetchall()
    max_seq = 0
    for r in rows:
        s = _seq_from_numero(r["numero"], owner_kod, anio)
        if s and s > max_seq:
            max_seq = s
    if USE_PG:
        _ex(conn, "INSERT INTO counters (owner_kod, anio, last_seq) VALUES (?, ?, ?) "
            "ON CONFLICT (owner_kod, anio) DO UPDATE SET last_seq = EXCLUDED.last_seq",
            (owner_kod, anio, max_seq))
    else:
        cur = _ex(conn, "UPDATE counters SET last_seq = ? WHERE owner_kod = ? AND anio = ?",
                  (max_seq, owner_kod, anio))
        if cur.rowcount == 0:
            _ex(conn, "INSERT INTO counters (owner_kod, anio, last_seq) VALUES (?, ?, ?)",
                (owner_kod, anio, max_seq))


def delete_invoice(numero: str) -> bool:
    """Smaže fakturu a srovná číselnou řadu (viz _resync_counter). Vrací True,
    pokud řádek existoval. Po smazání všech faktur majitele/roku začne řada od 01."""
    conn = get_conn()
    try:
        row = _ex(conn, "SELECT owner_kod, fecha_expedicion FROM invoices WHERE numero = ?",
                  (numero,)).fetchone()
        if row is None:
            return False
        _ex(conn, "DELETE FROM invoices WHERE numero = ?", (numero,))
        _resync_counter(conn, row["owner_kod"], int(row["fecha_expedicion"][:4]))
        conn.commit()
        return True
    finally:
        conn.close()


def rename_invoice(old_numero: str, new_numero: str) -> None:
    """Ruční přečíslování faktury. Ověří jedinečnost a srovná counter.
    Vyhodí ValueError (česky) při prázdném/duplicitním čísle nebo nenalezení."""
    new_numero = (new_numero or "").strip()
    if not new_numero:
        raise ValueError("Číslo faktury nesmí být prázdné.")
    if new_numero == old_numero:
        return
    conn = get_conn()
    try:
        row = _ex(conn, "SELECT owner_kod, fecha_expedicion FROM invoices WHERE numero = ?",
                  (old_numero,)).fetchone()
        if row is None:
            raise ValueError("Faktura {} nenalezena.".format(old_numero))
        if _ex(conn, "SELECT 1 FROM invoices WHERE numero = ?", (new_numero,)).fetchone():
            raise ValueError("Faktura s číslem {} už existuje.".format(new_numero))
        _ex(conn, "UPDATE invoices SET numero = ? WHERE numero = ?", (new_numero, old_numero))
        _resync_counter(conn, row["owner_kod"], int(row["fecha_expedicion"][:4]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
