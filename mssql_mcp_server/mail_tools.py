"""
mail_tools — wysyłka e-maili przez SMTP (MSSQL MCP Server).

Tool:
- send_email : wyślij e-mail z konta skonfigurowanego w .env (Gmail/Workspace
               przez SMTP + App Password). Twardy guard: odbiorcy tylko
               w dozwolonych domenach (MAIL_ALLOWED_RECIPIENT_DOMAINS).

Konfiguracja (.env — plik jest w .gitignore, NIE commitować danych):
- MAIL_SMTP_HOST                  default: smtp.gmail.com
- MAIL_SMTP_PORT                  default: 465 (SSL)
- MAIL_USER                       np. jacek.mikucki@certusoft.pl
- MAIL_APP_PASSWORD               Google App Password (wymaga 2FA na koncie;
                                  https://myaccount.google.com/apppasswords)
- MAIL_FROM_NAME                  opcjonalna nazwa nadawcy (display name)
- MAIL_ALLOWED_RECIPIENT_DOMAINS  default: certusoft.pl (lista po przecinku)
- MAIL_MAX_ATTACHMENT_MB          default: 20 (łączny rozmiar załączników)

Bezpieczeństwo:
- Narzędzie wysyła NAPRAWDĘ — agent może go użyć wyłącznie po akceptacji
  treści przez użytkownika.
- Odbiorca spoza dozwolonych domen => odmowa (żaden mail nie wychodzi).
- Hasło nigdy nie jest logowane ani zwracane w wynikach.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import List, Optional, Sequence

logger = logging.getLogger("mssql_mcp_server.mail")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _cfg() -> dict:
    user = os.environ.get("MAIL_USER", "").strip()
    password = os.environ.get("MAIL_APP_PASSWORD", "").strip()
    if not user or not password:
        raise RuntimeError(
            "Brak konfiguracji poczty: ustaw MAIL_USER i MAIL_APP_PASSWORD "
            "w mssql-mcp-server/.env (App Password: "
            "https://myaccount.google.com/apppasswords) i zrestartuj serwer MCP."
        )
    return {
        "host": os.environ.get("MAIL_SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(os.environ.get("MAIL_SMTP_PORT", "465")),
        "user": user,
        "password": password,
        "from_name": os.environ.get("MAIL_FROM_NAME", "").strip(),
        "allowed_domains": [
            d.strip().lower()
            for d in os.environ.get("MAIL_ALLOWED_RECIPIENT_DOMAINS", "certusoft.pl").split(",")
            if d.strip()
        ],
        "max_attachment_mb": float(os.environ.get("MAIL_MAX_ATTACHMENT_MB", "20")),
    }


def _normalize_recipients(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[;,]", value)
    elif isinstance(value, Sequence):
        parts = list(value)
    else:
        raise ValueError(f"Nieprawidłowa lista adresatów: {value!r}")
    result = []
    for p in parts:
        p = str(p).strip()
        if not p:
            continue
        if not _EMAIL_RE.match(p):
            raise ValueError(f"Nieprawidłowy adres e-mail: {p!r}")
        result.append(p)
    return result


def _check_domains(recipients: List[str], allowed: List[str]) -> None:
    if not allowed:
        return
    bad = [r for r in recipients if r.split("@", 1)[1].lower() not in allowed]
    if bad:
        raise ValueError(
            "Odbiorcy spoza dozwolonych domen ("
            + ", ".join(allowed)
            + "): "
            + ", ".join(bad)
            + ". Rozszerz MAIL_ALLOWED_RECIPIENT_DOMAINS w .env jeśli to zamierzone."
        )


def _normalize_attachments(value, max_total_mb: float) -> List[Path]:
    """Ścieżki plików → lista Path; waliduje istnienie i łączny rozmiar.

    Walidacja PRZED połączeniem z SMTP — zły plik nie może skończyć się
    mailem wysłanym bez załącznika.
    """
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        parts = [value]
    elif isinstance(value, Sequence):
        parts = list(value)
    else:
        raise ValueError(f"Nieprawidłowa lista załączników: {value!r}")

    paths: List[Path] = []
    total = 0
    for p in parts:
        p = str(p).strip()
        if not p:
            continue
        path = Path(p)
        if not path.is_absolute():
            raise ValueError(
                f"Załącznik musi mieć ścieżkę absolutną (dostałem {p!r}) — "
                "serwer MCP ma inny katalog roboczy niż agent."
            )
        if not path.exists() or not path.is_file():
            raise ValueError(f"Załącznik nie istnieje albo nie jest plikiem: {path}")
        total += path.stat().st_size
        paths.append(path)

    limit = max_total_mb * 1024 * 1024
    if total > limit:
        raise ValueError(
            f"Załączniki mają łącznie {total / 1024 / 1024:.1f} MB, limit to "
            f"{max_total_mb:g} MB (MAIL_MAX_ATTACHMENT_MB w .env)."
        )
    return paths


# Windows czyta typy MIME z rejestru i mapuje np. .csv na application/vnd.ms-excel —
# dla tekstowych formatów wymuszamy sensowny typ, żeby klient pocztowy nie udawał Excela.
_MIME_OVERRIDES = {
    ".csv": "text/csv",
    ".md": "text/markdown",
    ".log": "text/plain",
}


def _attach(msg: EmailMessage, paths: List[Path]) -> None:
    for path in paths:
        ctype = _MIME_OVERRIDES.get(path.suffix.lower())
        if ctype is None:
            ctype, encoding = mimetypes.guess_type(path.name)
            if ctype is None or encoding is not None:
                ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(
            path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=path.name,
        )


# ---------------------------------------------------------------------------
# send_email
# ---------------------------------------------------------------------------

def send_email(
    to,
    subject: str,
    body: str,
    cc=None,
    html: Optional[str] = None,
    attachments=None,
) -> str:
    cfg = _cfg()

    to_list = _normalize_recipients(to)
    cc_list = _normalize_recipients(cc)
    if not to_list:
        raise ValueError("Brak adresata (parametr 'to').")
    if not subject or not subject.strip():
        raise ValueError("Brak tematu (parametr 'subject').")
    if not body or not body.strip():
        raise ValueError("Brak treści (parametr 'body').")

    _check_domains(to_list + cc_list, cfg["allowed_domains"])
    attach_paths = _normalize_attachments(attachments, cfg["max_attachment_mb"])

    msg = EmailMessage()
    msg["From"] = formataddr((cfg["from_name"], cfg["user"])) if cfg["from_name"] else cfg["user"]
    msg["To"] = ", ".join(to_list)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)
    msg["Subject"] = subject.strip()
    msg.set_content(body)
    if html and html.strip():
        msg.add_alternative(html, subtype="html")
    # add_attachment po add_alternative sam przełącza kopertę na multipart/mixed.
    _attach(msg, attach_paths)

    logger.info(
        "Sending e-mail: to=%s cc=%s subject=%r attachments=%s via %s:%s as %s",
        to_list, cc_list, subject.strip(), [p.name for p in attach_paths],
        cfg["host"], cfg["port"], cfg["user"],
    )

    if cfg["port"] == 587:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as smtp:
            smtp.starttls()
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30) as smtp:
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)

    return (
        "OK — e-mail wysłany.\n"
        f"  From: {cfg['user']}\n"
        f"  To: {', '.join(to_list)}\n"
        + (f"  Cc: {', '.join(cc_list)}\n" if cc_list else "")
        + f"  Subject: {subject.strip()}"
        + (
            "\n  Załączniki: "
            + ", ".join(f"{p.name} ({p.stat().st_size / 1024:.0f} kB)" for p in attach_paths)
            if attach_paths
            else ""
        )
    )


# ---------------------------------------------------------------------------
# MCP wiring (ten sam wzorzec co rag_tools / cs_tools)
# ---------------------------------------------------------------------------

MAIL_TOOL_NAMES = {
    "send_email",
}


def tool_descriptors():
    from mcp.types import Tool

    return [
        Tool(
            name="send_email",
            description=(
                "Send a REAL e-mail via SMTP from the account configured in .env "
                "(MAIL_USER, Gmail App Password). Recipients restricted to "
                "MAIL_ALLOWED_RECIPIENT_DOMAINS (default certusoft.pl). "
                "Supports file attachments via 'attachments' (absolute paths). "
                "Use ONLY after the user explicitly approved the exact content."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {
                        "description": "Recipient(s): string ('a@x.pl' or 'a@x.pl, b@x.pl') or array of strings.",
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                    "subject": {"type": "string", "description": "E-mail subject."},
                    "body": {"type": "string", "description": "Plain-text body."},
                    "cc": {
                        "description": "Optional CC recipient(s), same format as 'to'.",
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                    "html": {"type": "string", "description": "Optional HTML alternative body."},
                    "attachments": {
                        "description": (
                            "Optional file attachment(s): ABSOLUTE path as string, or array of "
                            "absolute paths. MIME type is guessed from the extension; total size "
                            "limited by MAIL_MAX_ATTACHMENT_MB (default 20 MB). A missing file or "
                            "a relative path is rejected BEFORE anything is sent."
                        ),
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                },
                "required": ["to", "subject", "body"],
            },
        ),
    ]


def handle_tool(name: str, arguments: dict, connection_string: str) -> str:
    # connection_string nieużywany — sygnatura spójna z rag_tools/cs_tools.
    arguments = arguments or {}

    if name == "send_email":
        return send_email(
            to=arguments.get("to"),
            subject=arguments.get("subject", ""),
            body=arguments.get("body", ""),
            cc=arguments.get("cc"),
            html=arguments.get("html"),
            attachments=arguments.get("attachments"),
        )

    raise ValueError(f"Unknown mail tool: {name}")
