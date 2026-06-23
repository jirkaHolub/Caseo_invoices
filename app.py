"""FastAPI aplikace – generátor španělských faktur (samofakturace pro Caseo).

Spuštění:  uvicorn app:app --reload   →  http://localhost:8000
"""
from __future__ import annotations

import base64
import csv
import io
import os
import secrets
from datetime import date
from decimal import Decimal
from typing import Optional
from urllib.parse import quote, quote_plus

from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse, Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

try:  # lokálně načte .env; na Vercelu proměnné přicházejí z dashboardu (no-op)
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import db
import domain
import mailer
import qr
from pdf import render_pdf, ACCENT

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BASIC_AUTH_USER = os.environ.get("BASIC_AUTH_USER", "")
BASIC_AUTH_PASS = os.environ.get("BASIC_AUTH_PASS", "")

app = FastAPI(title="Caseo – Facturas")
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    """HTTP Basic auth – aktivní jen když jsou nastavené obě env proměnné."""
    if BASIC_AUTH_USER and BASIC_AUTH_PASS:
        header = request.headers.get("Authorization", "")
        authorized = False
        if header.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                authorized = (secrets.compare_digest(user, BASIC_AUTH_USER)
                              and secrets.compare_digest(pwd, BASIC_AUTH_PASS))
            except Exception:
                authorized = False
        if not authorized:
            return Response(status_code=401, headers={
                "WWW-Authenticate": 'Basic realm="Caseo Facturas"'})
    return await call_next(request)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.filters["eur"] = domain.format_eur
templates.env.filters["fecha"] = domain.format_date
templates.env.filters["num_es"] = domain.format_number_es
# Odvozené (z base/total) – IRPF jako srážka (záporně) a líquido k úhradě.
templates.env.filters["irpf"] = lambda base: domain.format_eur(-domain.compute_retencion(base))
templates.env.filters["liquido"] = lambda total, base: domain.format_eur(
    domain.compute_liquido(total, base))


@app.on_event("startup")
def _startup() -> None:
    # Lokálně (SQLite) vytvoří schéma. Na serverless/Postgresu se schéma zakládá
    # přes schema.sql – proto chybu nešíříme dál, ať start nikdy nespadne.
    try:
        db.init_db()
    except Exception:
        pass


def render(template: str, request: Request, **ctx) -> HTMLResponse:
    ctx.setdefault("active", "")
    ctx["request"] = request
    ctx["accent"] = ACCENT
    # Starlette ≥0.29: signatura je TemplateResponse(request, name, context).
    return templates.TemplateResponse(request, template, ctx)


def settings_complete(s) -> bool:
    return bool((s["razon"] or "").strip() and (s["nif"] or "").strip()
                and (s["domicilio"] or "").strip())


def build_ctx(inv: sqlite3.Row, s: sqlite3.Row) -> dict:
    """Sestaví slovník s formátovanými hodnotami pro PDF / náhled faktury."""
    year, _, month = (inv["mes_najmu"] or "-").partition("-")
    liquido = domain.compute_liquido(inv["total"], inv["base_imponible"])
    # QR platba (SPAYD): IBAN majitele, částka k úhradě, VS (X-VS) a zpráva
    # s účelem "payout caseo" + číslem faktury.
    qr_payload = qr.spayd_payload(inv["owner_iban"], liquido, inv["owner_vs"],
                                  "payout caseo " + inv["numero"])
    return {
        "numero": inv["numero"],
        "fecha_expedicion": domain.format_date(inv["fecha_expedicion"]),
        "fecha_vencimiento": domain.format_date(inv["fecha_vencimiento"]),
        "periodo": "{}/{}".format(month, year),
        "emisor_nombre": inv["owner_nombre"],
        "emisor_nif": inv["owner_nif"],
        "emisor_tipo_id": inv["owner_tipo_id"] or "NIF",
        "emisor_domicilio": inv["owner_domicilio"],
        "emisor_propiedad": inv["owner_propiedad"] or "",
        "emisor_email": inv["owner_email"] or "",
        "emisor_iban": inv["owner_iban"] or "",
        "variabilni_symbol": inv["owner_vs"] or "",
        "dest_razon": s["razon"],
        "dest_nif": s["nif"],
        "dest_domicilio": s["domicilio"],
        "concepto": inv["concepto"],
        "base_imponible": domain.format_eur(inv["base_imponible"]),
        "tipo_iva": inv["tipo_iva"],
        "cuota_iva": domain.format_eur(inv["cuota_iva"]),
        "total": domain.format_eur(inv["total"]),
        "tipo_irpf": domain.TIPO_IRPF,
        "retencion": domain.format_eur(-domain.compute_retencion(inv["base_imponible"])),
        "liquido": domain.format_eur(liquido),
        "qr_payload": qr_payload or "",
        "qr_svg": qr.qr_svg(qr_payload) if qr_payload else "",
        "leyenda": domain.LEYENDA,
        "accent": ACCENT,
    }


def _pdf_bytes(numero: str) -> Optional[bytes]:
    """Vygeneruje PDF faktury do paměti (žádný zápis na disk). Vrací bytes nebo None."""
    inv = db.get_invoice(numero)
    if inv is None:
        return None
    s = db.get_settings()
    ctx = build_ctx(inv, s)
    html = templates.get_template("invoice_pdf.html").render(c=ctx, embed=False)
    return render_pdf(ctx, html=html)


# ============================================================ navigace

@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/facturas", status_code=303)


# ============================================================ majitelé

def _parse_property_id(value: str):
    """Form hodnota property_id → int nebo None ('' = bez nemovitosti)."""
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def _selected_property_id(owner_kod: str):
    """Hodnota výběru ve formuláři faktury: 'prop:<id>' → id nemovitosti, jinak None."""
    if owner_kod and owner_kod.startswith("prop:"):
        tail = owner_kod[len("prop:"):]
        if tail.isdigit():
            return int(tail)
    return None


@app.get("/propietarios", response_class=HTMLResponse)
def owners_page(request: Request, msg: str = "", err: str = "", edit: str = "",
                edit_prop: str = ""):
    owners = db.list_owners()
    properties = db.list_properties()
    edit_owner = db.get_owner(edit) if edit else None
    edit_property = db.get_property(int(edit_prop)) if edit_prop.isdigit() else None
    return render("owners.html", request, active="owners", owners=owners,
                  properties=properties, edit_owner=edit_owner,
                  edit_property=edit_property, msg=msg, err=err)


@app.post("/propietarios")
def owners_create(
    kod: str = Form(...),
    nombre: str = Form(...),
    nif: str = Form(...),
    domicilio: str = Form(...),
    email: str = Form(""),
    nombre_propiedad: str = Form(""),
    variabilni_symbol: str = Form(""),
    tipo_id: str = Form("NIF"),
    iban: str = Form(""),
    property_id: str = Form(""),
):
    kod = kod.strip().upper()
    if not kod:
        return RedirectResponse("/propietarios?err=Kód+je+povinný.", status_code=303)
    try:
        db.create_owner(kod, nombre.strip(), nif.strip(), domicilio.strip(),
                        email.strip(), nombre_propiedad.strip(), variabilni_symbol.strip(),
                        domain.normalize_tipo_id(tipo_id), iban.replace(" ", "").upper(),
                        _parse_property_id(property_id))
    except db.DuplicateKod:
        return RedirectResponse(
            "/propietarios?err=Majitel+s+kódem+{}+už+existuje.".format(kod), status_code=303)
    return RedirectResponse("/propietarios?msg=Majitel+{}+uložen.".format(kod), status_code=303)


@app.post("/propietarios/{kod}/actualizar")
def owners_update(
    kod: str,
    nombre: str = Form(...),
    nif: str = Form(...),
    domicilio: str = Form(...),
    email: str = Form(""),
    nombre_propiedad: str = Form(""),
    variabilni_symbol: str = Form(""),
    tipo_id: str = Form("NIF"),
    iban: str = Form(""),
    property_id: str = Form(""),
):
    db.update_owner(kod, nombre.strip(), nif.strip(), domicilio.strip(),
                    email.strip(), nombre_propiedad.strip(), variabilni_symbol.strip(),
                    domain.normalize_tipo_id(tipo_id), iban.replace(" ", "").upper(),
                    _parse_property_id(property_id))
    return RedirectResponse("/propietarios?msg=Majitel+{}+upraven.".format(kod), status_code=303)


@app.post("/propietarios/{kod}/eliminar")
def owners_delete(kod: str):
    if db.owner_invoice_count(kod) > 0:
        return RedirectResponse(
            "/propietarios?err=Majitele+{}+nelze+smazat+–+má+vystavené+faktury.".format(kod),
            status_code=303)
    db.delete_owner(kod)
    return RedirectResponse("/propietarios?msg=Majitel+{}+smazán.".format(kod), status_code=303)


# ============================================================ nemovitosti (společné karty)

@app.post("/propiedades")
def property_create(
    nombre: str = Form(...),
    iban: str = Form(""),
    variabilni_symbol: str = Form(""),
):
    nombre = nombre.strip()
    if not nombre:
        return RedirectResponse("/propietarios?err=Název+nemovitosti+je+povinný.",
                                status_code=303)
    db.create_property(nombre, iban.replace(" ", "").upper(), variabilni_symbol.strip())
    return RedirectResponse(
        "/propietarios?msg=Nemovitost+„{}\"+uložena.".format(nombre), status_code=303)


@app.post("/propiedades/{prop_id}/actualizar")
def property_update(
    prop_id: int,
    nombre: str = Form(...),
    iban: str = Form(""),
    variabilni_symbol: str = Form(""),
):
    db.update_property(prop_id, nombre.strip(), iban.replace(" ", "").upper(),
                       variabilni_symbol.strip())
    return RedirectResponse("/propietarios?msg=Nemovitost+upravena.", status_code=303)


@app.post("/propiedades/{prop_id}/eliminar")
def property_delete(prop_id: int):
    db.delete_property(prop_id)
    return RedirectResponse(
        "/propietarios?msg=Nemovitost+smazána+(majitelé+odpojeni).", status_code=303)


# ============================================================ nová faktura

@app.get("/facturas/nueva", response_class=HTMLResponse)
def invoice_new(request: Request, owner: str = ""):
    owners = db.list_owners()
    properties = db.list_properties()
    s = db.get_settings()
    hoy = date.today()
    mes_default = "{:04d}-{:02d}".format(hoy.year, hoy.month)
    return render("new_invoice.html", request, active="new", owners=owners,
                  properties=properties, settings_ok=settings_complete(s),
                  mes_default=mes_default, owner_selected=owner)


@app.post("/api/preview", response_class=JSONResponse)
def invoice_preview(
    owner_kod: str = Form(...),
    mes: str = Form(...),
    total: str = Form(""),
    concepto: str = Form(""),
):
    try:
        total_d = domain.parse_amount(total)
        exp, ven = domain.default_dates(mes)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)})

    anio = exp.year
    year, _, month = mes.partition("-")
    resp = {
        "ok": True,
        "fecha_expedicion": exp.isoformat(),
        "fecha_vencimiento": ven.isoformat(),
        "fecha_expedicion_es": domain.format_date(exp),
        "fecha_vencimiento_es": domain.format_date(ven),
        "periodo": "{}/{}".format(month, year),
        "tipo_iva": domain.TIPO_IVA,
        "tipo_irpf": domain.TIPO_IRPF,
    }

    # Režim nemovitosti (spolumajitelství): total se rozdělí mezi spolumajitele,
    # každý dostane vlastní fakturu (náhled obou).
    prop_id = _selected_property_id(owner_kod)
    if prop_id is not None:
        prop = db.get_property(prop_id)
        co_owners = db.owners_for_property(prop_id) if prop else []
        if not co_owners:
            return JSONResponse({"ok": False, "error": "Nemovitost nemá žádné spolumajitele."})
        shares = domain.split_amount(total_d, len(co_owners))
        splits, dup = [], None
        for owner, share in zip(co_owners, shares):
            base, cuota, total_part = domain.compute_amounts(share)
            seq = db.peek_next_seq(owner["kod"], anio)
            d = db.invoice_exists_for_month(owner["kod"], mes)
            if d and not dup:
                dup = d["numero"]
            splits.append({
                "kod": owner["kod"], "nombre": owner["nombre"],
                "numero": domain.format_numero(owner["kod"], anio, seq),
                "total": domain.format_eur(total_part),
                "retencion": domain.format_eur(-domain.compute_retencion(base)),
                "liquido": domain.format_eur(domain.compute_liquido(total_part, base)),
            })
        resp.update({
            "is_property": True,
            "property_nombre": prop["nombre"],
            "total": domain.format_eur(total_d),
            "concepto": concepto.strip() or domain.default_concepto(prop["nombre"], mes),
            "splits": splits,
            "duplicate": dup,
        })
        return JSONResponse(resp)

    # Jednotlivý majitel (původní chování).
    owner = db.get_owner(owner_kod)
    if owner is None:
        return JSONResponse({"ok": False, "error": "Vyberte majitele."})
    base, cuota, total_d = domain.compute_amounts(total_d)
    seq = db.peek_next_seq(owner_kod, anio)
    dup = db.invoice_exists_for_month(owner_kod, mes)
    resp.update({
        "is_property": False,
        "numero": domain.format_numero(owner_kod, anio, seq),
        "base": domain.format_eur(base),
        "cuota": domain.format_eur(cuota),
        "total": domain.format_eur(total_d),
        "retencion": domain.format_eur(-domain.compute_retencion(base)),
        "liquido": domain.format_eur(domain.compute_liquido(total_d, base)),
        "concepto": concepto.strip() or domain.default_concepto(owner["nombre_propiedad"], mes),
        "variabilni_symbol": owner["variabilni_symbol"] or "",
        "duplicate": (dup["numero"] if dup else None),
    })
    return JSONResponse(resp)


@app.post("/facturas")
def invoice_create(
    request: Request,
    owner_kod: str = Form(...),
    mes: str = Form(...),
    total: str = Form(...),
    concepto: str = Form(""),
    fecha_expedicion: str = Form(""),
    fecha_vencimiento: str = Form(""),
    confirmar: str = Form(""),
):
    s = db.get_settings()
    if not settings_complete(s):
        return _new_with_error(request, owner_kod, mes, total, concepto,
                               "Nejdřív vyplňte fiskální údaje Caseo v Nastavení.")
    try:
        total_d = domain.parse_amount(total)
        domain.parse_mes(mes)
        exp_def, ven_def = domain.default_dates(mes)
    except ValueError as e:
        return _new_with_error(request, owner_kod, mes, total, concepto, str(e))

    exp = _parse_iso(fecha_expedicion) or exp_def
    ven = _parse_iso(fecha_vencimiento) or ven_def
    anio = exp.year

    # Režim nemovitosti: jeden vstup → 2 (či více) faktur rozdělených mezi spolumajitele.
    prop_id = _selected_property_id(owner_kod)
    if prop_id is not None:
        return _create_property_invoices(request, prop_id, owner_kod, mes, total,
                                         total_d, concepto, exp, ven, anio, confirmar)

    owner = db.get_owner(owner_kod)
    if owner is None:
        return RedirectResponse("/facturas/nueva?", status_code=303)

    # Kontrola duplicity – generuj až po potvrzení.
    dup = db.invoice_exists_for_month(owner_kod, mes)
    if dup and confirmar != "1":
        return _new_with_error(
            request, owner_kod, mes, total, concepto,
            "Pro tohoto majitele a měsíc {} už existuje faktura {}. "
            "Potvrďte vygenerování další.".format(mes, dup["numero"]),
            warn_duplicate=dup["numero"])

    base, cuota, total_d = domain.compute_amounts(total_d)
    concepto_final = concepto.strip() or domain.default_concepto(owner["nombre_propiedad"], mes)

    numero = db.create_invoice_atomic(
        owner_kod=owner_kod, anio=anio,
        fecha_expedicion=exp.isoformat(), fecha_vencimiento=ven.isoformat(),
        mes_najmu=mes, concepto=concepto_final,
        base_imponible=float(base), tipo_iva=domain.TIPO_IVA,
        cuota_iva=float(cuota), total=float(total_d),
    )
    return RedirectResponse(
        "/facturas/{}?msg=Faktura+vygenerována.".format(numero), status_code=303)


def _create_property_invoices(request, prop_id, owner_kod, mes, total, total_d,
                              concepto, exp, ven, anio, confirmar):
    """Vystaví faktury pro všechny spolumajitele nemovitosti (rozdělení dle split_amount)."""
    prop = db.get_property(prop_id)
    co_owners = db.owners_for_property(prop_id) if prop else []
    if not co_owners:
        return _new_with_error(request, owner_kod, mes, total, concepto,
                               "Nemovitost nemá žádné spolumajitele – přiřaďte je na kartě majitele.")

    # Duplicita u kteréhokoli spolumajitele → vyžádej potvrzení.
    if confirmar != "1":
        for o in co_owners:
            d = db.invoice_exists_for_month(o["kod"], mes)
            if d:
                return _new_with_error(
                    request, owner_kod, mes, total, concepto,
                    "Pro spolumajitele {} a měsíc {} už existuje faktura {}. "
                    "Potvrďte vygenerování dalších.".format(o["kod"], mes, d["numero"]),
                    warn_duplicate=d["numero"])

    concepto_final = concepto.strip() or domain.default_concepto(prop["nombre"], mes)
    shares = domain.split_amount(total_d, len(co_owners))
    items = []
    for o, share in zip(co_owners, shares):
        base, cuota, total_part = domain.compute_amounts(share)
        items.append({
            "owner_kod": o["kod"], "anio": anio,
            "fecha_expedicion": exp.isoformat(), "fecha_vencimiento": ven.isoformat(),
            "mes_najmu": mes, "concepto": concepto_final,
            "base_imponible": float(base), "tipo_iva": domain.TIPO_IVA,
            "cuota_iva": float(cuota), "total": float(total_part),
        })
    numeros = db.create_invoices_atomic(items)
    msg = "Vystaveny {} faktury: {}".format(len(numeros), ", ".join(numeros))
    return RedirectResponse("/facturas?msg=" + quote_plus(msg), status_code=303)


def _new_with_error(request, owner_kod, mes, total, concepto, err,
                    warn_duplicate: str = "") -> HTMLResponse:
    owners = db.list_owners()
    properties = db.list_properties()
    s = db.get_settings()
    return render("new_invoice.html", request, active="new", owners=owners,
                  properties=properties, settings_ok=settings_complete(s), mes_default=mes,
                  owner_selected=owner_kod, err=err, warn_duplicate=warn_duplicate,
                  prev_total=total, prev_concepto=concepto)


def _parse_iso(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# ============================================================ registr

@app.get("/facturas", response_class=HTMLResponse)
def registry(request: Request, owner: str = "", anio: str = "", msg: str = ""):
    anio_int = int(anio) if anio.isdigit() else None
    invoices = db.list_invoices(owner_kod=owner or None, anio=anio_int)
    owners = db.list_owners()
    years = sorted({inv["fecha_expedicion"][:4] for inv in db.list_invoices()}, reverse=True)
    total_sum = sum(
        domain.compute_liquido(inv["total"], inv["base_imponible"]) for inv in invoices)
    return render("registry.html", request, active="registry", invoices=invoices,
                  owners=owners, years=years, f_owner=owner, f_anio=anio,
                  total_sum=total_sum, msg=msg)


@app.get("/facturas/{numero}", response_class=HTMLResponse)
def invoice_detail(request: Request, numero: str, msg: str = "", err: str = ""):
    inv = db.get_invoice(numero)
    if inv is None:
        return render("not_found.html", request, numero=numero)
    s = db.get_settings()
    ctx = build_ctx(inv, s)
    return render("invoice_detail.html", request, active="registry", inv=inv, c=ctx,
                  msg=msg, err=err, mail_ready=mailer.is_configured())


@app.get("/facturas/{numero}/pdf")
def invoice_pdf(numero: str):
    data = _pdf_bytes(numero)
    if data is None:
        return JSONResponse({"error": "Faktura nenalezena."}, status_code=404)
    return Response(content=data, media_type="application/pdf", headers={
        "Content-Disposition": 'inline; filename="{}.pdf"'.format(numero)})


@app.post("/facturas/{numero}/delete")
def invoice_delete(numero: str):
    """Smaže fakturu (nevratné). POST kvůli bezpečnosti; chrání HTTP Basic auth."""
    deleted = db.delete_invoice(numero)
    msg = ("Faktura {} byla smazána.".format(numero) if deleted
           else "Faktura {} nenalezena.".format(numero))
    return RedirectResponse("/facturas?msg=" + quote_plus(msg), status_code=303)


@app.post("/facturas/{numero}/numero")
def invoice_rename(numero: str, nuevo_numero: str = Form(...)):
    """Ruční změna čísla faktury (ověří jedinečnost, srovná číselnou řadu)."""
    try:
        db.rename_invoice(numero, nuevo_numero)
    except ValueError as e:
        return RedirectResponse(
            "/facturas/{}?err={}".format(quote(numero, safe=""), quote_plus(str(e))),
            status_code=303)
    target = (nuevo_numero or "").strip() or numero
    return RedirectResponse(
        "/facturas/{}?msg={}".format(quote(target, safe=""),
                                     quote_plus("Číslo faktury změněno.")),
        status_code=303)


@app.post("/facturas/{numero}/email")
def invoice_email(numero: str):
    """Odešle fakturu e-mailem majiteli (Resend, PDF v příloze)."""
    inv = db.get_invoice(numero)
    if inv is None:
        return RedirectResponse("/facturas?err=" + quote_plus("Faktura nenalezena."),
                                status_code=303)

    def back(param: str, text: str):
        return RedirectResponse(
            "/facturas/{}?{}={}".format(quote(numero, safe=""), param, quote_plus(text)),
            status_code=303)

    to = (inv["owner_email"] or "").strip()
    if not to:
        return back("err", "Majitel nemá vyplněný e-mail – doplň ho na kartě majitele.")
    pdf = _pdf_bytes(numero)
    if pdf is None:
        return back("err", "Nepodařilo se vygenerovat PDF faktury.")
    html = templates.env.get_template("email_invoice.html").render(
        c=build_ctx(inv, db.get_settings()))
    try:
        mailer.send_email(to, "Faktura {}".format(numero), html,
                          attachments=[{"filename": numero + ".pdf", "content": pdf}])
    except mailer.MailError as e:
        return back("err", "Odeslání selhalo: " + str(e))
    db.mark_invoice_sent(numero)
    return back("msg", "Faktura odeslána na " + to)


# ============================================================ CSV export

@app.get("/export.csv")
def export_csv(owner: str = "", anio: str = ""):
    anio_int = int(anio) if anio.isdigit() else None
    invoices = db.list_invoices(owner_kod=owner or None, anio=anio_int)
    buf = io.StringIO()
    buf.write("﻿")  # BOM pro Excel
    w = csv.writer(buf, delimiter=";")
    w.writerow(["numero", "owner_kod", "razon_social", "mes_najmu",
                "fecha_expedicion", "fecha_vencimiento", "base_imponible",
                "tipo_iva", "cuota_iva", "total",
                "tipo_irpf", "retencion_irpf", "liquido"])
    for inv in invoices:
        w.writerow([
            inv["numero"], inv["owner_kod"], inv["owner_nombre"], inv["mes_najmu"],
            domain.format_date(inv["fecha_expedicion"]),
            domain.format_date(inv["fecha_vencimiento"]),
            domain.format_number_es(inv["base_imponible"]),
            "{} %".format(inv["tipo_iva"]),
            domain.format_number_es(inv["cuota_iva"]),
            domain.format_number_es(inv["total"]),
            "{} %".format(domain.TIPO_IRPF),
            domain.format_number_es(domain.compute_retencion(inv["base_imponible"])),
            domain.format_number_es(domain.compute_liquido(inv["total"], inv["base_imponible"])),
        ])
    buf.seek(0)
    headers = {"Content-Disposition": "attachment; filename=facturas_caseo.csv"}
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers=headers)


# ============================================================ nastavení

@app.get("/ajustes", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    s = db.get_settings()
    return render("settings.html", request, active="settings", s=s, msg=msg,
                  complete=settings_complete(s))


@app.post("/ajustes")
def settings_save(
    razon: str = Form(...),
    nif: str = Form(...),
    domicilio: str = Form(...),
):
    db.update_settings(razon.strip(), nif.strip(), domicilio.strip())
    return RedirectResponse("/ajustes?msg=Nastavení+uloženo.", status_code=303)
