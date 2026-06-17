"""Odesílání e-mailů přes Resend (HTTP API) – bez externí závislosti (stdlib urllib).

Konfigurace přes env (na Vercelu v Environment Variables):
  RESEND_API_KEY   – povinné, API klíč z resend.com
  MAIL_FROM        – odesílatel, např. 'Caseo <faktury@tvojedomena.cz>'
                     (doména musí být v Resendu ověřená; default je testovací
                     onboarding@resend.dev, který doručí jen na adresu účtu)
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Caseo <onboarding@resend.dev>"


class MailError(Exception):
    """Odeslání e-mailu selhalo (chybí konfigurace nebo chyba od Resendu)."""


def is_configured() -> bool:
    """True, pokud je nastavený RESEND_API_KEY (jinak nemá smysl nabízet odeslání)."""
    return bool(os.environ.get("RESEND_API_KEY", "").strip())


def mail_from() -> str:
    return os.environ.get("MAIL_FROM", "").strip() or DEFAULT_FROM


def send_email(to, subject: str, html: str, attachments=None) -> str:
    """Odešle e-mail. `attachments` = seznam dictů {'filename', 'content'(bytes)}.
    Vrací ID zprávy z Resendu. Při chybě vyhodí MailError (česky)."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        raise MailError("Chybí RESEND_API_KEY – nastav ho ve Vercel env a redeployni.")

    payload = {
        "from": mail_from(),
        "to": [to] if isinstance(to, str) else list(to),
        "subject": subject,
        "html": html,
    }
    if attachments:
        payload["attachments"] = [
            {"filename": a["filename"],
             "content": base64.b64encode(a["content"]).decode("ascii")}
            for a in attachments
        ]

    req = urllib.request.Request(
        RESEND_API_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json",
                 # Cloudflare u Resendu blokuje výchozí UA "Python-urllib" (chyba 1010).
                 "User-Agent": "Caseo-Invoices/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
            return (json.loads(body).get("id", "") if body else "")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise MailError("Resend HTTP {}: {}".format(exc.code, detail[:300]))
    except urllib.error.URLError as exc:
        raise MailError("Síťová chyba při odesílání: {}".format(exc.reason))
