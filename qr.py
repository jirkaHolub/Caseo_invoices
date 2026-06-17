"""QR platba (český standard SPAYD) pro fakturu.

Plátcem je Caseo (česky), proto SPAYD – čtou ho české bankovní aplikace, podporuje
variabilní symbol i částku v EUR a zahraniční IBAN. Pro PDF se QR vykresluje nativně
v reportlabu (z `qr_payload`), pro web/WeasyPrint zde vyrobíme inline SVG.
"""
from __future__ import annotations

from typing import Optional


def spayd_payload(iban, amount, vs=None, msg=None) -> Optional[str]:
    """Sestaví SPAYD řetězec. Vrátí None, pokud chybí IBAN.

    Např.: SPD*1.0*ACC:CZ65...*AM:84.30*CC:EUR*X-VS:20260001*MSG:Faktura CA-2026-0001
    """
    iban = (iban or "").replace(" ", "").upper()
    if not iban:
        return None
    parts = ["SPD", "1.0", "ACC:" + iban, "AM:{:.2f}".format(amount), "CC:EUR"]
    vs_digits = "".join(ch for ch in str(vs or "") if ch.isdigit())
    if vs_digits:
        parts.append("X-VS:" + vs_digits)
    if msg:
        parts.append("MSG:" + str(msg).replace("*", "-")[:60])
    return "*".join(parts)


def qr_svg(payload: str, quiet: int = 4) -> str:
    """Inline SVG QR kódu (čtverečky z matice). Bez Pillow, škálovatelné."""
    import qrcode  # pure-python; matici umí i bez Pillow

    qr = qrcode.QRCode(border=quiet, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(payload)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    rects = ['<rect width="100%" height="100%" fill="#fff"/>']
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if dark:
                rects.append('<rect x="{}" y="{}" width="1" height="1"/>'.format(x, y))
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" '
            'shape-rendering="crispEdges" fill="#000">{body}</svg>'
            ).format(n=n, body="".join(rects))
