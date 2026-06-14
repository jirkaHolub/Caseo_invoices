"""Generování PDF faktury.

Preferovaně WeasyPrint (z HTML šablony, pokud je nainstalovaný a má systémové
knihovny), jinak fallback na reportlab. Obě cesty produkují stejná povinná pole
faktury v češtině. Layout je kompaktní a navržený tak, aby se vešel na 1 stránku.
"""
from __future__ import annotations

import io
import os
from typing import Optional

ACCENT = "#1371B5"
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))

# Kandidátní TTF fonty s plnou podporou české diakritiky (ě, č, ř, ž, ů, …).
# Přibalený DejaVuSans je PRVNÍ → diakritika funguje stejně na macOS i na Linux
# serveru (Vercel). Vestavěná Helvetica v reportlabu je Latin-1 a háčky nezvládne.
_FONT_CANDIDATES = [
    (os.path.join(_PKG_DIR, "fonts", "DejaVuSans.ttf"),
     os.path.join(_PKG_DIR, "fonts", "DejaVuSans-Bold.ttf")),
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]
_fonts_cached = None


def _register_fonts():
    """Zaregistruje TTF font (regular, bold). Vrací jejich jména.

    Fallback na vestavěnou Helvetica, pokud žádný TTF není k dispozici
    (diakritika pak nemusí být správná, ale generování nespadne).
    """
    global _fonts_cached
    if _fonts_cached is not None:
        return _fonts_cached
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for regular, bold in _FONT_CANDIDATES:
        if os.path.exists(regular) and os.path.exists(bold):
            try:
                pdfmetrics.registerFont(TTFont("Body", regular))
                pdfmetrics.registerFont(TTFont("Body-Bold", bold))
                pdfmetrics.registerFontFamily(
                    "Body", normal="Body", bold="Body-Bold",
                    italic="Body", boldItalic="Body-Bold")
                _fonts_cached = ("Body", "Body-Bold")
                return _fonts_cached
            except Exception:
                continue
    _fonts_cached = ("Helvetica", "Helvetica-Bold")
    return _fonts_cached


def _try_weasyprint(html: str) -> Optional[bytes]:
    try:
        from weasyprint import HTML  # type: ignore
    except Exception:
        return None
    try:
        return HTML(string=html, base_url=_PKG_DIR).write_pdf()
    except Exception:
        return None


def _reportlab(ctx: dict, target) -> int:
    """Vykreslí fakturu přes reportlab. Vrací počet stránek (cíl: 1)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable,
    )

    accent = colors.HexColor(ACCENT)
    grey = colors.HexColor("#555555")
    light = colors.HexColor("#EAF2FA")
    rule = colors.HexColor("#CCCCCC")

    REG, BOLD = _register_fonts()
    styles = getSampleStyleSheet()
    base = ParagraphStyle("base", parent=styles["Normal"], fontName=REG,
                          fontSize=8.5, leading=11.5)
    small = ParagraphStyle("small", parent=base, fontSize=7.5, leading=10, textColor=grey)
    label = ParagraphStyle("label", parent=base, fontSize=6.8, leading=8.5, textColor=accent,
                           fontName=BOLD, spaceAfter=1)
    label_r = ParagraphStyle("label_r", parent=label, alignment=TA_RIGHT)
    h_title = ParagraphStyle("title", parent=base, fontSize=19, leading=21,
                             fontName=BOLD, textColor=accent)
    num_style = ParagraphStyle("num", parent=base, fontSize=11, leading=13,
                               alignment=TA_RIGHT, fontName=BOLD)
    num_sub = ParagraphStyle("numsub", parent=small, alignment=TA_RIGHT)
    party_name = ParagraphStyle("pname", parent=base, fontName=BOLD, fontSize=9.5, leading=12)
    money = ParagraphStyle("money", parent=base, alignment=TA_RIGHT)
    money_b = ParagraphStyle("moneyb", parent=money, fontName=BOLD)
    money_total = ParagraphStyle("moneytot", parent=money_b, fontSize=11.5, textColor=accent)
    total_label = ParagraphStyle("totlabel", parent=base, fontName=BOLD, fontSize=9)

    def P(text, style):
        return Paragraph(str(text).replace("\n", "<br/>"), style)

    doc = SimpleDocTemplate(
        target, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
        title="Faktura {}".format(ctx["numero"]), author=ctx["emisor_nombre"],
    )
    W = doc.width
    story = []

    # --- Hlavička: FAKTURA + číslo ---
    header = Table(
        [[P("FAKTURA", h_title),
          [P(ctx["numero"], num_style),
           P("Samofakturace · vystaveno odběratelem", num_sub)]]],
        colWidths=[W * 0.5, W * 0.5],
    )
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header)
    story.append(Spacer(1, 5))
    story.append(HRFlowable(width="100%", thickness=1.5, color=accent, spaceAfter=11))

    # --- Dodavatel / Odběratel: dvě symetrické boxy s mezerou ---
    def party(label_txt, nombre, nif, domicilio, extra=""):
        cell = [P(label_txt, label), P(nombre or "—", party_name),
                P("NIF: {}".format(nif or "—"), base),
                P(domicilio or "—", base)]
        if extra:
            cell.append(P(extra, small))
        return cell

    gap = W * 0.04
    col = (W - gap) / 2.0
    parties = Table(
        [[party("DODAVATEL (poskytovatel)", ctx["emisor_nombre"], ctx["emisor_nif"],
                ctx["emisor_domicilio"], ctx.get("emisor_propiedad", "")),
          "",
          party("ODBĚRATEL (zákazník)", ctx["dest_razon"], ctx["dest_nif"],
                ctx["dest_domicilio"])]],
        colWidths=[col, gap, col],
    )
    parties.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, 0), light),
        ("BACKGROUND", (2, 0), (2, 0), light),
        ("LINEABOVE", (0, 0), (0, 0), 2, accent),
        ("LINEABOVE", (2, 0), (2, 0), 2, accent),
        ("LEFTPADDING", (0, 0), (0, 0), 10), ("RIGHTPADDING", (0, 0), (0, 0), 10),
        ("LEFTPADDING", (2, 0), (2, 0), 10), ("RIGHTPADDING", (2, 0), (2, 0), 10),
        ("LEFTPADDING", (1, 0), (1, 0), 0), ("RIGHTPADDING", (1, 0), (1, 0), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 9), ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    story.append(parties)
    story.append(Spacer(1, 13))

    # --- Datumy (+ VS) v rovnoměrných sloupcích ---
    fecha_headers = [P("Datum vystavení", label), P("Období plnění", label),
                     P("Datum splatnosti", label)]
    fecha_values = [P(ctx["fecha_expedicion"], base), P(ctx["periodo"], base),
                    P(ctx["fecha_vencimiento"], base)]
    if ctx.get("variabilni_symbol"):
        fecha_headers.append(P("Variabilní symbol", label))
        fecha_values.append(P(ctx["variabilni_symbol"], base))
    _n = len(fecha_headers)
    fechas = Table([fecha_headers, fecha_values], colWidths=[W / _n] * _n)
    fechas.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
    ]))
    story.append(fechas)
    story.append(Spacer(1, 13))

    # --- Položky ---
    items = Table(
        [[P("Popis", label), P("Základ daně", label_r), P("Sazba DPH", label_r),
          P("DPH", label_r), P("Celkem", label_r)],
         [P(ctx["concepto"], base), P(ctx["base_imponible"], money),
          P("{} %".format(ctx["tipo_iva"]), money), P(ctx["cuota_iva"], money),
          P(ctx["total"], money_b)]],
        colWidths=[W * 0.40, W * 0.16, W * 0.13, W * 0.14, W * 0.17],
    )
    items.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 1, accent),
        ("LINEBELOW", (0, 1), (-1, 1), 0.5, rule),
        ("TOPPADDING", (0, 1), (-1, 1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
        ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
    ]))
    story.append(items)
    story.append(Spacer(1, 10))

    # --- Souhrn: pravý vyvážený box ---
    res_w = W * 0.50
    resumen = Table(
        [[P("Základ daně", base), P(ctx["base_imponible"], money)],
         [P("DPH ({} %)".format(ctx["tipo_iva"]), base), P(ctx["cuota_iva"], money)],
         [P("Mezisoučet (vč. DPH)", base), P(ctx["total"], money)],
         [P("Daň z příjmu (IRPF {} %)".format(ctx["tipo_irpf"]), base),
          P(ctx["retencion"], money)],
         [P("CELKEM K ÚHRADĚ", total_label), P(ctx["liquido"], money_total)]],
        colWidths=[res_w * 0.62, res_w * 0.38], hAlign="RIGHT",
    )
    resumen.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), light),
        ("LINEABOVE", (0, 4), (-1, 4), 1, accent),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 4), (-1, 4), 6), ("BOTTOMPADDING", (0, 4), (-1, 4), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (-1, 0), (-1, -1), 10),
    ]))
    story.append(resumen)
    story.append(Spacer(1, 16))

    # --- Patička: legenda ---
    story.append(HRFlowable(width="100%", thickness=0.5, color=rule, spaceAfter=6))
    story.append(P(ctx["leyenda"], small))
    if ctx.get("emisor_email"):
        story.append(P("Kontakt dodavatele: {}".format(ctx["emisor_email"]), small))

    doc.build(story)
    return doc.page


def render_pdf(ctx: dict, html: Optional[str] = None) -> bytes:
    """Vrátí PDF jako bytes (do paměti – žádný zápis na disk, vhodné pro serverless).

    Preferuje WeasyPrint (z `html`), jinak reportlab.
    """
    if html:
        data = _try_weasyprint(html)
        if data is not None:
            return data
    buf = io.BytesIO()
    _reportlab(ctx, buf)
    return buf.getvalue()
