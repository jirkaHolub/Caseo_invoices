-- Schéma pro Supabase / PostgreSQL.
-- Spusť jednou v Supabase → SQL Editor (New query → vlož → Run).
-- (Aplikace se to snaží vytvořit i sama při startu, ale na serverless je
--  spolehlivější mít schéma vytvořené tímto skriptem předem.)

CREATE TABLE IF NOT EXISTS owners (
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
);

CREATE TABLE IF NOT EXISTS settings (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    razon     TEXT NOT NULL DEFAULT '',
    nif       TEXT NOT NULL DEFAULT '',
    domicilio TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS invoices (
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
);

CREATE TABLE IF NOT EXISTS counters (
    owner_kod TEXT NOT NULL,
    anio      INTEGER NOT NULL,
    last_seq  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner_kod, anio)
);

-- Jediný řádek nastavení (fiskální blok Caseo, vyplníš v appce v Nastavení).
INSERT INTO settings (id, razon, nif, domicilio)
VALUES (1, 'Caseo', '', '')
ON CONFLICT (id) DO NOTHING;
