# Caseo · Facturas

Lokální webová aplikace (FastAPI) pro generování **faktur s DPH 21 %**
vystavených **jménem majitele nemovitosti vůči společnosti Caseo** – tj.
**samofakturace** (daňový doklad vystavuje odběratel jménem dodavatele).

> Appka i faktura (PDF) jsou **v češtině**. Sazba DPH 21 %, měna EUR.

## Funkce
- **Majitelé** – CRUD fiskálních údajů (kód, razón social, NIF, domicilio, e-mail, nemovitost).
- **Nová faktura** – výběr majitele, měsíc nájmu a **částka brutto** (vč. 21 % IVA),
  živý náhled (base / IVA / total, datumy, předběžné číslo), varování při duplicitě.
- **Registr** – tabulka s filtrem podle majitele a roku, součet, **export CSV**.
- **Nastavení** – fiskální blok Caseo (odběratel).
- **PDF** do `./facturas/{kod}/{numero}.pdf` (reportlab; volitelně WeasyPrint z HTML šablony).

## Výpočet (vstup = částka brutto)
```
base_imponible = round(total / 1.21, 2)
cuota_iva      = round(total - base, 2)   # dopočet, base + IVA == total na cent
tipo_iva       = 21
```

## Číslování
`{KOD}-{ROK}-{NNNN}` (např. `SE-2026-0001`), per majitel + per kalendářní rok,
souvislé, bez děr (reset 1.1.). Skutečné číslo se přiděluje **atomicky** až při
generování (`BEGIN IMMEDIATE`). Číslo se nikdy nerecykluje. Rok se odvozuje z
*fecha de expedición*.

## Datumy (vstup = měsíc nájmu M, např. `2026-06`)
- `fecha_expedicion`  = poslední den měsíce M → `30/06/2026`
- `fecha_vencimiento` = 15. následujícího měsíce → `15/07/2026`

Obojí lze při vystavení ručně přepsat (sekce „Upravit datumy“).

## Spuštění
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --reload        # http://localhost:8000
```

> Aplikace cílí na Python 3.9+ a běží bez loginu (jen lokálně).
> SQLite databáze `data.db` a PDF složka `facturas/` se vytvoří automaticky.

### PDF engine
Ve výchozím stavu se používá **reportlab** (čistě Python, žádné systémové závislosti).
Chcete-li HTML→PDF přes **WeasyPrint** (preferováno, pokud je dostupné):
```bash
brew install pango          # macOS systémové knihovny
pip install weasyprint
```
Aplikace WeasyPrint použije automaticky, jinak se vrátí k reportlabu.

## Datový model
- `owners` – kod (unikátní), nombre/razón social, nif, domicilio, email?, nombre_propiedad?, variabilni_symbol?
- `settings` – razon, nif, domicilio (Caseo, odběratel; jediný řádek)
- `invoices` – numero, owner_kod, fecha_expedicion, fecha_vencimiento, mes_najmu,
  concepto, base_imponible, tipo_iva, cuota_iva, total, pdf_path, created_at *(immutable)*
- `counters` – (owner_kod, anio) → last_seq

## Struktura
```
app.py             FastAPI – routy + náhled API
db.py              SQLite – schéma a dotazy, atomické číslování
domain.py          výpočet IVA, datumy, číslo, concepto, ES formátování
pdf.py             generování PDF (reportlab / WeasyPrint)
templates/         Jinja2 (UI v češtině, faktura ve španělštině)
static/style.css   lehké CSS, akcent Caseo #1371B5
facturas/          vygenerovaná PDF
```
