"""
Daglig Gmail-helsesjekk.

Bekrefter at Gmail API + token.json fortsatt fungerer, uten å hente eller
behandle e-poster. Bruker det lettest mulige API-kallet (getProfile) og
gjør ALDRI en interaktiv nettleser-innlogging - dersom tokenet er ugyldig
og ikke kan fornyes stille (refresh_token), markeres sjekken som feilet slik
at reautentisering kan gjøres manuelt. Dette gjør modulen trygg å kjøre
headless i Docker / på en server (Railway, Render, cron, osv.).

Kan kjøres:
- automatisk fra app.py via APScheduler (kl. 12:00 daglig)
- manuelt: python gmail_healthcheck.py
- fra en ekstern cron/scheduler i Docker/Railway/Render
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_PATH = "token.json"
DB_PATH = "boliger.db"
LOKAL_TIDSSONE = ZoneInfo("Europe/Oslo")

# Feiltyper som betyr at brukeren må logge inn på nytt (ikke en forbigående feil)
REAUTH_PAKREVD_FEILTYPER = {
    "mangler_token",
    "mangler_credentials",
    "invalid_grant",
    "401_unauthorized",
    "token_kan_ikke_fornyes",
}


def _init_db(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS gmail_health_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        checked_at TEXT NOT NULL,
        status TEXT NOT NULL,
        error_type TEXT,
        error_message TEXT
    )
    """)
    conn.commit()


def _get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    _init_db(conn)
    return conn


def _last_ok_status():
    """Henter siste kjente status og siste vellykkede tidspunkt fra loggen."""
    conn = _get_db_connection()
    try:
        siste = conn.execute("""
            SELECT checked_at, status, error_type, error_message
            FROM gmail_health_log
            ORDER BY id DESC LIMIT 1
        """).fetchone()

        siste_ok = conn.execute("""
            SELECT checked_at FROM gmail_health_log
            WHERE status = 'ok'
            ORDER BY id DESC LIMIT 1
        """).fetchone()

        return siste, (siste_ok[0] if siste_ok else None)
    finally:
        conn.close()


def hent_gmail_status():
    """Public helper for Streamlit-UI: returnerer status som dict, eller None hvis aldri kjørt."""
    siste, siste_ok = _last_ok_status()
    if not siste:
        return None

    checked_at, status, error_type, error_message = siste
    return {
        "checked_at": checked_at,
        "status": status,
        "error_type": error_type,
        "error_message": error_message,
        "last_success_at": siste_ok,
        "krever_reautentisering": error_type in REAUTH_PAKREVD_FEILTYPER,
    }


def _logg_resultat(status, error_type=None, error_message=None):
    conn = _get_db_connection()
    try:
        conn.execute(
            "INSERT INTO gmail_health_log (checked_at, status, error_type, error_message) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), status, error_type, error_message),
        )
        conn.commit()
    finally:
        conn.close()


def _last_gmail_service_uten_interaktiv_login():
    """
    Laster Gmail-service kun fra eksisterende token.json.
    Gjør en stille (ikke-interaktiv) refresh hvis token er utløpt, men
    starter ALDRI en nettleser-basert innlogging (fungerer derfor headless).
    """
    if not os.path.exists(TOKEN_PATH):
        raise FileNotFoundError("mangler_token")

    creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        else:
            raise RefreshError("token_kan_ikke_fornyes: mangler gyldig refresh_token, reautentisering kreves")

    return build("gmail", "v1", credentials=creds)


def klassifiser_feil(exc: Exception) -> str:
    """Klassifiserer en feil til en kort, gjenkjennelig kode."""
    if isinstance(exc, FileNotFoundError):
        return "mangler_token" if str(exc) == "mangler_token" else "mangler_credentials"

    if isinstance(exc, RefreshError):
        melding = str(exc)
        if "invalid_grant" in melding:
            return "invalid_grant"
        if "token_kan_ikke_fornyes" in melding:
            return "token_kan_ikke_fornyes"
        return "refresh_feil"

    if isinstance(exc, HttpError):
        status = exc.resp.status if getattr(exc, "resp", None) else None
        if status == 401:
            return "401_unauthorized"
        if status == 403:
            return "403_forbidden"
        return f"http_feil_{status}"

    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, OSError)):
        return "nettverksfeil"

    return "ukjent_feil"


def kjor_gmail_helsesjekk():
    """
    Kjører den daglige Gmail-helsesjekken.

    Gjør ETT minimalt API-kall (getProfile) - henter ikke og behandler ikke
    noen e-poster. Logger resultatet (suksess eller feil m/klassifisering)
    til gmail_health_log i boliger.db.
    """
    try:
        service = _last_gmail_service_uten_interaktiv_login()
        service.users().getProfile(userId="me").execute()
    except Exception as exc:
        feiltype = klassifiser_feil(exc)
        _logg_resultat(status="feilet", error_type=feiltype, error_message=str(exc))
        return False, feiltype, str(exc)

    _logg_resultat(status="ok")
    return True, None, None


if __name__ == "__main__":
    ok, feiltype, melding = kjor_gmail_helsesjekk()
    if ok:
        print("Gmail API: OK")
        sys.exit(0)
    else:
        print(f"Gmail API: FEILET ({feiltype}) - {melding}")
        sys.exit(1)
