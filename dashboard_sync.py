"""Synkroniserer prosessert boligdata fra BoligScanner til en separat
Dashboard-database (et annet, presentasjons-bare prosjekt på disk).

BoligScanner er alltid kilden til sannhet. Dashboard-databasen behandles kun
som et eksportmål: denne modulen kobler seg til en SQLite-fil på en
konfigurerbar sti, oppretter/migrerer skjemaet ved behov, og setter inn eller
oppdaterer rader - den sletter, gjetter eller genererer aldri data på egen
hånd, og den skraper/parser aldri noe selv.

Matching for å unngå duplikater: finn_ad_id først, deretter finn_url som
fallback (samme mønster som brukes internt i app.py). Kun kolonnene i
MANAGED_COLUMNS blir noensinne skrevet til - eventuelle egne
"dashboard-only"-kolonner i mål-databasen (f.eks. favoritter/notater lagt til
direkte i Dashboard-prosjektet) røres aldri og overlever dermed en synk.

app.py er ansvarlig for å bygge selve postene (dicts) som sendes inn hit -
denne modulen vet ingenting om Streamlit, regnestykker for yield/kontantstrøm,
eller hvordan FINN-data hentes. Den er bevisst holdt til ren synk-/IO-logikk.
"""

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

CONFIG_PATH = "dashboard_sync_config.json"

DEFAULT_CONFIG = {
    "dashboard_db_path": "",
    "dashboard_data_folder": "",
}

# (kolonnenavn, SQLite-type) for alt denne modulen har lov til å skrive til i
# dashboard-databasens boliger-tabell. Utvid denne listen for å synke flere
# felt - resten av modulen tilpasser seg automatisk (CREATE TABLE, ALTER
# TABLE-migrering, INSERT og UPDATE bruker alle denne samme listen).
MANAGED_COLUMNS = [
    ("finn_ad_id", "TEXT"),
    ("finn_url", "TEXT"),
    ("adresse", "TEXT"),
    ("postnummer", "TEXT"),
    ("by", "TEXT"),
    ("eieform", "TEXT"),
    ("solgt", "INTEGER"),
    ("pris", "INTEGER"),
    ("felleskost", "INTEGER"),
    ("soverom", "INTEGER"),
    ("leie", "INTEGER"),
    ("yield_pct", "REAL"),
    ("netto_etter_lan", "REAL"),
    ("kapitalbehov", "REAL"),
    ("broker_name", "TEXT"),
    ("broker_office", "TEXT"),
    ("broker_profile_url", "TEXT"),
    ("broker_source_domain", "TEXT"),
    ("broker_listing_url", "TEXT"),
    ("salgsoppgave_status", "TEXT"),
    ("salgsoppgave_local_path", "TEXT"),
    ("salgsoppgave_document_url", "TEXT"),
    ("tilstandsrapport_status", "TEXT"),
    ("tilstandsrapport_local_path", "TEXT"),
    ("tilstandsrapport_document_url", "TEXT"),
    ("document_analysis_json", "TEXT"),
    ("image_url", "TEXT"),
    ("image_local_path", "TEXT"),
]

MANAGED_COLUMN_NAMES = [navn for navn, _ in MANAGED_COLUMNS]


# ---------------- KONFIGURASJON ----------------

def load_config() -> dict:
    """Leser lagret konfigurasjon fra disk. Returnerer tomme stier hvis
    filen ikke finnes eller ikke kan leses - kaster aldri unntak."""
    if not os.path.exists(CONFIG_PATH):
        return dict(DEFAULT_CONFIG)

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULT_CONFIG)

    return {
        "dashboard_db_path": data.get("dashboard_db_path", ""),
        "dashboard_data_folder": data.get("dashboard_data_folder", ""),
    }


def save_config(dashboard_db_path: str, dashboard_data_folder: str) -> None:
    data = {
        "dashboard_db_path": (dashboard_db_path or "").strip(),
        "dashboard_data_folder": (dashboard_data_folder or "").strip(),
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------- SKJEMA ----------------

def ensure_schema(dashboard_db_path: str) -> None:
    """Oppretter dashboard-databasefilen og boliger-tabellen hvis de ikke
    finnes, og legger til eventuelle manglende MANAGED_COLUMNS med trygg
    ALTER TABLE (no-op hvis kolonnen allerede finnes)."""
    parent = os.path.dirname(dashboard_db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    conn = sqlite3.connect(dashboard_db_path)
    try:
        c = conn.cursor()

        kolonner_sql = ",\n        ".join(f"{navn} {sqltype}" for navn, sqltype in MANAGED_COLUMNS)
        c.execute(f"""
        CREATE TABLE IF NOT EXISTS boliger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {kolonner_sql},
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_synced_at TEXT
        )
        """)
        conn.commit()

        for navn, sqltype in MANAGED_COLUMNS:
            try:
                c.execute(f"ALTER TABLE boliger ADD COLUMN {navn} {sqltype}")
                conn.commit()
            except sqlite3.OperationalError:
                pass

        try:
            c.execute("ALTER TABLE boliger ADD COLUMN last_synced_at TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _finn_rad_id(c, finn_ad_id: Optional[str], finn_url: Optional[str]) -> Optional[int]:
    if finn_ad_id:
        row = c.execute("SELECT id FROM boliger WHERE finn_ad_id = ?", (finn_ad_id,)).fetchone()
        if row:
            return row[0]

    if finn_url:
        row = c.execute("SELECT id FROM boliger WHERE finn_url = ?", (finn_url,)).fetchone()
        if row:
            return row[0]

    return None


def _sett_inn_rad(c, payload: dict) -> None:
    plassholdere = ", ".join("?" for _ in MANAGED_COLUMN_NAMES)
    c.execute(f"""
    INSERT INTO boliger ({", ".join(MANAGED_COLUMN_NAMES)}, last_synced_at)
    VALUES ({plassholdere}, ?)
    """, [payload.get(k) for k in MANAGED_COLUMN_NAMES] + [_now_iso()])


def _oppdater_rad(c, rad_id: int, payload: dict) -> None:
    """Oppdaterer KUN MANAGED_COLUMNS + last_synced_at. Eventuelle andre
    kolonner som finnes i dashboard-databasens egen boliger-tabell (lagt til
    utenfor denne modulen) blir aldri rørt, og overlever dermed synken."""
    set_klausul = ", ".join(f"{k} = ?" for k in MANAGED_COLUMN_NAMES)
    c.execute(f"""
    UPDATE boliger
    SET {set_klausul}, last_synced_at = ?
    WHERE id = ?
    """, [payload.get(k) for k in MANAGED_COLUMN_NAMES] + [_now_iso(), rad_id])


def _kopier_dokument_hvis_konfigurert(
    source_path: Optional[str], data_folder: Optional[str], finn_ad_id: Optional[str], dokumenttype: str
) -> Optional[str]:
    """Kopierer et dokument til dashboard_data_folder hvis den er konfigurert
    og filen faktisk finnes lokalt. Uten en konfigurert mappe beholdes bare
    den originale stien uendret (Dashboard leser den fra samme filsystem, or
    via image_url/*_document_url for ekstern tilgang)."""
    if not data_folder:
        return source_path

    if not source_path or not os.path.exists(source_path):
        return source_path

    try:
        os.makedirs(data_folder, exist_ok=True)
        ext = os.path.splitext(source_path)[1] or ".pdf"
        filnavn = f"{finn_ad_id or 'ukjent'}_{dokumenttype}{ext}"
        dest_path = os.path.join(data_folder, filnavn)
        shutil.copyfile(source_path, dest_path)
        return dest_path
    except OSError:
        return source_path


def sync_boliger(
    records: List[dict],
    dashboard_db_path: str,
    data_folder: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """Synkroniserer records (liste med dicts, nøkler = MANAGED_COLUMN_NAMES)
    til dashboard-databasen på dashboard_db_path.

    - Oppretter database/tabell/manglende kolonner ved behov (kun når
      dry_run=False - en tørrkjøring skal ikke engang opprette db-filen).
    - Matcher eksisterende rad på finn_ad_id, deretter finn_url.
    - dry_run=True gjør INGEN skriving og INGEN filkopiering - kun en
      opptelling av hva som ville skjedd.

    Returnerer: {"total", "inserted", "updated", "skipped", "errors", "error_details"}
    """
    sammendrag = {
        "total": len(records),
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "error_details": [],
    }

    dashboard_db_path = (dashboard_db_path or "").strip()

    if not dashboard_db_path:
        sammendrag["errors"] = len(records)
        sammendrag["error_details"].append({
            "finn_url": None,
            "error": "Mangler sti til dashboard-database (dashboard_db_path er ikke satt).",
        })
        return sammendrag

    if not dry_run:
        ensure_schema(dashboard_db_path)

    conn = None
    c = None

    # I en tørrkjøring skal vi aldri opprette selve db-filen (sqlite3.connect
    # oppretter filen bare ved å koble til) - så vi kobler oss kun til hvis
    # den allerede finnes, kun for å kunne slå opp eksisterende rader.
    if not dry_run or os.path.exists(dashboard_db_path):
        conn = sqlite3.connect(dashboard_db_path)
        c = conn.cursor()

    try:
        for rec in records:
            finn_url = rec.get("finn_url")
            finn_ad_id = rec.get("finn_ad_id")

            if not finn_url and not finn_ad_id:
                sammendrag["skipped"] += 1
                continue

            try:
                eksisterende_id = _finn_rad_id(c, finn_ad_id, finn_url) if c else None

                payload = dict(rec)

                if not dry_run and data_folder:
                    payload["salgsoppgave_local_path"] = _kopier_dokument_hvis_konfigurert(
                        rec.get("salgsoppgave_local_path"), data_folder, finn_ad_id, "salgsoppgave"
                    )
                    payload["tilstandsrapport_local_path"] = _kopier_dokument_hvis_konfigurert(
                        rec.get("tilstandsrapport_local_path"), data_folder, finn_ad_id, "tilstandsrapport"
                    )

                if eksisterende_id:
                    if not dry_run:
                        _oppdater_rad(c, eksisterende_id, payload)
                    sammendrag["updated"] += 1
                else:
                    if not dry_run:
                        _sett_inn_rad(c, payload)
                    sammendrag["inserted"] += 1

            except Exception as e:
                sammendrag["errors"] += 1
                sammendrag["error_details"].append({"finn_url": finn_url, "error": str(e)})

        if conn and not dry_run:
            conn.commit()
    finally:
        if conn:
            conn.close()

    return sammendrag
