import atexit
import json
import os
import re
import base64
import sqlite3
import time
import requests
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from gmail_healthcheck import kjor_gmail_helsesjekk, hent_gmail_status
from salgsoppgave_downloader import (
    hent_salgsoppgave,
    extract_finn_ad_id,
    extract_broker_info,
    is_downloaded_status,
    DOWNLOADED_STATUSES,
    RESOLVED_STATUSES,
    STATUS_LINK_FOUND_NOT_PDF,
    STATUS_INVALID_PDF_RESPONSE,
    STATUS_LISTING_SOLD_OR_INACTIVE,
    STATUS_DOWNLOADED_VALID_PDF,
    get_default_headers,
    REQUEST_TIMEOUT,
)
from broker_site_fallback import find_and_download_from_broker_site
from broker_document_parser import (
    download_broker_documents,
    velg_primaert_dokument,
    er_megler_side_solgt_eller_inaktiv,
    PRIMARY_DOCUMENT_TYPES,
    CONDITION_REPORT_DOCUMENT_TYPES,
)
from document_parser import analyze_salgsoppgave_pdf, COMPONENT_ORDER, COMPONENT_DISPLAY_NAVN
import dashboard_sync

# Statuser (for både salgsoppgave og tilstandsrapport) som betyr "et dokument
# ble funnet, men ikke lastet ned som en direkte PDF" - fortsatt et FUNN, bare
# noe brukeren må åpne selv (typisk en digital salgsoppgave hos Aktiv o.l.).
DOCUMENT_URL_ONLY_STATUSES = (STATUS_LINK_FOUND_NOT_PDF, STATUS_INVALID_PDF_RESPONSE)


st.set_page_config(page_title="Boligscanner", layout="wide")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

conn = sqlite3.connect("boliger.db", check_same_thread=False)
c = conn.cursor()


# ---------------- AUTOMATISK DAGLIG GMAIL-HELSESJEKK ----------------
# Starter en bakgrunnsplanlegger som kjører kjor_gmail_helsesjekk() hver dag
# kl. 12:00 (Europe/Oslo), uten at noen trenger å trykke på en knapp.
# st.cache_resource sikrer at planleggeren kun startes én gang per prosess,
# selv om Streamlit kjører hele skriptet på nytt ved hver interaksjon.
# Dette fungerer også når appen kjører i Docker/på en server (Railway,
# Render o.l.), så lenge prosessen holdes i live - helsesjekken krever
# aldri en interaktiv nettleser-innlogging.
@st.cache_resource
def _start_gmail_health_scheduler():
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(
        kjor_gmail_helsesjekk,
        trigger="cron",
        hour=12,
        minute=0,
        timezone=ZoneInfo("Europe/Oslo"),
        id="gmail_daglig_helsesjekk",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.start()
    atexit.register(lambda: scheduler.shutdown(wait=False))
    return scheduler


_start_gmail_health_scheduler()


# ---------------- GMAIL-STATUS I SIDEBAR ----------------

_gmail_status = hent_gmail_status()

st.sidebar.header("Gmail API-status")

if _gmail_status is None:
    st.sidebar.info("Ingen helsesjekk kjørt enda. Kjøres automatisk hver dag kl. 12:00.")
elif _gmail_status["status"] == "ok":
    st.sidebar.success(f"Gmail API: OK\n\nSist bekreftet: {_gmail_status['checked_at']}")
else:
    st.sidebar.error(
        f"Gmail API: Feilet\n\n"
        f"Feiltype: {_gmail_status['error_type']}\n\n"
        f"Sist vellykket: {_gmail_status['last_success_at'] or 'aldri'}"
    )
    if _gmail_status["krever_reautentisering"]:
        st.sidebar.warning(
            "⚠️ Gmail-tokenet er ugyldig eller utløpt. Gmail må autentiseres på "
            "nytt: slett token.json og kjør den lokale innloggingsflyten "
            "(f.eks. gmail_test.py) på nytt for å generere et nytt token.json."
        )

if st.sidebar.button("Kjør Gmail-helsesjekk nå"):
    ok, feiltype, melding = kjor_gmail_helsesjekk()
    if ok:
        st.sidebar.success("Gmail API: OK")
    else:
        st.sidebar.error(f"Gmail API feilet: {feiltype} - {melding}")
    st.rerun()


# ---------------- DATABASE ----------------

c.execute("""
CREATE TABLE IF NOT EXISTS boliger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT,
    adresse TEXT,
    postnummer TEXT,
    by TEXT,
    pris INTEGER,
    felleskost INTEGER,
    soverom INTEGER,
    leie INTEGER,
    strom INTEGER,
    kommunale INTEGER,
    andre INTEGER,
    image_url TEXT,
    eieform TEXT,
    solgt INTEGER,
    alerted_rules TEXT,
    lat REAL,
    lon REAL,
    nearest_school TEXT,
    nearest_school_km REAL,
    nearest_school_min REAL
)
""")
conn.commit()

for col, coltype in [
    ("url", "TEXT"),
    ("image_url", "TEXT"),
    ("eieform", "TEXT"),
    ("solgt", "INTEGER"),
    ("alerted_rules", "TEXT"),
    ("lat", "REAL"),
    ("lon", "REAL"),
    ("nearest_school", "TEXT"),
    ("nearest_school_km", "REAL"),
    ("nearest_school_min", "REAL"),
    ("broker_name", "TEXT"),
    ("broker_office", "TEXT"),
    ("broker_profile_url", "TEXT"),
    ("broker_source_domain", "TEXT"),
    ("salgsoppgave_status", "TEXT"),
    ("salgsoppgave_local_path", "TEXT"),
    ("broker_listing_url", "TEXT"),
    ("salgsoppgave_source", "TEXT"),
    ("salgsoppgave_source_detail", "TEXT"),
    ("tilstandsrapport_local_path", "TEXT"),
    ("downloaded_documents_json", "TEXT"),
    ("salgsoppgave_document_url", "TEXT"),
    ("tilstandsrapport_document_url", "TEXT"),
    ("salgsoppgave_download_status", "TEXT"),
    ("tilstandsrapport_download_status", "TEXT"),
    ("document_analysis_json", "TEXT"),
    ("document_analysis_source_path", "TEXT"),
]:
    try:
        c.execute(f"ALTER TABLE boliger ADD COLUMN {col} {coltype}")
        conn.commit()
    except sqlite3.OperationalError:
        pass

c.execute("""
CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    bolig_id INTEGER,
    bolig_url TEXT,
    filter_name TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

c.execute("""
CREATE TABLE IF NOT EXISTS leiepriser (
    by TEXT PRIMARY KEY,
    leie_per_rom INTEGER NOT NULL
)
""")
conn.commit()

c.execute("""
CREATE TABLE IF NOT EXISTS salgsoppgave_forsok (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    finn_url TEXT,
    finn_ad_id TEXT,
    found_document_url TEXT,
    local_pdf_path TEXT,
    status TEXT,
    attempted_at TEXT,
    error_message TEXT
)
""")
conn.commit()

for col, coltype in [
    ("broker_name", "TEXT"),
    ("broker_office", "TEXT"),
    ("broker_profile_url", "TEXT"),
    ("broker_source_domain", "TEXT"),
    ("broker_listing_url", "TEXT"),
    ("salgsoppgave_source", "TEXT"),
    ("salgsoppgave_source_detail", "TEXT"),
    ("tilstandsrapport_local_path", "TEXT"),
    ("downloaded_documents_json", "TEXT"),
    ("salgsoppgave_document_url", "TEXT"),
    ("tilstandsrapport_document_url", "TEXT"),
    ("salgsoppgave_download_status", "TEXT"),
    ("tilstandsrapport_download_status", "TEXT"),
]:
    try:
        c.execute(f"ALTER TABLE salgsoppgave_forsok ADD COLUMN {col} {coltype}")
        conn.commit()
    except sqlite3.OperationalError:
        pass
# ---------------- HELPERS ----------------

def clean_number(text):
    if not text:
        return 0
    numbers = re.sub(r"[^\d]", "", str(text))
    return int(numbers) if numbers else 0


def find_value(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def nok(x):
    return f"{int(round(x)):,}".replace(",", " ") + " kr"


def get_leie_per_rom(by):
    if not by:
        return 0

    row = c.execute("""
    SELECT leie_per_rom
    FROM leiepriser
    WHERE LOWER(by) = LOWER(?)
    """, (by.strip(),)).fetchone()

    if row:
        return row[0]

    return 0


def estimate_rent(soverom, by=""):
    leie_per_rom = get_leie_per_rom(by)

    if soverom <= 0:
        return 0

    if leie_per_rom <= 0:
        return 0

    return soverom * leie_per_rom


def annuitet_mnd(lan, rente_prosent, ar):
    if lan <= 0 or ar <= 0:
        return 0
    r = rente_prosent / 100 / 12
    n = ar * 12
    if r == 0:
        return lan / n
    return lan * (r * (1 + r) ** n) / ((1 + r) ** n - 1)


def kapitalberegning(pris, ek_prosent, bruk_makslaan, maks_laan, eieform):
    min_ek = pris * ek_prosent / 100
    onsket_lan = pris - min_ek

    if bruk_makslaan and onsket_lan > maks_laan:
        lan = maks_laan
        ekstra_ek = onsket_lan - maks_laan
    else:
        lan = onsket_lan
        ekstra_ek = 0

    dokumentavgift = pris * 0.025 if eieform == "Selveier" else 0
    tinglysing = 1170
    ek_til_bolig = min_ek + ekstra_ek
    omkostninger = dokumentavgift + tinglysing
    kapitalbehov = ek_til_bolig + omkostninger

    return {
        "lan": lan,
        "ekstra_ek": ekstra_ek,
        "ek_til_bolig": ek_til_bolig,
        "dokumentavgift": dokumentavgift,
        "tinglysing": tinglysing,
        "omkostninger": omkostninger,
        "kapitalbehov": kapitalbehov,
    }


def rentehopp_toleranse(netto_for_lan, lan, rente, ar):
    hopp = 0
    test_rente = rente

    while hopp < 200:
        termin = annuitet_mnd(lan, test_rente, ar)
        if netto_for_lan - termin < 0:
            return hopp
        hopp += 1
        test_rente += 0.25

    return hopp


def beregn_tall(pris, felleskost, leie, strom, kommunale, andre, eieform, rente, ar, ek_prosent, bruk_makslaan, maks_laan):
    kostnader = felleskost + strom + kommunale + andre
    netto_for_lan = leie - kostnader
    kapital = kapitalberegning(pris, ek_prosent, bruk_makslaan, maks_laan, eieform)
    termin = annuitet_mnd(kapital["lan"], rente, ar)
    netto_etter_lan = netto_for_lan - termin
    yield_pct = (leie * 12 / pris * 100) if pris > 0 else 0
    hopp = rentehopp_toleranse(netto_for_lan, kapital["lan"], rente, ar)

    return netto_for_lan, termin, netto_etter_lan, yield_pct, hopp, kapital


# ---------------- SLACK ----------------

def get_slack_webhook():
    try:
        return st.secrets["SLACK_WEBHOOK_URL"]
    except Exception:
        return ""


def send_slack_message(text):
    webhook_url = get_slack_webhook()

    if not webhook_url:
        return False, "Mangler SLACK_WEBHOOK_URL i .streamlit/secrets.toml"

    try:
        res = requests.post(webhook_url, json={"text": text}, timeout=10)
        if res.status_code >= 400:
            return False, f"Slack-feil: {res.status_code} - {res.text}"
        return True, "Varsel sendt"
    except Exception as e:
        return False, str(e)


# ---------------- OPENROUTESERVICE ----------------

def get_ors_key():
    try:
        return st.secrets["ORS_API_KEY"]
    except Exception:
        return ""


def geocode_address(address):
    api_key = get_ors_key()

    if not api_key:
        return None, None, "Mangler ORS_API_KEY i .streamlit/secrets.toml"

    url = "https://api.openrouteservice.org/geocode/search"

    try:
        search_texts = [
            address,
            address.replace(", Norge", ""),
            address.replace("Norge", "Norway"),
        ]

        last_error = None

        for text in search_texts:
            params = {
                "api_key": api_key,
                "text": text,
                "size": 1,
                "boundary.country": "NO",
            }

            res = requests.get(url, params=params, timeout=15)
            data = res.json()

            if res.status_code >= 400:
                last_error = f"ORS geocoding-feil: {res.status_code} - {data}"
                continue

            features = data.get("features", [])

            if features:
                coords = features[0]["geometry"]["coordinates"]
                lon, lat = coords
                return lat, lon, None

        return None, None, last_error or "Fant ingen geocoding-treff"

    except Exception as e:
        return None, None, str(e)
    

    


def get_distance(lat1, lon1, lat2, lon2):
    api_key = get_ors_key()

    if not api_key:
        return None, None, "Mangler ORS_API_KEY i .streamlit/secrets.toml"

    url = "https://api.openrouteservice.org/v2/directions/driving-car"

    headers = {
        "Authorization": api_key,
        "Content-Type": "application/json"
    }

    body = {
        "coordinates": [
            [lon1, lat1],
            [lon2, lat2]
        ]
    }

    try:
        res = requests.post(url, json=body, headers=headers, timeout=15)
        data = res.json()

        if res.status_code >= 400:
            return None, None, f"ORS distance-feil: {res.status_code} - {data}"

        distance = data["routes"][0]["summary"]["distance"] / 1000
        duration = data["routes"][0]["summary"]["duration"] / 60
        return distance, duration, None

    except Exception as e:
        return None, None, str(e)



SKOLER = {
    "Gjøvik": [
        {"navn": "NTNU Gjøvik", "lat": 60.7885, "lon": 10.6809},
    ],

    "Kristiansund": [
        {"navn": "NTNU Kristiansund", "lat": 63.1114, "lon": 7.7319},
        {"navn": "HVL Kristiansund", "lat": 63.1114, "lon": 7.7319},
    ],

    "Volda": [
        {"navn": "Høgskulen i Volda", "lat": 62.1476, "lon": 6.0741},
    ],

    "Ålesund": [
        {"navn": "NTNU Ålesund", "lat": 62.4720, "lon": 6.2360},
    ],

    "Bodø": [
        {"navn": "Nord universitet Bodø", "lat": 67.2822, "lon": 14.5583},
    ],

    "Haugesund": [
        {"navn": "HVL Haugesund", "lat": 59.4120, "lon": 5.2750},
    ],

    "Stavanger": [
        {"navn": "UiS", "lat": 58.9700, "lon": 5.7314},
        {"navn": "BI Stavanger", "lat": 58.9810, "lon": 5.7270},
        {"navn": "VID Stavanger", "lat": 58.9700, "lon": 5.7314},
    ],

    "Porsgrunn": [
        {"navn": "USN Porsgrunn", "lat": 59.1352, "lon": 9.6335},
    ],

    "Levanger": [
        {"navn": "Nord universitet Levanger", "lat": 63.7425, "lon": 11.3078},
    ],

    "Steinkjer": [
        {"navn": "Nord universitet Steinkjer", "lat": 64.0135, "lon": 11.4939},
    ],

    "Trondheim": [
        {"navn": "NTNU Gløshaugen", "lat": 63.4195, "lon": 10.4021},
    ],

    "Fredrikstad": [
        {"navn": "Høgskolen i Østfold", "lat": 59.2000, "lon": 10.9417},
    ],

    # 🔥 NYE (det du manglet)
    "Grimstad": [
        {"navn": "UiA Grimstad", "lat": 58.3364, "lon": 8.5833},
    ],

    "Kristiansand": [
        {"navn": "UiA Kristiansand", "lat": 58.1578, "lon": 8.0019},
        {"navn": "Ansgar Høyskole", "lat": 58.2045, "lon": 8.0838},
    ],
}

def finn_naermeste_skole(by, adresse, postnummer):
    relevante_skoler = SKOLER.get(by, [])

    if not relevante_skoler:
        return {
            "lat": None,
            "lon": None,
            "nearest_school": "",
            "nearest_school_km": None,
            "nearest_school_min": None,
        }

    full_adresse = f"{adresse}, {postnummer} {by}, Norge"
    lat, lon, geo_error = geocode_address(full_adresse)

    if geo_error or not lat or not lon:
        return {
            "lat": None,
            "lon": None,
            "nearest_school": "",
            "nearest_school_km": None,
            "nearest_school_min": None,
        }

    beste = None

    for skole in relevante_skoler:
        dist, tid, dist_error = get_distance(lat, lon, skole["lat"], skole["lon"])

        if dist_error or dist is None:
            continue

        if beste is None or dist < beste["nearest_school_km"]:
            beste = {
                "lat": lat,
                "lon": lon,
                "nearest_school": skole["navn"],
                "nearest_school_km": dist,
                "nearest_school_min": tid,
            }

    if beste:
        return beste

    return {
        "lat": lat,
        "lon": lon,
        "nearest_school": "",
        "nearest_school_km": None,
        "nearest_school_min": None,
    }


# ---------------- ALERT LOGIKK ----------------

def get_alert_key(alert_name, min_yield_alert, min_netto_alert, max_pris_alert, min_soverom_alert, valgte_omrader_alert, max_skole_km_alert):
    omrader_key = "-".join(sorted(valgte_omrader_alert)) if valgte_omrader_alert else "alle"
    return f"{alert_name}|yield{min_yield_alert}|netto{min_netto_alert}|pris{max_pris_alert}|soverom{min_soverom_alert}|skolekm{max_skole_km_alert}|{omrader_key}"


def bolig_matcher_alert(bolig, yield_pct, netto_etter_lan, alert_settings):
    if bolig.get("solgt", 0) and not alert_settings["varsle_solgte"]:
        return False

    if alert_settings["valgte_omrader_alert"]:
        if bolig["by"] not in alert_settings["valgte_omrader_alert"]:
            return False

    if yield_pct < alert_settings["min_yield_alert"]:
        return False

    if netto_etter_lan < alert_settings["min_netto_alert"]:
        return False

    if alert_settings["max_pris_alert"] > 0 and bolig["pris"] > alert_settings["max_pris_alert"]:
        return False

    if bolig["soverom"] < alert_settings["min_soverom_alert"]:
        return False

    if alert_settings.get("max_skole_km_alert", 0) > 0:
        km = bolig.get("nearest_school_km")
        if km is None:
            return False
        if km > alert_settings["max_skole_km_alert"]:
            return False

    return True


def maybe_send_slack_alert(bolig_id, bolig, alert_settings, rente, nedbetaling_ar, ek_prosent, bruk_makslaan, maks_laan):
    if not alert_settings["slack_alerts_on"]:
        return False, "Slack-varsling er av"

    _, termin, netto_etter_lan, yield_pct, hopp, kapital = beregn_tall(
        bolig["pris"],
        bolig["felleskost"],
        bolig["leie"],
        bolig["strom"],
        bolig["kommunale"],
        bolig["andre"],
        bolig["eieform"],
        rente,
        nedbetaling_ar,
        ek_prosent,
        bruk_makslaan,
        maks_laan,
    )

    if not bolig_matcher_alert(bolig, yield_pct, netto_etter_lan, alert_settings):
        return False, "Matcher ikke filter"

    alert_key = alert_settings["alert_key"]

    row = c.execute("SELECT alerted_rules FROM boliger WHERE id=?", (bolig_id,)).fetchone()
    alerted_rules = row[0] if row and row[0] else ""

    if alert_key in alerted_rules:
        return False, "Allerede varslet for dette filteret"

    skole_text = "Ukjent"
    if bolig.get("nearest_school_km") is not None:
        skole_text = (
            f"{bolig.get('nearest_school', '')} – "
            f"{bolig.get('nearest_school_km'):.2f} km / "
            f"ca. {bolig.get('nearest_school_min'):.0f} min med bil"
        )

    break_even_leie = bolig["leie"] - netto_etter_lan

    text = f"""
🏠 *Ny bolig matcher filteret: {alert_settings["alert_name"]}*

📍 *Adresse:* {bolig["adresse"]}
🌍 *Område:* {bolig["postnummer"]} {bolig["by"]}

💰 *Kjøpspris:* {nok(bolig["pris"])}
🏦 *Kapitalbehov:* {nok(kapital["kapitalbehov"])}
📉 *Break-even leie:* {nok(break_even_leie)} / mnd

📊 *Estimatorer*
• Estimert leieinntekt: {nok(bolig["leie"])} / mnd
• Yield: {yield_pct:.2f} %
• Netto kontantstrøm: {nok(netto_etter_lan)} / mnd
• Tåler renteøkninger: {hopp} x 0,25 %-poeng

🎓 *Nærmeste universitet/skole:* {skole_text}

🔗 {bolig["url"]}
""".strip()

    ok, msg = send_slack_message(text)

    if ok: 
        nye_alerted_rules = (alerted_rules + "," + alert_key).strip(",")
        c.execute("UPDATE boliger SET alerted_rules=? WHERE id=?", (nye_alerted_rules, bolig_id))
        conn.commit()
        
        log_event("slack_varsel", bolig_id, bolig["url"], alert_settings["alert_name"])

    return ok, msg


# ---------------- FINN SCRAPER ----------------

def scrape_finn(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(url, headers=headers, timeout=10)
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)

    solgt_match = re.search(
        r"\bSolgt\b\s+.{0,250}?,\s*\d{4}\s+[A-ZÆØÅa-zæøå\s\-]+",
        text
    )
    solgt = 1 if solgt_match else 0

    image_url = ""
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if src and "finncdn" in src:
            image_url = src
            break

    full_adresse = find_value([
        r"Kart\s+(.+?,\s*\d{4}\s+[A-ZÆØÅa-zæøå\-]+)",
        r"(.+?,\s*\d{4}\s+[A-ZÆØÅa-zæøå\-]+)"
    ], text)

    full_adresse = full_adresse.replace("Kart", "").strip()
    match = re.search(r"(.+?),\s*(\d{4})\s+(.+)", full_adresse)

    if match:
        adresse = match.group(1)
        postnummer = match.group(2)
        by = match.group(3)
    else:
        adresse = full_adresse
        postnummer = ""
        by = ""

    pris = clean_number(find_value([
    r"Prisantydning\s*([\d\s]{5,})\s*kr",
    r"Totalpris\s*([\d\s]{5,})\s*kr",
], text))

    felleskost = clean_number(find_value([
        r"Felleskost.*?([\d\s]+)",
    ], text))

    soverom = clean_number(find_value([
        r"Soverom\s*(\d+)",
        r"(\d+)\s*soverom",
    ], text))


    eieform = "Selveier"

    try:
        broker_info = extract_broker_info(r.text, url)
    except Exception:
        broker_info = {
            "broker_name": None,
            "broker_office": None,
            "broker_profile_url": None,
            "broker_source_domain": None,
        }

    return {
        "url": url,
        "adresse": adresse,
        "postnummer": postnummer,
        "by": by,
        "pris": pris,
        "felleskost": felleskost,
        "soverom": soverom,
        "leie": estimate_rent(soverom, by),
        "strom": 1500,
        "kommunale": 1000,
        "andre": 0,
        "image_url": image_url,
        "eieform": eieform,
        "solgt": solgt,
        "broker_name": broker_info["broker_name"],
        "broker_office": broker_info["broker_office"],
        "broker_profile_url": broker_info["broker_profile_url"],
        "broker_source_domain": broker_info["broker_source_domain"],
    }


# ---------------- GMAIL ----------------

def get_gmail_service():
    import os

    creds = None

    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)

        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_email_body(payload):
    if "parts" in payload:
        for part in payload["parts"]:
            body = get_email_body(part)
            if body:
                return body

    data = payload.get("body", {}).get("data")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    return ""


def hent_finn_lenker_fra_gmail(max_results=100):
    service = get_gmail_service()

    results = service.users().messages().list(
        userId="me",
        q="label:finn-boligvarsler",
        maxResults=max_results
    ).execute()

    messages = results.get("messages", [])
    links = []

    for msg in messages:
        message = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="full"
        ).execute()

        body = get_email_body(message["payload"])
        found = re.findall(r"https://www\.finn\.no/\d+[^\s\"<>]*", body)

        for link in found:
            link = link.replace("&amp;", "&")
            link = link.split("?")[0]

            if link not in links:
                links.append(link)

    return links


# ---------------- DATABASE FUNKSJONER ----------------

def url_exists(url):
    c.execute("SELECT id FROM boliger WHERE url=?", (url,))
    return c.fetchone() is not None

def log_event(event_type, bolig_id=None, bolig_url="", filter_name=""):
    c.execute("""
    INSERT INTO event_log (event_type, bolig_id, bolig_url, filter_name)
    VALUES (?, ?, ?, ?)
    """, (event_type, bolig_id, bolig_url, filter_name))
    conn.commit()


def lagre_salgsoppgave_forsok(result):
    downloaded_documents_json = json.dumps(result.downloaded_documents, ensure_ascii=False)

    c.execute("""
    INSERT INTO salgsoppgave_forsok (
        finn_url, finn_ad_id, found_document_url, local_pdf_path,
        status, attempted_at, error_message,
        broker_name, broker_office, broker_profile_url, broker_source_domain,
        broker_listing_url, salgsoppgave_source, salgsoppgave_source_detail,
        tilstandsrapport_local_path, downloaded_documents_json,
        salgsoppgave_document_url, tilstandsrapport_document_url,
        salgsoppgave_download_status, tilstandsrapport_download_status
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result.finn_url,
        result.finn_ad_id,
        result.found_document_url,
        result.local_pdf_path,
        result.status,
        result.attempted_at,
        result.error_message,
        result.broker_name,
        result.broker_office,
        result.broker_profile_url,
        result.broker_source_domain,
        result.broker_listing_url,
        result.salgsoppgave_source,
        result.salgsoppgave_source_detail,
        result.tilstandsrapport_local_path,
        downloaded_documents_json,
        result.found_document_url,
        result.tilstandsrapport_document_url,
        result.status,
        result.tilstandsrapport_status,
    ))
    conn.commit()

    bolig_id = oppdater_bolig_broker_og_salgsoppgave(result.finn_ad_id, result.finn_url, result)

    # Analyser salgsoppgaven med en gang en ekte PDF er lastet ned - dette
    # dekker automatisk alle flytene (enkelt-knapp, batch, backfill) siden de
    # alle går via denne funksjonen.
    if bolig_id and result.local_pdf_path:
        analyser_og_lagre_dokumentanalyse(bolig_id, result.local_pdf_path)

    return bolig_id


def finn_bolig_id_for_finn_annonse(finn_ad_id, finn_url):
    """Finner id i boliger-tabellen for en FINN-annonse.

    Prøver finn_ad_id først (robust mot at url-varianter/spørrestrenger endrer seg),
    faller tilbake til eksakt url-match. Brukes for å oppdatere riktig eksisterende
    rad uten å risikere å opprette duplikater.
    """
    if finn_ad_id:
        alle = c.execute("""
        SELECT id, url FROM boliger WHERE url IS NOT NULL AND url != ''
        """).fetchall()

        for rid, rurl in alle:
            if extract_finn_ad_id(rurl) == finn_ad_id:
                return rid

    if finn_url:
        row = c.execute("SELECT id FROM boliger WHERE url = ?", (finn_url,)).fetchone()
        if row:
            return row[0]

    return None


def oppdater_bolig_broker_og_salgsoppgave(finn_ad_id, finn_url, result):
    """Oppdaterer megler- og salgsoppgave-felter på den matchende bolig-raden.

    Regraderer aldri: hvis raden allerede har en ekte nedlastet PDF for
    salgsoppgave og/eller tilstandsrapport fra et tidligere forsøk, og DETTE
    forsøket bare fant en digital lenke (eller ingenting), beholdes det
    forrige, bedre resultatet for akkurat det dokumentet uendret. De to
    dokumenttypene vurderes helt uavhengig av hverandre.

    Returnerer bolig_id hvis en rad ble funnet og oppdatert, ellers None.
    """
    bolig_id = finn_bolig_id_for_finn_annonse(finn_ad_id, finn_url)

    if not bolig_id:
        return None

    eksisterende = c.execute("""
    SELECT salgsoppgave_status, salgsoppgave_local_path, salgsoppgave_document_url,
           tilstandsrapport_download_status, tilstandsrapport_local_path, tilstandsrapport_document_url
    FROM boliger WHERE id = ?
    """, (bolig_id,)).fetchone()

    (
        eksisterende_salgsoppgave_status, eksisterende_salgsoppgave_path, eksisterende_salgsoppgave_url,
        eksisterende_tilstand_status, eksisterende_tilstand_path, eksisterende_tilstand_url,
    ) = eksisterende if eksisterende else (None, None, None, None, None, None)

    ny_salgsoppgave_status = result.status
    ny_salgsoppgave_path = result.local_pdf_path
    ny_salgsoppgave_url = result.found_document_url

    if eksisterende_salgsoppgave_status in DOWNLOADED_STATUSES and ny_salgsoppgave_status not in DOWNLOADED_STATUSES:
        ny_salgsoppgave_status = eksisterende_salgsoppgave_status
        ny_salgsoppgave_path = eksisterende_salgsoppgave_path
        ny_salgsoppgave_url = eksisterende_salgsoppgave_url

    ny_tilstand_status = result.tilstandsrapport_status
    ny_tilstand_path = result.tilstandsrapport_local_path
    ny_tilstand_url = result.tilstandsrapport_document_url

    if eksisterende_tilstand_status == STATUS_DOWNLOADED_VALID_PDF and ny_tilstand_status != STATUS_DOWNLOADED_VALID_PDF:
        ny_tilstand_status = eksisterende_tilstand_status
        ny_tilstand_path = eksisterende_tilstand_path
        ny_tilstand_url = eksisterende_tilstand_url

    # Ikke overskriv med en tom liste hvis dette forsøket ikke fant noen nye
    # dokumenter - da beholdes det som ble lastet ned i et tidligere forsøk.
    downloaded_documents_json = (
        json.dumps(result.downloaded_documents, ensure_ascii=False)
        if result.downloaded_documents else None
    )

    c.execute("""
    UPDATE boliger
    SET broker_name = COALESCE(?, broker_name),
        broker_office = COALESCE(?, broker_office),
        broker_profile_url = COALESCE(?, broker_profile_url),
        broker_source_domain = COALESCE(?, broker_source_domain),
        broker_listing_url = COALESCE(?, broker_listing_url),
        salgsoppgave_status = ?,
        salgsoppgave_local_path = ?,
        salgsoppgave_document_url = ?,
        salgsoppgave_download_status = ?,
        salgsoppgave_source = ?,
        salgsoppgave_source_detail = ?,
        tilstandsrapport_local_path = ?,
        tilstandsrapport_document_url = ?,
        tilstandsrapport_download_status = ?,
        downloaded_documents_json = COALESCE(?, downloaded_documents_json)
    WHERE id = ?
    """, (
        result.broker_name,
        result.broker_office,
        result.broker_profile_url,
        result.broker_source_domain,
        result.broker_listing_url,
        ny_salgsoppgave_status,
        ny_salgsoppgave_path,
        ny_salgsoppgave_url,
        ny_salgsoppgave_status,
        result.salgsoppgave_source,
        result.salgsoppgave_source_detail,
        ny_tilstand_path,
        ny_tilstand_url,
        ny_tilstand_status,
        downloaded_documents_json,
        bolig_id,
    ))
    conn.commit()

    return bolig_id


def analyser_og_lagre_dokumentanalyse(bolig_id, local_pdf_path, force=False):
    """Kjører document_parser på salgsoppgave-PDF-en og lagrer resultatet som
    document_analysis_json på bolig-raden.

    Parser aldri samme fil to ganger: hvis document_analysis_source_path
    allerede peker på nøyaktig denne filstien, hoppes analysen over med
    mindre force=True er satt eksplisitt (f.eks. fra en "Analyser på nytt"-knapp).
    """
    if not local_pdf_path or not os.path.exists(local_pdf_path):
        return False

    if not force:
        row = c.execute(
            "SELECT document_analysis_source_path FROM boliger WHERE id = ?", (bolig_id,)
        ).fetchone()
        if row and row[0] == local_pdf_path:
            return False

    analyse = analyze_salgsoppgave_pdf(local_pdf_path)
    analyse_json = json.dumps(analyse, ensure_ascii=False) if analyse else None

    c.execute("""
    UPDATE boliger
    SET document_analysis_json = ?, document_analysis_source_path = ?
    WHERE id = ?
    """, (analyse_json, local_pdf_path, bolig_id))
    conn.commit()

    return True


def _hent_meglerside_trygt(url):
    """Henter en meglerside med samme høflige innstillinger som resten av
    modulen. Returnerer None (aldri unntak) ved nettverksfeil/4xx/5xx."""
    try:
        resp = requests.get(url, headers=get_default_headers(), timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        return None

    if resp.status_code >= 400:
        return None

    return resp


def _anvend_broker_dokumenter(result, documents, kilde_beskrivelse):
    """Fyller inn salgsoppgave-/tilstandsrapport-feltene på result basert på
    dokumentene funnet på en godtatt meglerside.

    Både salgsoppgave og tilstandsrapport behandles uavhengig av hverandre -
    en digital/fil-proxy-lenke telles som "funnet" selv uten nedlastet PDF
    (result.status/tilstandsrapport_status = document_link_found_but_not_direct_pdf),
    og document_url lagres uansett slik at brukeren kan åpne den manuelt.
    """
    salgsoppgave_doc = velg_primaert_dokument(documents, PRIMARY_DOCUMENT_TYPES)
    tilstand_doc = velg_primaert_dokument(documents, CONDITION_REPORT_DOCUMENT_TYPES)

    result.downloaded_documents = [doc.as_dict() for doc in documents if doc.local_path]

    if tilstand_doc:
        result.tilstandsrapport_status = tilstand_doc.download_status
        if tilstand_doc.download_status == STATUS_DOWNLOADED_VALID_PDF:
            result.tilstandsrapport_local_path = tilstand_doc.local_path
        else:
            result.tilstandsrapport_document_url = tilstand_doc.url
    else:
        result.tilstandsrapport_status = "document_not_found"

    if salgsoppgave_doc:
        result.salgsoppgave_source = "broker_site"
        result.found_document_url = salgsoppgave_doc.url

        if salgsoppgave_doc.download_status == STATUS_DOWNLOADED_VALID_PDF:
            result.status = "found_from_broker_site"
            result.local_pdf_path = salgsoppgave_doc.local_path
            result.salgsoppgave_source_detail = f"Salgsoppgave lastet ned {kilde_beskrivelse}."
        elif salgsoppgave_doc.download_status == STATUS_LINK_FOUND_NOT_PDF:
            result.status = STATUS_LINK_FOUND_NOT_PDF
            result.salgsoppgave_source_detail = (
                f"Salgsoppgave funnet {kilde_beskrivelse}, men må åpnes som digital salgsoppgave "
                "(ikke en direkte PDF-fil)."
            )
        else:
            result.status = STATUS_INVALID_PDF_RESPONSE
            result.salgsoppgave_source_detail = (
                f"Fant en dokumentlenke {kilde_beskrivelse}, men innholdet så ikke ut som en gyldig PDF."
            )
    else:
        result.status = "not_found"
        result.salgsoppgave_source = "not_found"
        result.salgsoppgave_source_detail = (
            f"Ingen salgsoppgave/prospekt funnet {kilde_beskrivelse}."
            + (" Fant derimot en tilstandsrapport/takst." if tilstand_doc else "")
        )


def hent_salgsoppgave_med_broker_fallback(finn_url, adresse="", postnummer="", by=""):
    """Prøver FINN-annonsen først (hent_salgsoppgave). Hvis salgsoppgave ikke ble
    funnet der og annonsen fortsatt er aktiv, følges denne foretrukne rekkefølgen:

    1. FINN-PDF direkte (hent_salgsoppgave).
    2. Hvis FINN-annonsen har en direkte lenke til meglerens boligside (typisk
       knappen "Se komplett salgsoppgave"), følges DEN lenken. Den garanterer at
       vi havner på riktig eiendom, så broker_listing_url lagres med en gang -
       selv om dokumentsøket på meglersiden ikke gir noen nedlastbar fil.
    3. KUN hvis FINN-annonsen ikke har en slik direktelenke, brukes den eldre
       adressesøk-fallbacken (broker_site_fallback) som prøver å gjette seg
       frem til riktig bolig hos megleren.

    Et funnet dokument som viser seg å være en digital salgsoppgave/fil-proxy
    (typisk Aktiv) telles som FUNNET (status=document_link_found_but_not_direct_pdf),
    ikke som en feil - se broker_document_parser.py.

    Returnerer (result, debug_info). debug_info er en dict med detaljer om
    FINN-forsøket og megler-søket - brukes av "Test broker fallback"-seksjonen
    for feilsøking/tuning, men er fritt å ignorere for andre kallere.
    """
    result = hent_salgsoppgave(finn_url)

    debug_info = {
        "finn_status": result.status,
        "finn_listing_active": result.finn_listing_active,
        "finn_source_detail": result.salgsoppgave_source_detail,
        "broker_property_link": result.broker_property_link,
        "broker_link_strategy": None,  # "direct_link" | "address_search" | None
        "broker_attempted": False,
        "broker_search_urls_attempted": [],
        "broker_candidate_urls": [],
        "broker_candidate_evaluations": [],
        "broker_doc_links_found": [],
        "broker_skip_reason": None,
    }

    if is_downloaded_status(result.status):
        return result, debug_info

    if result.status == "error":
        return result, debug_info

    # result.status == "not_found" herfra - vurder megler-fallback.
    if not result.finn_listing_active:
        debug_info["broker_skip_reason"] = "Annonsen er ikke aktiv (solgt/avsluttet)."
        result.status = STATUS_LISTING_SOLD_OR_INACTIVE
        result.tilstandsrapport_status = STATUS_LISTING_SOLD_OR_INACTIVE
        result.salgsoppgave_source = "not_found"
        result.salgsoppgave_source_detail = (
            (result.salgsoppgave_source_detail + " " if result.salgsoppgave_source_detail else "")
            + "Annonsen er ikke aktiv (solgt/avsluttet) - hopper over megler-søk."
        ).strip()
        return result, debug_info

    # --- Steg 2: direkte lenke via "Se komplett salgsoppgave" (foretrukket) ---
    if result.broker_property_link:
        debug_info["broker_attempted"] = True
        debug_info["broker_link_strategy"] = "direct_link"

        # Lagres med en gang - selv om siden under skulle vise seg å ikke gi
        # noen nedlastbare dokumenter.
        result.broker_listing_url = result.broker_property_link

        broker_page = _hent_meglerside_trygt(result.broker_property_link)

        if broker_page is None:
            result.salgsoppgave_source = "not_found"
            result.salgsoppgave_source_detail = (
                f"Fant direktelenke til megler ({result.broker_property_link}), "
                "men klarte ikke å hente meglersiden."
            )
            return result, debug_info

        if er_megler_side_solgt_eller_inaktiv(broker_page.text):
            result.status = STATUS_LISTING_SOLD_OR_INACTIVE
            result.tilstandsrapport_status = STATUS_LISTING_SOLD_OR_INACTIVE
            result.salgsoppgave_source = "not_found"
            result.salgsoppgave_source_detail = (
                f"Meglersiden ({result.broker_property_link}) indikerer at boligen er solgt/ikke lenger aktiv."
            )
            return result, debug_info

        documents = download_broker_documents(
            broker_page.text, result.broker_property_link, finn_ad_id=result.finn_ad_id
        )
        debug_info["broker_doc_links_found"] = [
            {"type": doc.doc_type, "url": doc.url, "text": doc.text, "download_status": doc.download_status}
            for doc in documents
        ]

        _anvend_broker_dokumenter(
            result, documents, f"via direktelenke fra FINN ('Se komplett salgsoppgave') til {result.broker_property_link}"
        )

        # Viktig: siden direktelenken fantes, skal IKKE adressesøk-fallbacken
        # forsøkes i tillegg (se punkt 7 i kravspesifikasjonen).
        return result, debug_info

    # --- Steg 3: adressesøk-fallback (kun når direktelenken mangler) ---
    debug_info["broker_link_strategy"] = "address_search"

    if not result.broker_source_domain:
        debug_info["broker_skip_reason"] = "Ingen kjent meglerdomene oppdaget på FINN-annonsen."
        result.salgsoppgave_source = "not_found"
        result.salgsoppgave_source_detail = (
            (result.salgsoppgave_source_detail + " " if result.salgsoppgave_source_detail else "")
            + "Ingen kjent meglerdomene oppdaget - hopper over megler-søk."
        ).strip()
        return result, debug_info

    if not adresse:
        debug_info["broker_skip_reason"] = "Mangler adresse for boligen."
        result.salgsoppgave_source = "not_found"
        result.salgsoppgave_source_detail = (
            (result.salgsoppgave_source_detail + " " if result.salgsoppgave_source_detail else "")
            + "Mangler adresse for boligen - kan ikke søke hos megler."
        ).strip()
        return result, debug_info

    debug_info["broker_attempted"] = True

    try:
        broker_result = find_and_download_from_broker_site(
            broker_domain=result.broker_source_domain,
            adresse=adresse,
            postnummer=postnummer,
            by=by,
            finn_ad_id=result.finn_ad_id,
        )
    except Exception as e:
        debug_info["broker_skip_reason"] = f"Megler-søk feilet uventet: {e}"
        result.salgsoppgave_source = "not_found"
        result.salgsoppgave_source_detail = f"Megler-søk feilet uventet: {e}"
        return result, debug_info

    debug_info["broker_search_urls_attempted"] = broker_result.search_urls_attempted
    debug_info["broker_candidate_urls"] = broker_result.candidate_urls
    debug_info["broker_candidate_evaluations"] = broker_result.candidate_evaluations
    debug_info["broker_doc_links_found"] = broker_result.doc_links_found

    if broker_result.listing_url:
        result.broker_listing_url = broker_result.listing_url

    if broker_result.listing_sold_or_inactive:
        result.status = STATUS_LISTING_SOLD_OR_INACTIVE
        result.tilstandsrapport_status = STATUS_LISTING_SOLD_OR_INACTIVE
        result.salgsoppgave_source = "not_found"
        result.salgsoppgave_source_detail = broker_result.match_detail
        return result, debug_info

    _anvend_broker_dokumenter(result, broker_result.documents, "hos megler (adressesøk)")

    return result, debug_info


# Kolonnerekkefølgen brukt i alle batch-/backfill-resultattabeller.
RESULTAT_TABELL_KOLONNER = [
    "adresse", "finn_ad_id", "broker_source_domain", "broker_listing_url",
    "salgsoppgave_status", "salgsoppgave_document_url", "salgsoppgave_local_path",
    "tilstandsrapport_status", "tilstandsrapport_document_url", "tilstandsrapport_local_path",
    "error_message",
]


def resultat_rad_fra_result(adresse_visning, result):
    return {
        "adresse": adresse_visning,
        "finn_url": result.finn_url,
        "finn_ad_id": result.finn_ad_id,
        "broker_name": result.broker_name,
        "broker_source_domain": result.broker_source_domain,
        "broker_listing_url": result.broker_listing_url,
        "salgsoppgave_status": result.status,
        "salgsoppgave_document_url": result.found_document_url,
        "salgsoppgave_local_path": result.local_pdf_path,
        "tilstandsrapport_status": result.tilstandsrapport_status,
        "tilstandsrapport_document_url": result.tilstandsrapport_document_url,
        "tilstandsrapport_local_path": result.tilstandsrapport_local_path,
        "error_message": result.error_message,
    }


def resultat_rad_uventet_feil(adresse_visning, bolig_url, error_message):
    return {
        "adresse": adresse_visning,
        "finn_url": bolig_url,
        "finn_ad_id": None,
        "broker_name": None,
        "broker_source_domain": None,
        "broker_listing_url": None,
        "salgsoppgave_status": "error",
        "salgsoppgave_document_url": None,
        "salgsoppgave_local_path": None,
        "tilstandsrapport_status": None,
        "tilstandsrapport_document_url": None,
        "tilstandsrapport_local_path": None,
        "error_message": error_message,
    }


def status_bucket(status):
    """Grupperer en salgsoppgave-status for oppsummerings-tellere i batch-kjøringer."""
    if is_downloaded_status(status):
        return "downloaded"
    if status in DOCUMENT_URL_ONLY_STATUSES:
        return "digital"
    if status in ("not_found", STATUS_LISTING_SOLD_OR_INACTIVE):
        return "not_found"
    return "error"


def hent_nylige_boliger_med_url(limit=20):
    return c.execute("""
    SELECT id, url, adresse, postnummer, by
    FROM boliger
    WHERE url IS NOT NULL AND url != ''
    ORDER BY id DESC
    LIMIT ?
    """, (limit,)).fetchall()


def hent_boliger_med_manglende_data(limit=20):
    return c.execute("""
    SELECT id, url, adresse, postnummer, by
    FROM boliger
    WHERE url IS NOT NULL AND url != ''
    AND (
        broker_name IS NULL OR broker_name = ''
        OR broker_source_domain IS NULL OR broker_source_domain = ''
        OR salgsoppgave_status IS NULL OR salgsoppgave_status = ''
        OR salgsoppgave_local_path IS NULL OR salgsoppgave_local_path = ''
    )
    ORDER BY id DESC
    LIMIT ?
    """, (limit,)).fetchall()


DOWNLOADED_STATUSES_SQL = "(" + ", ".join(f"'{s}'" for s in DOWNLOADED_STATUSES) + ")"
RESOLVED_STATUSES_SQL = "(" + ", ".join(f"'{s}'" for s in RESOLVED_STATUSES) + ")"


def hent_alle_boliger_for_full_backfill(force_recheck=False):
    if force_recheck:
        return c.execute("""
        SELECT id, url, adresse, postnummer, by
        FROM boliger
        WHERE url IS NOT NULL AND url != ''
        ORDER BY id ASC
        """).fetchall()

    # Solgt/inaktiv hopper alltid over (uansett broker_name - en solgt megler-
    # side gir aldri mer informasjon ved et nytt forsøk). De andre "ferdige"
    # statusene (inkl. digital salgsoppgave) krever i tillegg at vi har
    # megler-info, slik at en rad uten kjent megler fortsatt forsøkes på nytt.
    return c.execute(f"""
    SELECT id, url, adresse, postnummer, by
    FROM boliger
    WHERE url IS NOT NULL AND url != ''
    AND NOT (
        salgsoppgave_status = '{STATUS_LISTING_SOLD_OR_INACTIVE}'
        OR (
            broker_name IS NOT NULL AND broker_name != ''
            AND salgsoppgave_status IN {RESOLVED_STATUSES_SQL}
        )
    )
    ORDER BY id ASC
    """).fetchall()


SALGSOPPGAVE_FORSOK_KOLONNER = [
    "finn_url", "finn_ad_id", "found_document_url", "local_pdf_path",
    "status", "attempted_at", "error_message",
    "broker_name", "broker_office", "broker_profile_url", "broker_source_domain",
    "broker_listing_url", "salgsoppgave_source", "salgsoppgave_source_detail",
    "tilstandsrapport_local_path", "tilstandsrapport_document_url", "tilstandsrapport_download_status",
]


def hent_siste_vellykkede_salgsoppgave(finn_ad_id, finn_url):
    kolonner = ", ".join(SALGSOPPGAVE_FORSOK_KOLONNER)
    row = None

    if finn_ad_id:
        row = c.execute(f"""
        SELECT {kolonner}
        FROM salgsoppgave_forsok
        WHERE finn_ad_id = ? AND status IN {DOWNLOADED_STATUSES_SQL}
        ORDER BY id DESC LIMIT 1
        """, (finn_ad_id,)).fetchone()

    if not row and finn_url:
        row = c.execute(f"""
        SELECT {kolonner}
        FROM salgsoppgave_forsok
        WHERE finn_url = ? AND status IN {DOWNLOADED_STATUSES_SQL}
        ORDER BY id DESC LIMIT 1
        """, (finn_url,)).fetchone()

    if not row:
        return None

    return dict(zip(SALGSOPPGAVE_FORSOK_KOLONNER, row))


def lagre_bolig(data):
    if url_exists(data["url"]):
        return False, None

    skoledata = finn_naermeste_skole(
        data["by"],
        data["adresse"],
        data["postnummer"]
    )

    c.execute("""
    INSERT INTO boliger (
        url, adresse, postnummer, by, pris, felleskost,
        soverom, leie, strom, kommunale, andre, image_url, eieform, solgt,
        alerted_rules, lat, lon, nearest_school, nearest_school_km, nearest_school_min,
        broker_name, broker_office, broker_profile_url, broker_source_domain
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["url"], data["adresse"], data["postnummer"], data["by"],
        data["pris"], data["felleskost"], data["soverom"], data["leie"],
        data["strom"], data["kommunale"], data["andre"],
        data["image_url"], data["eieform"], data.get("solgt", 0),
        "",
        skoledata["lat"],
        skoledata["lon"],
        skoledata["nearest_school"],
        skoledata["nearest_school_km"],
        skoledata["nearest_school_min"],
        data.get("broker_name"),
        data.get("broker_office"),
        data.get("broker_profile_url"),
        data.get("broker_source_domain"),
    ))
    
    ny_bolig_id = c.lastrowid
    conn.commit()
    log_event("bolig_lagret", ny_bolig_id, data["url"], "")
    return True, ny_bolig_id

def oppdater_solgt_status():
    alle = c.execute("""
    SELECT id, url
    FROM boliger
    WHERE url IS NOT NULL
    AND url != ''
    AND (solgt IS NULL OR solgt = 0)
    """).fetchall()

    oppdatert = 0
    feilet = 0
    nye_solgte = 0

    progress = st.progress(0)

    for i, row in enumerate(alle):
        bolig_id, bolig_url = row

        try:
            ny_data = scrape_finn(bolig_url)
            ny_solgt = ny_data.get("solgt", 0)

            c.execute(
                "UPDATE boliger SET solgt=? WHERE id=?",
                (ny_solgt, bolig_id)
            )

            oppdatert += 1

            if ny_solgt:
                nye_solgte += 1
                log_event("solgt_oppdatert", bolig_id, bolig_url, "")

        except Exception:
            feilet += 1

        if alle:
            progress.progress((i + 1) / len(alle))

    conn.commit()
    return oppdatert, feilet, nye_solgte, len(alle)

def oppdater_skoleavstand(update_all=False):
    if update_all:
        rows_update = c.execute("""
        SELECT id, adresse, postnummer, by
        FROM boliger
        WHERE adresse IS NOT NULL AND adresse != ''
        """).fetchall()
    else:
        rows_update = c.execute("""
        SELECT id, adresse, postnummer, by
        FROM boliger
        WHERE adresse IS NOT NULL AND adresse != ''
        AND (nearest_school_km IS NULL OR nearest_school = '' OR nearest_school IS NULL)
        """).fetchall()

    oppdatert = 0
    feilet = 0

    progress = st.progress(0)

    for i, row in enumerate(rows_update):
        bolig_id, adresse, postnummer, by = row

        try:
            skoledata = finn_naermeste_skole(by, adresse, postnummer)

            c.execute("""
            UPDATE boliger
            SET lat=?, lon=?, nearest_school=?, nearest_school_km=?, nearest_school_min=?
            WHERE id=?
            """, (
                skoledata["lat"],
                skoledata["lon"],
                skoledata["nearest_school"],
                skoledata["nearest_school_km"],
                skoledata["nearest_school_min"],
                bolig_id
            ))

            oppdatert += 1

        except Exception:
            feilet += 1

        if rows_update:
            progress.progress((i + 1) / len(rows_update))

    conn.commit()

    return oppdatert, feilet, len(rows_update)




def vis_analyse(pris, felleskost, leie, strom, kommunale, andre, eieform, rente, ar, ek_prosent, bruk_makslaan, maks_laan):
    netto_for_lan, termin, netto_etter_lan, yield_pct, hopp, kapital = beregn_tall(
        pris, felleskost, leie, strom, kommunale, andre, eieform, rente, ar, ek_prosent, bruk_makslaan, maks_laan
    )

    st.subheader("Analyse")

    a1, a2, a3, a4 = st.columns(4)
    a1.metric("Netto før lån", nok(netto_for_lan))
    a2.metric("Terminbeløp/mnd", nok(termin))
    a3.metric("Netto etter lån", nok(netto_etter_lan))
    a4.metric("Tåler rentehopp", f"{hopp} stk")

    st.metric("Yield", f"{yield_pct:.2f} %")

    st.subheader("Kapitalbehov")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Totalt kapitalbehov", nok(kapital["kapitalbehov"]))
    k2.metric("EK til bolig", nok(kapital["ek_til_bolig"]))
    k3.metric("Dokumentavgift", nok(kapital["dokumentavgift"]))
    k4.metric("Tinglysing", nok(kapital["tinglysing"]))

    if kapital["ekstra_ek"] > 0:
        st.warning(f"Ekstra egenkapital nødvendig pga. maks lånebevis: {nok(kapital['ekstra_ek'])}")

    return kapital, netto_etter_lan, yield_pct, hopp

# ---------------- FINANSIERING ----------------

st.header("Finansiering")

f1, f2, f3, f4, f5 = st.columns(5)

with f1:
    rente = st.number_input("Rente %", value=5.0, step=0.25)

with f2:
    nedbetaling_ar = st.number_input("Nedbetalingstid år", value=30, step=1)

with f3:
    ek_prosent = st.number_input("Egenkapital %", value=15.0, step=1.0)

with f4:
    bruk_makslaan = st.checkbox("Har maks finansieringsbevis")

with f5:
    maks_laan = st.number_input("Maks lån", value=3000000, step=100000)


# ---------------- SIDEBAR ----------------

st.sidebar.header("Slack-varsling")

slack_alerts_on = st.sidebar.checkbox("Aktiver Slack-varsling", value=True)

alert_name = st.sidebar.text_input("Navn på filter", value="Positiv kontantstrøm")
min_yield_alert = st.sidebar.number_input("Varsel: minimum yield %", value=7.0, step=0.5)
min_netto_alert = st.sidebar.number_input("Varsel: minimum netto etter lån", value=0, step=500)
max_pris_alert = st.sidebar.number_input("Varsel: maks pris (0 = ingen maks)", value=0, step=100000)
min_soverom_alert = st.sidebar.number_input("Varsel: minimum soverom", value=0, step=1)
max_skole_km_alert = st.sidebar.number_input("Varsel: maks km til nærmeste skole (0 = ignorer)", value=0.0, step=0.5)
varsle_solgte = st.sidebar.checkbox("Varsle også solgte", value=False)

all_cities_sidebar = [
    row[0] for row in c.execute("""
    SELECT DISTINCT by FROM boliger 
    WHERE by IS NOT NULL AND by != ''
    ORDER BY by
    """).fetchall()
]

valgte_omrader_alert = st.sidebar.multiselect(
    "Varsel: områder",
    all_cities_sidebar,
    default=[],
    placeholder="Alle områder"
)

alert_key = get_alert_key(
    alert_name,
    min_yield_alert,
    min_netto_alert,
    max_pris_alert,
    min_soverom_alert,
    valgte_omrader_alert,
    max_skole_km_alert,
)

alert_settings = {
    "slack_alerts_on": slack_alerts_on,
    "alert_name": alert_name,
    "min_yield_alert": min_yield_alert,
    "min_netto_alert": min_netto_alert,
    "max_pris_alert": max_pris_alert,
    "min_soverom_alert": min_soverom_alert,
    "max_skole_km_alert": max_skole_km_alert,
    "varsle_solgte": varsle_solgte,
    "valgte_omrader_alert": valgte_omrader_alert,
    "alert_key": alert_key,
}

if st.sidebar.button("Test filter på lagrede boliger"):
    rows_alert = c.execute("""
    SELECT id, url, adresse, postnummer, by, pris, felleskost, soverom, leie, strom, kommunale, andre, image_url, eieform, solgt, lat, lon, nearest_school, nearest_school_km, nearest_school_min
    FROM boliger
    ORDER BY id DESC
    """).fetchall()

    sendt = 0
    sjekket = 0

    for row in rows_alert:
        bolig_id, saved_url, adresse, postnummer, by, pris, felleskost, soverom, leie, strom, kommunale, andre, image_url, eieform, solgt, lat, lon, nearest_school, nearest_school_km, nearest_school_min = row

        bolig = {
            "url": saved_url,
            "adresse": adresse,
            "postnummer": postnummer,
            "by": by,
            "pris": pris or 0,
            "felleskost": felleskost or 0,
            "soverom": soverom or 0,
            "leie": leie or 0,
            "strom": strom or 0,
            "kommunale": kommunale or 0,
            "andre": andre or 0,
            "image_url": image_url or "",
            "eieform": eieform or "Ukjent",
            "solgt": solgt or 0,
            "lat": lat,
            "lon": lon,
            "nearest_school": nearest_school,
            "nearest_school_km": nearest_school_km,
            "nearest_school_min": nearest_school_min,
        }

        ok, msg = maybe_send_slack_alert(
            bolig_id,
            bolig,
            alert_settings,
            rente,
            nedbetaling_ar,
            ek_prosent,
            bruk_makslaan,
            maks_laan
        )

        sjekket += 1
        if ok:
            sendt += 1

    st.sidebar.success(f"Sjekket {sjekket} boliger. Sendte {sendt} Slack-varsler.")




# ---------------- APP ----------------

st.title("Boligscanner")
        

with st.expander("Test avstand til skole", expanded=False):
    test_adresse = st.text_input("Testadresse", value="Jon Lilletuns vei 9, 4879 Grimstad")
    test_by = st.selectbox("Velg skole/by", list(SKOLER.keys()))

    if st.button("Test avstand"):
        lat, lon, geo_error = geocode_address(test_adresse)

        if geo_error:
            st.error(f"Geocoding feilet: {geo_error}")
        elif lat and lon:
            beste = None
            for skole in SKOLER[test_by]:
                dist, tid, dist_error = get_distance(lat, lon, skole["lat"], skole["lon"])
                if not dist_error and dist is not None:
                    if beste is None or dist < beste["dist"]:
                        beste = {"navn": skole["navn"], "dist": dist, "tid": tid}

            st.write(f"Fant koordinater: `{lat}, {lon}`")

            if beste:
                st.success(f"Nærmeste skole: {beste['navn']} – {beste['dist']:.2f} km – ca. {beste['tid']:.0f} min med bil")
            else:
                st.error("Fant koordinater, men klarte ikke beregne avstand.")
        else:
            st.error("Fant ikke adressen.")


st.subheader("Automatisk pipeline fra Gmail")

if st.button("Hent nye FINN-varsler fra Gmail"):
    try:
        links = hent_finn_lenker_fra_gmail(max_results=100)

        nye = 0
        allerede = 0
        feilet = 0
        varslet = 0

        for link in links:
            if url_exists(link):
                allerede += 1
                continue

            try:
                data = scrape_finn(link)
                lagret, bolig_id = lagre_bolig(data)

                if lagret:
                    nye += 1

                    row = c.execute("""
                    SELECT lat, lon, nearest_school, nearest_school_km, nearest_school_min
                    FROM boliger WHERE id=?
                    """, (bolig_id,)).fetchone()

                    data["lat"], data["lon"], data["nearest_school"], data["nearest_school_km"], data["nearest_school_min"] = row

                    ok, msg = maybe_send_slack_alert(
                        bolig_id,
                        data,
                        alert_settings,
                        rente,
                        nedbetaling_ar,
                        ek_prosent,
                        bruk_makslaan,
                        maks_laan
                    )

                    if ok:
                        varslet += 1
                        
                else:
                    allerede += 1

            except Exception:
                feilet += 1

        st.success(f"Ferdig: {nye} nye boliger lagret, {allerede} fantes fra før, {feilet} feilet, {varslet} Slack-varsler sendt.")

    except Exception as e:
        st.error(f"Gmail-feil: {e}")


st.subheader("Manuell FINN URL")

url = st.text_input("FINN URL")

if st.button("Hent fra FINN"):
    data = scrape_finn(url)

    st.session_state["ny_url"] = data["url"]
    st.session_state["ny_adresse"] = data["adresse"]
    st.session_state["ny_postnummer"] = data["postnummer"]
    st.session_state["ny_by"] = data["by"]
    st.session_state["ny_pris"] = data["pris"]
    st.session_state["ny_felleskost"] = data["felleskost"]
    st.session_state["ny_soverom"] = data["soverom"]
    st.session_state["ny_leie"] = data["leie"]
    st.session_state["ny_strom"] = data["strom"]
    st.session_state["ny_kommunale"] = data["kommunale"]
    st.session_state["ny_andre"] = data["andre"]
    st.session_state["ny_image_url"] = data["image_url"]
    st.session_state["ny_eieform"] = data["eieform"]
    st.session_state["ny_solgt"] = bool(data["solgt"])
    st.session_state["ny_broker_name"] = data.get("broker_name")
    st.session_state["ny_broker_office"] = data.get("broker_office")
    st.session_state["ny_broker_profile_url"] = data.get("broker_profile_url")
    st.session_state["ny_broker_source_domain"] = data.get("broker_source_domain")

    st.rerun()

st.caption(f"PDF-er lagres i mappen: `{os.path.join('data', 'salgsoppgaver')}/`")

if st.button("Hent salgsoppgave"):
    result, _debug_info = hent_salgsoppgave_med_broker_fallback(
        url,
        adresse=st.session_state.get("ny_adresse", ""),
        postnummer=st.session_state.get("ny_postnummer", ""),
        by=st.session_state.get("ny_by", ""),
    )
    lagre_salgsoppgave_forsok(result)

    if is_downloaded_status(result.status):
        kilde_tekst = "FINN-annonsen" if result.status == "found_from_finn" else "meglerens nettside"
        st.success(f"Salgsoppgave funnet og lastet ned (kilde: {kilde_tekst}).")
        st.write("**Full filsti (Vis filplassering / Åpne mappe):**")
        st.code(os.path.abspath(result.local_pdf_path), language=None)

        try:
            with open(result.local_pdf_path, "rb") as f:
                st.download_button(
                    "Last ned PDF",
                    data=f.read(),
                    file_name=os.path.basename(result.local_pdf_path),
                    mime="application/pdf",
                    key=f"dl_single_{result.finn_ad_id or result.finn_url}",
                )
        except OSError as e:
            st.warning(f"Fant ikke filen på disk for nedlasting: {e}")
    elif result.status in DOCUMENT_URL_ONLY_STATUSES:
        st.info("Salgsoppgave funnet, men må åpnes som digital salgsoppgave.")
        if result.found_document_url:
            st.link_button("Åpne digital salgsoppgave", result.found_document_url)
        if result.salgsoppgave_source_detail:
            st.caption(result.salgsoppgave_source_detail)
    elif result.status == STATUS_LISTING_SOLD_OR_INACTIVE:
        st.warning("Boligen ser ut til å være solgt/ikke lenger aktiv hos megler.")
        if result.salgsoppgave_source_detail:
            st.caption(result.salgsoppgave_source_detail)
    elif result.status == "not_found":
        st.warning("Fant ikke salgsoppgave for denne annonsen.")
        if result.broker_listing_url:
            st.write(f"**Fant meglerannonse (uten nedlastbar PDF):** {result.broker_listing_url}")
        if result.salgsoppgave_source_detail:
            st.caption(result.salgsoppgave_source_detail)
    else:
        st.error(f"Klarte ikke å hente salgsoppgave: {result.error_message}")

    if result.status != "error":
        if result.broker_name or result.broker_source_domain:
            st.write(
                f"**Megler:** {result.broker_name or 'Ukjent navn'}"
                + (f" ({result.broker_source_domain})" if result.broker_source_domain else "")
            )
        if result.broker_listing_url:
            st.write(f"**Meglerannonse:** {result.broker_listing_url}")

        if result.tilstandsrapport_local_path and os.path.exists(result.tilstandsrapport_local_path):
            try:
                with open(result.tilstandsrapport_local_path, "rb") as f:
                    st.download_button(
                        "Last ned tilstandsrapport",
                        data=f.read(),
                        file_name=os.path.basename(result.tilstandsrapport_local_path),
                        mime="application/pdf",
                        key=f"dl_single_tilstandsrapport_{result.finn_ad_id or result.finn_url}",
                    )
            except OSError:
                pass
        elif result.tilstandsrapport_document_url:
            st.link_button("Åpne digital tilstandsrapport", result.tilstandsrapport_document_url)


st.subheader("Batch: salgsoppgaver for nylige boliger")

batch_limit = st.number_input(
    "Antall nylige boliger å sjekke", value=20, min_value=1, step=1, key="batch_salgsoppgave_limit"
)
batch_overskriv = st.checkbox(
    "Prøv på nytt / overskriv", value=False, key="batch_salgsoppgave_overskriv"
)

st.caption(f"PDF-er lagres i mappen: `{os.path.join('data', 'salgsoppgaver')}/`")

if st.button("Hent salgsoppgaver for nylige boliger"):
    boliger_a_sjekke = hent_nylige_boliger_med_url(limit=int(batch_limit))

    resultater = []
    antall_checked = 0
    antall_downloaded = 0
    antall_digital = 0
    antall_not_found = 0
    antall_error = 0

    total = len(boliger_a_sjekke)
    progress = st.progress(0)

    for i, (bolig_id, bolig_url, adresse, postnummer, by) in enumerate(boliger_a_sjekke):
        antall_checked += 1
        adresse_visning = ", ".join(filter(None, [adresse, f"{postnummer} {by}".strip()]))

        try:
            finn_ad_id = extract_finn_ad_id(bolig_url)

            eksisterende = None
            if not batch_overskriv:
                eksisterende = hent_siste_vellykkede_salgsoppgave(finn_ad_id, bolig_url)

            if (
                eksisterende
                and eksisterende["local_pdf_path"]
                and os.path.exists(eksisterende["local_pdf_path"])
            ):
                antall_downloaded += 1
                resultater.append({
                    "adresse": adresse_visning,
                    "finn_url": bolig_url,
                    "finn_ad_id": eksisterende["finn_ad_id"] or finn_ad_id,
                    "broker_name": eksisterende.get("broker_name"),
                    "broker_source_domain": eksisterende.get("broker_source_domain"),
                    "broker_listing_url": eksisterende.get("broker_listing_url"),
                    "salgsoppgave_status": eksisterende["status"],
                    "salgsoppgave_document_url": eksisterende.get("found_document_url"),
                    "salgsoppgave_local_path": eksisterende["local_pdf_path"],
                    "tilstandsrapport_status": eksisterende.get("tilstandsrapport_download_status"),
                    "tilstandsrapport_document_url": eksisterende.get("tilstandsrapport_document_url"),
                    "tilstandsrapport_local_path": eksisterende.get("tilstandsrapport_local_path"),
                    "error_message": "Allerede lastet ned tidligere (hoppet over)",
                })
                continue

            result, _debug_info = hent_salgsoppgave_med_broker_fallback(bolig_url, adresse, postnummer, by)
            lagre_salgsoppgave_forsok(result)

            bucket = status_bucket(result.status)
            if bucket == "downloaded":
                antall_downloaded += 1
            elif bucket == "digital":
                antall_digital += 1
            elif bucket == "not_found":
                antall_not_found += 1
            else:
                antall_error += 1

            resultater.append(resultat_rad_fra_result(adresse_visning, result))

            time.sleep(1.5)

        except Exception as e:
            antall_error += 1
            resultater.append(resultat_rad_uventet_feil(adresse_visning, bolig_url, f"Uventet feil: {e}"))

        progress.progress((i + 1) / total if total else 1.0)

    st.success(
        f"Sjekket {antall_checked} boliger. "
        f"{antall_downloaded} lastet ned, {antall_digital} funnet som digital salgsoppgave, "
        f"{antall_not_found} ikke funnet, {antall_error} feilet."
    )

    if resultater:
        st.dataframe(
            pd.DataFrame(resultater)[RESULTAT_TABELL_KOLONNER],
            use_container_width=True,
            column_config={
                "salgsoppgave_local_path": st.column_config.TextColumn("salgsoppgave_local_path", width="large"),
                "salgsoppgave_document_url": st.column_config.TextColumn("salgsoppgave_document_url", width="large"),
                "tilstandsrapport_local_path": st.column_config.TextColumn("tilstandsrapport_local_path", width="large"),
                "tilstandsrapport_document_url": st.column_config.TextColumn("tilstandsrapport_document_url", width="large"),
                "error_message": st.column_config.TextColumn("error_message", width="large"),
            },
        )

        nedlastede = [
            r for r in resultater
            if is_downloaded_status(r["salgsoppgave_status"])
            and r["salgsoppgave_local_path"] and os.path.exists(r["salgsoppgave_local_path"])
        ]

        if nedlastede:
            st.write("**Last ned enkeltfiler:**")
            for r in nedlastede:
                try:
                    with open(r["salgsoppgave_local_path"], "rb") as f:
                        st.download_button(
                            f"⬇ {r['adresse'] or r['finn_ad_id'] or r['finn_url']}",
                            data=f.read(),
                            file_name=os.path.basename(r["salgsoppgave_local_path"]),
                            mime="application/pdf",
                            key=f"dl_batch_{r['finn_ad_id'] or r['finn_url']}",
                        )
                except OSError:
                    continue


st.subheader("Backfill/test: megler og salgsoppgave for manglende boliger")
st.caption(
    "Finner opptil 20 boliger med FINN-url som mangler megler-info og/eller "
    "salgsoppgave, og prøver å hente dette på nytt. Nyttig for å teste at "
    "megler-gjenkjenning og salgsoppgave-nedlasting fungerer."
)

if st.button("Oppdater 20 manglende boliger"):
    boliger_mangler = hent_boliger_med_manglende_data(limit=20)

    backfill_resultater = []
    backfill_checked = 0
    backfill_broker_found = 0
    backfill_downloaded = 0
    backfill_digital = 0
    backfill_not_found = 0
    backfill_error = 0

    total_mangler = len(boliger_mangler)
    backfill_progress = st.progress(0)
    backfill_status_text = st.empty()

    for i, (bolig_id, bolig_url, adresse, postnummer, by) in enumerate(boliger_mangler):
        backfill_checked += 1
        adresse_visning = ", ".join(filter(None, [adresse, f"{postnummer} {by}".strip()]))
        backfill_status_text.write(f"Sjekker {i + 1}/{total_mangler}: {adresse_visning or bolig_url}")

        try:
            result, _debug_info = hent_salgsoppgave_med_broker_fallback(bolig_url, adresse, postnummer, by)
            lagre_salgsoppgave_forsok(result)

            if result.broker_name or result.broker_source_domain:
                backfill_broker_found += 1

            bucket = status_bucket(result.status)
            if bucket == "downloaded":
                backfill_downloaded += 1
            elif bucket == "digital":
                backfill_digital += 1
            elif bucket == "not_found":
                backfill_not_found += 1
            else:
                backfill_error += 1

            backfill_resultater.append(resultat_rad_fra_result(adresse_visning, result))

        except Exception as e:
            backfill_error += 1
            backfill_resultater.append(resultat_rad_uventet_feil(adresse_visning, bolig_url, f"Uventet feil: {e}"))

        backfill_progress.progress((i + 1) / total_mangler if total_mangler else 1.0)
        time.sleep(1.5)

    backfill_status_text.empty()

    st.success(
        f"Sjekket {backfill_checked} boliger. "
        f"{backfill_broker_found} meglere funnet, {backfill_downloaded} salgsoppgaver lastet ned, "
        f"{backfill_digital} funnet som digital salgsoppgave, "
        f"{backfill_not_found} ikke funnet, {backfill_error} feilet."
    )

    if backfill_resultater:
        st.dataframe(
            pd.DataFrame(backfill_resultater)[RESULTAT_TABELL_KOLONNER],
            use_container_width=True,
            column_config={
                "salgsoppgave_local_path": st.column_config.TextColumn("salgsoppgave_local_path", width="large"),
                "salgsoppgave_document_url": st.column_config.TextColumn("salgsoppgave_document_url", width="large"),
                "tilstandsrapport_local_path": st.column_config.TextColumn("tilstandsrapport_local_path", width="large"),
                "tilstandsrapport_document_url": st.column_config.TextColumn("tilstandsrapport_document_url", width="large"),
                "error_message": st.column_config.TextColumn("error_message", width="large"),
            },
        )
    else:
        st.info("Fant ingen boliger med manglende megler- eller salgsoppgave-data.")


st.subheader("Full backfill: megler og salgsoppgave for HELE databasen")
st.caption(
    "Går gjennom alle boliger med gyldig FINN-url. Boliger som allerede har "
    "både megler-navn og en nedlastet salgsoppgave hoppes automatisk over - "
    "hvis prosessen blir avbrutt (f.eks. lukket fane eller feil), kan du bare "
    "trykke på knappen igjen for å fortsette der den stoppet."
)

full_backfill_delay = st.number_input(
    "Forsinkelse mellom forespørsler (sekunder)",
    value=1.0, min_value=0.2, step=0.5, key="full_backfill_delay"
)
full_backfill_force = st.checkbox(
    "Force re-check (ignorer tidligere resultater og sjekk alle på nytt)",
    value=False, key="full_backfill_force"
)

antall_full_backfill = len(hent_alle_boliger_for_full_backfill(force_recheck=full_backfill_force))
st.caption(f"{antall_full_backfill} boliger vil bli behandlet med gjeldende innstillinger.")

if st.button("Oppdater hele databasen"):
    boliger_full = hent_alle_boliger_for_full_backfill(force_recheck=full_backfill_force)

    full_checked = 0
    full_broker_found = 0
    full_downloaded = 0
    full_digital = 0
    full_not_found = 0
    full_error = 0
    full_resultater = []

    total_full = len(boliger_full)
    full_progress = st.progress(0)
    full_status_text = st.empty()
    start_time = time.time()

    for i, (bolig_id, bolig_url, adresse, postnummer, by) in enumerate(boliger_full):
        full_checked += 1
        adresse_visning = ", ".join(filter(None, [adresse, f"{postnummer} {by}".strip()]))

        elapsed = time.time() - start_time
        if i > 0:
            snitt_per_stk = elapsed / i
            gjenstaende_sek = snitt_per_stk * (total_full - i)
            eta_tekst = f"{int(gjenstaende_sek // 60)} min {int(gjenstaende_sek % 60)} sek"
        else:
            eta_tekst = "beregner..."

        full_status_text.write(
            f"Behandler {i + 1}/{total_full}: {adresse_visning or bolig_url}  \n"
            f"Estimert gjenstående tid: {eta_tekst}"
        )

        try:
            result, _debug_info = hent_salgsoppgave_med_broker_fallback(bolig_url, adresse, postnummer, by)
            lagre_salgsoppgave_forsok(result)

            if result.broker_name or result.broker_source_domain:
                full_broker_found += 1

            bucket = status_bucket(result.status)
            if bucket == "downloaded":
                full_downloaded += 1
            elif bucket == "digital":
                full_digital += 1
            elif bucket == "not_found":
                full_not_found += 1
            else:
                full_error += 1

            full_resultater.append(resultat_rad_fra_result(adresse_visning, result))

        except Exception as e:
            full_error += 1
            full_resultater.append(resultat_rad_uventet_feil(adresse_visning, bolig_url, f"Uventet feil: {e}"))

        full_progress.progress((i + 1) / total_full if total_full else 1.0)
        time.sleep(full_backfill_delay)

    full_status_text.empty()

    total_runtime_sek = time.time() - start_time
    runtime_tekst = f"{int(total_runtime_sek // 60)} min {int(total_runtime_sek % 60)} sek"

    st.success(
        f"Sjekket {full_checked} boliger på {runtime_tekst}. "
        f"{full_broker_found} meglere funnet, {full_downloaded} salgsoppgaver lastet ned, "
        f"{full_digital} funnet som digital salgsoppgave, "
        f"{full_not_found} ikke funnet, {full_error} feilet."
    )

    if full_resultater:
        st.dataframe(
            pd.DataFrame(full_resultater)[RESULTAT_TABELL_KOLONNER],
            use_container_width=True,
            column_config={
                "salgsoppgave_local_path": st.column_config.TextColumn("salgsoppgave_local_path", width="large"),
                "salgsoppgave_document_url": st.column_config.TextColumn("salgsoppgave_document_url", width="large"),
                "tilstandsrapport_local_path": st.column_config.TextColumn("tilstandsrapport_local_path", width="large"),
                "tilstandsrapport_document_url": st.column_config.TextColumn("tilstandsrapport_document_url", width="large"),
                "error_message": st.column_config.TextColumn("error_message", width="large"),
            },
        )
    else:
        st.info("Ingen boliger å behandle (alle er allerede fullført, eller ingen har gyldig FINN-url).")


st.subheader("Test broker fallback")
st.caption(
    "Kjør kun megler-fallback-flyten for én FINN-url og se hvert steg i detalj - "
    "for feilsøking/tuning av søke-mønstre og adressematching. Påvirker ikke de "
    "andre knappene på siden."
)

test_broker_url = st.text_input("FINN URL å teste", key="test_broker_url")

t1, t2 = st.columns(2)
with t1:
    test_broker_debug_mode = st.checkbox("Debug-modus (vis alle detaljer)", value=True, key="test_broker_debug_mode")
with t2:
    test_broker_save = st.checkbox("Save result to bolig database", value=False, key="test_broker_save")

if st.button("Kjør test", key="test_broker_run"):
    try:
        finn_data = scrape_finn(test_broker_url)
    except Exception as e:
        finn_data = None
        st.error(f"Klarte ikke å hente FINN-annonsen: {e}")

    if finn_data:
        result, debug_info = hent_salgsoppgave_med_broker_fallback(
            test_broker_url,
            adresse=finn_data.get("adresse", ""),
            postnummer=finn_data.get("postnummer", ""),
            by=finn_data.get("by", ""),
        )

        if test_broker_save:
            lagre_salgsoppgave_forsok(result)
            st.info("Lagret: bolig-raden er oppdatert og forsøket er logget i salgsoppgave_forsok.")
        else:
            st.info("Kun test - ingenting er lagret i databasen (huk av \"Save result to bolig database\" for å lagre).")

        if is_downloaded_status(result.status):
            st.success(f"Endelig status: {result.status} (kilde: {result.salgsoppgave_source})")
        elif result.status in DOCUMENT_URL_ONLY_STATUSES:
            st.info(f"Endelig status: {result.status} - fant en dokumentlenke, men ikke en direkte nedlastbar PDF.")
        elif result.status == STATUS_LISTING_SOLD_OR_INACTIVE:
            st.warning("Endelig status: listing_sold_or_inactive - boligen ser ut til å være solgt/ikke lenger aktiv.")
        elif result.status == "not_found":
            st.warning("Endelig status: not_found - fant ikke salgsoppgave verken på FINN eller hos megler.")
        else:
            st.error(f"Endelig status: error - {result.error_message}")

        st.write("### Debug-informasjon")
        st.write(f"**finn_url:** {result.finn_url}")
        st.write(f"**finn_ad_id:** {result.finn_ad_id or '-'}")
        st.write(f"**Adresse:** {finn_data.get('adresse') or '-'}")
        st.write(f"**Postnummer:** {finn_data.get('postnummer') or '-'}")
        st.write(f"**By:** {finn_data.get('by') or '-'}")
        st.write(f"**Megler-navn:** {result.broker_name or '-'}")
        st.write(f"**Megler-domene:** {result.broker_source_domain or '-'}")
        st.write(f"**FINN-annonse aktiv:** {result.finn_listing_active}")
        st.write(f"**FINN salgsoppgave-status (før evt. megler-fallback):** {debug_info['finn_status']}")
        st.write(
            f"**\"Se komplett salgsoppgave\"-lenke funnet på FINN:** "
            f"{debug_info['broker_property_link'] or '(ingen - bruker adressesøk-fallback hvis megler er kjent)'}"
        )

        if test_broker_debug_mode:
            st.write(f"**FINN-detalj:** {debug_info['finn_source_detail'] or '-'}")
            st.write(f"**Megler-fallback forsøkt:** {debug_info['broker_attempted']}")
            st.write(
                f"**Strategi brukt:** "
                f"{ {'direct_link': 'Direktelenke fra FINN', 'address_search': 'Adressesøk (fallback)'}.get(debug_info['broker_link_strategy'], '-') }"
            )

            if debug_info["broker_skip_reason"]:
                st.write(f"**Grunn til at megler-fallback ble hoppet over:** {debug_info['broker_skip_reason']}")

            if debug_info["broker_attempted"]:
                if debug_info["broker_link_strategy"] == "address_search":
                    st.write("**Megler-søke-URL-er forsøkt:**")
                    if debug_info["broker_search_urls_attempted"]:
                        st.code("\n".join(debug_info["broker_search_urls_attempted"]), language=None)
                    else:
                        st.write("- (ingen)")

                    st.write("**Kandidat-annonse-URL-er funnet hos megler:**")
                    if debug_info["broker_candidate_urls"]:
                        st.code("\n".join(debug_info["broker_candidate_urls"]), language=None)
                    else:
                        st.write("- (ingen)")

                    st.write("**Vurdering av hver kandidat (godtatt/avvist og hvorfor):**")
                    if debug_info["broker_candidate_evaluations"]:
                        st.dataframe(
                            pd.DataFrame(debug_info["broker_candidate_evaluations"]),
                            use_container_width=True,
                            column_config={
                                "url": st.column_config.TextColumn("url", width="large"),
                                "reason": st.column_config.TextColumn("reason", width="large"),
                            },
                        )
                    else:
                        st.write("- (ingen kandidater ble sjekket)")

                st.write("**Dokumenter funnet på meglersiden (type/url/tekst):**")
                if debug_info["broker_doc_links_found"]:
                    st.dataframe(
                        pd.DataFrame(debug_info["broker_doc_links_found"]),
                        use_container_width=True,
                        column_config={"url": st.column_config.TextColumn("url", width="large")},
                    )
                else:
                    st.write("- (ingen, eller ingen meglerside ble godtatt)")

        st.write(f"**broker_listing_url (broker property URL):** {result.broker_listing_url or '-'}")
        st.write(f"**Endelig salgsoppgave-status:** {result.status}")
        st.write(f"**salgsoppgave_source:** {result.salgsoppgave_source or '-'}")
        st.write(f"**salgsoppgave_source_detail:** {result.salgsoppgave_source_detail or '-'}")
        st.write(f"**salgsoppgave_document_url:** {result.found_document_url or '-'}")
        st.write(f"**local_pdf_path (salgsoppgave):** {result.local_pdf_path or '-'}")
        st.write(f"**tilstandsrapport_status:** {result.tilstandsrapport_status or '-'}")
        st.write(f"**tilstandsrapport_document_url:** {result.tilstandsrapport_document_url or '-'}")
        st.write(f"**tilstandsrapport_local_path:** {result.tilstandsrapport_local_path or '-'}")

        if result.local_pdf_path and os.path.exists(result.local_pdf_path):
            try:
                with open(result.local_pdf_path, "rb") as f:
                    st.download_button(
                        "Last ned salgsoppgave",
                        data=f.read(),
                        file_name=os.path.basename(result.local_pdf_path),
                        mime="application/pdf",
                        key="dl_test_broker_salgsoppgave",
                    )
            except OSError:
                pass
        elif result.found_document_url:
            st.link_button("Åpne digital salgsoppgave", result.found_document_url, key="open_test_broker_salgsoppgave")

        if result.tilstandsrapport_local_path and os.path.exists(result.tilstandsrapport_local_path):
            try:
                with open(result.tilstandsrapport_local_path, "rb") as f:
                    st.download_button(
                        "Last ned tilstandsrapport",
                        data=f.read(),
                        file_name=os.path.basename(result.tilstandsrapport_local_path),
                        mime="application/pdf",
                        key="dl_test_broker_tilstandsrapport",
                    )
            except OSError:
                pass
        elif result.tilstandsrapport_document_url:
            st.link_button("Åpne digital tilstandsrapport", result.tilstandsrapport_document_url, key="open_test_broker_tilstandsrapport")


def bygg_dashboard_synk_poster(rente, nedbetaling_ar, ek_prosent, bruk_makslaan, maks_laan):
    """Bygger listen med bolig-poster (dicts) som skal synkes til Dashboard-
    databasen. yield/netto/kapitalbehov beregnes her med de samme
    finansieringsforutsetningene som er satt i sidebaren akkurat nå, slik at
    Dashboard-prosjektet slipper å implementere egen finansieringslogikk."""
    rows = c.execute("""
    SELECT url, adresse, postnummer, by, pris, felleskost, soverom, leie, strom, kommunale, andre,
           image_url, eieform, solgt,
           broker_name, broker_office, broker_profile_url, broker_source_domain, broker_listing_url,
           salgsoppgave_status, salgsoppgave_local_path, salgsoppgave_document_url,
           tilstandsrapport_download_status, tilstandsrapport_local_path, tilstandsrapport_document_url,
           document_analysis_json
    FROM boliger
    ORDER BY id
    """).fetchall()

    poster = []

    for row in rows:
        (
            url, adresse, postnummer, by, pris, felleskost, soverom, leie, strom, kommunale, andre,
            image_url, eieform, solgt,
            broker_name, broker_office, broker_profile_url, broker_source_domain, broker_listing_url,
            salgsoppgave_status, salgsoppgave_local_path, salgsoppgave_document_url,
            tilstandsrapport_status, tilstandsrapport_local_path, tilstandsrapport_document_url,
            document_analysis_json,
        ) = row

        pris = pris or 0
        felleskost = felleskost or 0
        soverom = soverom or 0
        leie = leie or 0
        strom = strom or 0
        kommunale = kommunale or 0
        andre = andre or 0
        eieform = eieform or "Ukjent"

        _, _, netto_etter_lan, yield_pct, _, kapital = beregn_tall(
            pris, felleskost, leie, strom, kommunale, andre, eieform,
            rente, nedbetaling_ar, ek_prosent, bruk_makslaan, maks_laan,
        )

        poster.append({
            "finn_ad_id": extract_finn_ad_id(url) if url else None,
            "finn_url": url,
            "adresse": adresse,
            "postnummer": postnummer,
            "by": by,
            "eieform": eieform,
            "solgt": solgt or 0,
            "pris": pris,
            "felleskost": felleskost,
            "soverom": soverom,
            "leie": leie,
            "yield_pct": round(yield_pct, 2) if yield_pct is not None else None,
            "netto_etter_lan": round(netto_etter_lan, 2) if netto_etter_lan is not None else None,
            "kapitalbehov": round(kapital["kapitalbehov"], 2) if kapital else None,
            "broker_name": broker_name,
            "broker_office": broker_office,
            "broker_profile_url": broker_profile_url,
            "broker_source_domain": broker_source_domain,
            "broker_listing_url": broker_listing_url,
            "salgsoppgave_status": salgsoppgave_status,
            "salgsoppgave_local_path": salgsoppgave_local_path,
            "salgsoppgave_document_url": salgsoppgave_document_url,
            "tilstandsrapport_status": tilstandsrapport_status,
            "tilstandsrapport_local_path": tilstandsrapport_local_path,
            "tilstandsrapport_document_url": tilstandsrapport_document_url,
            "document_analysis_json": document_analysis_json,
            "image_url": image_url,
            "image_local_path": None,
        })

    return poster


st.divider()
st.subheader("Oppdater dashboard-database")
st.caption(
    "Eksporterer prosessert boligdata fra BoligScanner til en separat "
    "Dashboard-database (et annet, presentasjons-bare prosjekt på disk). "
    "BoligScanner er alltid kilden til sannhet - dette leser kun fra "
    "boliger.db og skriver aldri tilbake hit."
)

_dashboard_config = dashboard_sync.load_config()

dc1, dc2 = st.columns(2)

with dc1:
    dashboard_db_path_input = st.text_input(
        "Sti til dashboard-database (.db-fil)",
        value=_dashboard_config["dashboard_db_path"],
        key="dashboard_db_path_input",
        help="F.eks. C:/Users/.../Dashboard/dashboard.db - opprettes automatisk hvis den ikke finnes.",
    )

with dc2:
    dashboard_data_folder_input = st.text_input(
        "Mappe for kopierte dokumenter (valgfritt)",
        value=_dashboard_config["dashboard_data_folder"],
        key="dashboard_data_folder_input",
        help="La stå tom for å bare lagre de originale filstiene/URL-ene uten å kopiere noe.",
    )

if st.button("Lagre sti-konfigurasjon", key="dashboard_save_config"):
    dashboard_sync.save_config(dashboard_db_path_input, dashboard_data_folder_input)
    st.success("Konfigurasjon lagret.")

dashboard_dry_run = st.checkbox("Test sync uten å skrive", value=True, key="dashboard_dry_run")

dashboard_synk_poster = bygg_dashboard_synk_poster(rente, nedbetaling_ar, ek_prosent, bruk_makslaan, maks_laan)

st.write(f"**{len(dashboard_synk_poster)} boliger klare for synkronisering.**")

if dashboard_synk_poster:
    forhandsvisning_kolonner = [
        "adresse", "by", "pris", "yield_pct", "netto_etter_lan",
        "broker_name", "salgsoppgave_status", "tilstandsrapport_status", "finn_url",
    ]
    st.dataframe(
        pd.DataFrame(dashboard_synk_poster)[forhandsvisning_kolonner],
        use_container_width=True,
        column_config={"finn_url": st.column_config.TextColumn("finn_url", width="large")},
    )

if st.button("Oppdater dashboard-database", key="dashboard_sync_run"):
    dashboard_sync.save_config(dashboard_db_path_input, dashboard_data_folder_input)

    sammendrag = dashboard_sync.sync_boliger(
        dashboard_synk_poster,
        dashboard_db_path_input,
        data_folder=dashboard_data_folder_input or None,
        dry_run=dashboard_dry_run,
    )

    modus_tekst = "Testkjøring (ingenting ble skrevet)" if dashboard_dry_run else "Synkronisering fullført"
    st.success(
        f"{modus_tekst}: {sammendrag['total']} totalt, {sammendrag['inserted']} nye, "
        f"{sammendrag['updated']} oppdatert, {sammendrag['skipped']} hoppet over, "
        f"{sammendrag['errors']} feilet."
    )

    if sammendrag["error_details"]:
        st.error("Noen boliger feilet under synkronisering:")
        st.dataframe(pd.DataFrame(sammendrag["error_details"]), use_container_width=True)


data = {
    "url": st.session_state.get("ny_url", url),
    "adresse": st.session_state.get("ny_adresse", ""),
    "postnummer": st.session_state.get("ny_postnummer", ""),
    "by": st.session_state.get("ny_by", ""),
    "pris": st.session_state.get("ny_pris", 0),
    "felleskost": st.session_state.get("ny_felleskost", 0),
    "soverom": st.session_state.get("ny_soverom", 0),
    "leie": st.session_state.get("ny_leie", 0),
    "strom": st.session_state.get("ny_strom", 1500),
    "kommunale": st.session_state.get("ny_kommunale", 1000),
    "andre": st.session_state.get("ny_andre", 0),
    "image_url": st.session_state.get("ny_image_url", ""),
    "eieform": st.session_state.get("ny_eieform", "Ukjent"),
    "solgt": st.session_state.get("ny_solgt", 0),
    "broker_name": st.session_state.get("ny_broker_name"),
    "broker_office": st.session_state.get("ny_broker_office"),
    "broker_profile_url": st.session_state.get("ny_broker_profile_url"),
    "broker_source_domain": st.session_state.get("ny_broker_source_domain"),
}

if data["image_url"]:
    st.image(data["image_url"])

st.subheader("Ny bolig / hentet bolig")

col1, col2 = st.columns(2)

with col1:
    adresse = st.text_input("Adresse", data["adresse"], key="ny_adresse")
    postnummer = st.text_input("Postnummer", data["postnummer"], key="ny_postnummer")
    by = st.text_input("By", data["by"], key="ny_by")
    pris = st.number_input("Pris", value=data["pris"], step=10000, key="ny_pris")
    felleskost = st.number_input("Felleskost", value=data["felleskost"], step=500, key="ny_felleskost")
    eieform = st.selectbox("Eieform", ["Ukjent", "Selveier", "Andel"], index=["Ukjent", "Selveier", "Andel"].index(data.get("eieform", "Ukjent")), key="ny_eieform")
    solgt = st.checkbox("Solgt", value=bool(data.get("solgt", 0)), key="ny_solgt")

with col2:
    soverom = st.number_input("Soverom", value=data["soverom"], key="ny_soverom")
    leie = st.number_input("Leie", value=data["leie"], step=500, key="ny_leie")
    strom = st.number_input("Strøm", value=data["strom"], step=500, key="ny_strom")
    kommunale = st.number_input("Kommunale", value=data["kommunale"], step=500, key="ny_kommunale")
    andre = st.number_input("Andre", value=data["andre"], step=500, key="ny_andre")

vis_analyse(pris, felleskost, leie, strom, kommunale, andre, eieform, rente, nedbetaling_ar, ek_prosent, bruk_makslaan, maks_laan)

if st.button("Lagre ny bolig"):
    ny_data = {
        "url": data["url"] or url,
        "adresse": adresse,
        "postnummer": postnummer,
        "by": by,
        "pris": pris,
        "felleskost": felleskost,
        "soverom": soverom,
        "leie": leie,
        "strom": strom,
        "kommunale": kommunale,
        "andre": andre,
        "image_url": data["image_url"],
        "eieform": eieform,
        "solgt": 1 if solgt else 0,
        "broker_name": data.get("broker_name"),
        "broker_office": data.get("broker_office"),
        "broker_profile_url": data.get("broker_profile_url"),
        "broker_source_domain": data.get("broker_source_domain"),
    }

    lagret, bolig_id = lagre_bolig(ny_data)

    if lagret:
        row = c.execute("""
        SELECT lat, lon, nearest_school, nearest_school_km, nearest_school_min
        FROM boliger WHERE id=?
        """, (bolig_id,)).fetchone()

        ny_data["lat"], ny_data["lon"], ny_data["nearest_school"], ny_data["nearest_school_km"], ny_data["nearest_school_min"] = row

        st.success("Lagret!")

        ok, msg = maybe_send_slack_alert(
            bolig_id,
            ny_data,
            alert_settings,
            rente,
            nedbetaling_ar,
            ek_prosent,
            bruk_makslaan,
            maks_laan
        )

        if ok:
            st.success("Slack-varsel sendt.")
        else:
            st.info(f"Ingen Slack-varsel: {msg}")
    else:
        st.info("Denne boligen er allerede lagret.")


st.divider()
st.header("Lagrede boliger")
st.subheader("Solgt-status")

if st.button("Oppdater solgt-status på aktive boliger"):
    oppdatert, feilet, nye_solgte, totalt = oppdater_solgt_status()
    st.success(
        f"Ferdig: {oppdatert} sjekket, {nye_solgte} markert som solgt, {feilet} feilet, {totalt} totalt."
    )
    st.rerun()
    
st.subheader("Skoleavstand")

u1, u2 = st.columns(2)

with u1:
    if st.button("Oppdater manglende skoleavstand"):
        oppdatert, feilet, totalt = oppdater_skoleavstand(update_all=False)
        st.success(f"Ferdig: {oppdatert} oppdatert, {feilet} feilet, {totalt} sjekket.")
        st.rerun()

with u2:
    if st.button("Oppdater ALLE skoleavstander"):
        st.warning("Dette kan bruke mange API-kall hvis du har mange boliger.")
        oppdatert, feilet, totalt = oppdater_skoleavstand(update_all=True)
        st.success(f"Ferdig: {oppdatert} oppdatert, {feilet} feilet, {totalt} sjekket.")
        st.rerun()


rows = c.execute("""
SELECT id, url, adresse, postnummer, by, pris, felleskost, soverom, leie, strom, kommunale, andre, image_url, eieform, solgt, lat, lon, nearest_school, nearest_school_km, nearest_school_min,
       broker_name, broker_source_domain, salgsoppgave_status, salgsoppgave_local_path, broker_listing_url, tilstandsrapport_local_path,
       salgsoppgave_document_url, tilstandsrapport_document_url, document_analysis_json
FROM boliger
ORDER BY id DESC
""").fetchall()

omrader = sorted(list(set([row[4] for row in rows if row[4]])))

s1, s2, s3, s4, s5, s6, s7 = st.columns(7)

with s1:
    sortering = st.selectbox("Sorter etter", ["Nyeste først", "Høyest yield", "Høyest netto kontantstrøm", "Lavest kapitalbehov", "Kortest til skole"])

with s2:
    min_yield = st.number_input("Minimum yield %", value=0.0, step=0.5)

with s3:
    min_netto = st.number_input("Minimum netto etter lån", value=-100000, step=500)

with s4:
    max_skole_km = st.number_input("Maks km skole", value=0.0, step=0.5)

with s5:
    valgte_omrader = st.multiselect("Områder", omrader, default=[], placeholder="Alle områder")

with s6:
    vis_solgte = st.checkbox("Vis solgte", value=False)

with s7:
    with st.form("adresse_sok_form"):
        adresse_sok = st.text_input("Søk adresse")
        st.form_submit_button("Søk")


if st.button("Slett alle boliger"):
    c.execute("DELETE FROM boliger")
    conn.commit()
    st.warning("Alle boliger er slettet.")
    st.rerun()


boliger = []

for row in rows:
    bolig_id, saved_url, adresse, postnummer, by, pris, felleskost, soverom, leie, strom, kommunale, andre, image_url, eieform, solgt, lat, lon, nearest_school, nearest_school_km, nearest_school_min, broker_name, broker_source_domain, salgsoppgave_status, salgsoppgave_local_path, broker_listing_url, tilstandsrapport_local_path, salgsoppgave_document_url, tilstandsrapport_document_url, document_analysis_json = row

    pris = pris or 0
    felleskost = felleskost or 0
    soverom = soverom or 0
    leie = leie or 0
    strom = strom or 0
    kommunale = kommunale or 0
    andre = andre or 0
    eieform = eieform or "Ukjent"
    solgt = solgt or 0

    if solgt and not vis_solgte:
        continue

    if valgte_omrader and by not in valgte_omrader:
        continue

    if adresse_sok:
        sok = adresse_sok.lower().strip()
        tekst = f"{adresse} {postnummer} {by}".lower()
        if sok not in tekst:
            continue

    netto_for_lan, termin, netto_etter_lan, yield_pct, hopp, kapital = beregn_tall(
        pris, felleskost, leie, strom, kommunale, andre, eieform, rente, nedbetaling_ar, ek_prosent, bruk_makslaan, maks_laan
    )

    if yield_pct < min_yield:
        continue

    if netto_etter_lan < min_netto:
        continue

    if max_skole_km > 0:
        if nearest_school_km is None:
            continue
        if nearest_school_km > max_skole_km:
            continue

    boliger.append({
        "id": bolig_id,
        "url": saved_url,
        "adresse": adresse,
        "postnummer": postnummer,
        "by": by,
        "pris": pris,
        "felleskost": felleskost,
        "soverom": soverom,
        "leie": leie,
        "strom": strom,
        "kommunale": kommunale,
        "andre": andre,
        "image_url": image_url,
        "eieform": eieform,
        "solgt": solgt,
        "lat": lat,
        "lon": lon,
        "nearest_school": nearest_school,
        "nearest_school_km": nearest_school_km,
        "nearest_school_min": nearest_school_min,
        "broker_name": broker_name,
        "broker_source_domain": broker_source_domain,
        "salgsoppgave_status": salgsoppgave_status,
        "salgsoppgave_local_path": salgsoppgave_local_path,
        "broker_listing_url": broker_listing_url,
        "tilstandsrapport_local_path": tilstandsrapport_local_path,
        "salgsoppgave_document_url": salgsoppgave_document_url,
        "tilstandsrapport_document_url": tilstandsrapport_document_url,
        "document_analysis_json": document_analysis_json,
        "netto_etter_lan": netto_etter_lan,
        "yield_pct": yield_pct,
        "hopp": hopp,
        "kapital": kapital,
    })

if sortering == "Høyest yield":
    boliger.sort(key=lambda x: x["yield_pct"], reverse=True)
elif sortering == "Høyest netto kontantstrøm":
    boliger.sort(key=lambda x: x["netto_etter_lan"], reverse=True)
elif sortering == "Lavest kapitalbehov":
    boliger.sort(key=lambda x: x["kapital"]["kapitalbehov"])
elif sortering == "Kortest til skole":
    boliger.sort(key=lambda x: x["nearest_school_km"] if x["nearest_school_km"] is not None else 9999)

if not boliger:
    st.info("Ingen boliger matcher filteret.")


# ---------------- PAGINATION ----------------

per_page = 20
total_boliger = len(boliger)
max_pages = max(1, (total_boliger + per_page - 1) // per_page)

if "page" not in st.session_state:
    st.session_state.page = 1

st.session_state.page = min(st.session_state.page, max_pages)


def get_visible_pages(current_page, max_pages):
    pages = []

    for i in range(1, max_pages + 1):
        if (
            i == 1
            or i == max_pages
            or current_page - 2 <= i <= current_page + 2
        ):
            pages.append(i)

    result = []
    last_page = 0

    for page_num in pages:
        if last_page and page_num - last_page > 1:
            result.append("...")
        result.append(page_num)
        last_page = page_num

    return result


def pagination_buttons(location):
    visible_pages = get_visible_pages(st.session_state.page, max_pages)

    cols = st.columns(len(visible_pages) + 2)

    with cols[0]:
        if st.button("‹", key=f"{location}_prev", disabled=st.session_state.page <= 1):
            st.session_state.page -= 1
            st.rerun()

    for idx, page_item in enumerate(visible_pages):
        with cols[idx + 1]:
            if page_item == "...":
                st.write("...")
            else:
                if st.button(
                    str(page_item),
                    key=f"{location}_page_{page_item}",
                    type="primary" if st.session_state.page == page_item else "secondary"
                ):
                    st.session_state.page = page_item
                    st.rerun()

    with cols[-1]:
        if st.button("›", key=f"{location}_next", disabled=st.session_state.page >= max_pages):
            st.session_state.page += 1
            st.rerun()



page = st.session_state.page
start_idx = (page - 1) * per_page
end_idx = start_idx + per_page

if total_boliger > 0:
    st.caption(f"Viser bolig {start_idx + 1}–{min(end_idx, total_boliger)} av {total_boliger}")
else:
    st.caption("Ingen boliger å vise")


for b in boliger[start_idx:end_idx]:

    with st.container(border=True):
        img_col, info_col = st.columns([1, 2])

        with img_col:
            if b["image_url"]:
                st.image(b["image_url"], use_container_width=True)
            else:
                st.write("Ingen bilde")

        with info_col:
            if b["solgt"]:
                st.markdown("### 🟡 SOLGT")

            st.subheader(b["adresse"])

            if b["url"]:
                st.link_button("Åpne FINN-annonsen", b["url"])

            st.write(f"{b['postnummer']} {b['by']}")
            st.write(f"**Kjøpesum:** {nok(b['pris'])}")
            st.write(f"**Eieform:** {b['eieform']}")
            st.write(f"**Kapitalbehov:** {nok(b['kapital']['kapitalbehov'])}")
            st.write(f"**Netto etter lån:** {nok(b['netto_etter_lan'])} / mnd")
            st.write(f"**Yield:** {b['yield_pct']:.2f} %")
            st.write(f"**Tåler rentehopp:** {b['hopp']} stk")

            if b.get("nearest_school_km") is not None:
                st.write(
                    f"**Nærmeste skole:** {b['nearest_school']} – "
                    f"{b['nearest_school_km']:.2f} km / ca. {b['nearest_school_min']:.0f} min med bil"
                )
            else:
                st.write("**Nærmeste skole:** Ikke beregnet")

            if b.get("broker_name") or b.get("broker_source_domain"):
                st.write(f"**Megler:** {b.get('broker_name') or 'Ukjent navn'}")
                if b.get("broker_source_domain"):
                    st.write(f"**Meglerhus/domene:** {b['broker_source_domain']}")
                if b.get("broker_listing_url"):
                    st.write(f"**Meglerannonse (broker property URL):** {b['broker_listing_url']}")

            salgsoppgave_status_visning = {
                "found_from_finn": "Funnet (FINN)",
                "found_from_broker_site": "Funnet (meglerside)",
                STATUS_LINK_FOUND_NOT_PDF: "Funnet (digital salgsoppgave)",
                STATUS_INVALID_PDF_RESPONSE: "Funnet (uventet dokumentformat)",
                STATUS_LISTING_SOLD_OR_INACTIVE: "Solgt/ikke lenger aktiv",
                "not_found": "Ikke funnet",
                "error": "Feil ved sjekk",
            }.get(b.get("salgsoppgave_status"), "Ikke sjekket")

            st.write(f"**Salgsoppgave:** {salgsoppgave_status_visning}")

            salgsoppgave_path = b.get("salgsoppgave_local_path")
            salgsoppgave_url = b.get("salgsoppgave_document_url")

            if salgsoppgave_path and os.path.exists(salgsoppgave_path):
                try:
                    with open(salgsoppgave_path, "rb") as f:
                        st.download_button(
                            "Last ned salgsoppgave",
                            data=f.read(),
                            file_name=os.path.basename(salgsoppgave_path),
                            mime="application/pdf",
                            key=f"dl_card_salgsoppgave_{b['id']}",
                        )
                except OSError:
                    pass
            elif salgsoppgave_url:
                st.link_button("Åpne digital salgsoppgave", salgsoppgave_url, key=f"open_card_salgsoppgave_{b['id']}")
            else:
                st.write("Ikke funnet")

            tilstandsrapport_path = b.get("tilstandsrapport_local_path")
            tilstandsrapport_url = b.get("tilstandsrapport_document_url")
            tilstandsrapport_funnet = bool(tilstandsrapport_path and os.path.exists(tilstandsrapport_path))

            st.write(f"**Tilstandsrapport:** {'Funnet' if (tilstandsrapport_funnet or tilstandsrapport_url) else 'Ikke funnet'}")

            if tilstandsrapport_funnet:
                try:
                    with open(tilstandsrapport_path, "rb") as f:
                        st.download_button(
                            "Last ned tilstandsrapport",
                            data=f.read(),
                            file_name=os.path.basename(tilstandsrapport_path),
                            mime="application/pdf",
                            key=f"dl_card_tilstandsrapport_{b['id']}",
                        )
                except OSError:
                    pass
            elif tilstandsrapport_url:
                st.link_button("Åpne digital tilstandsrapport", tilstandsrapport_url, key=f"open_card_tilstandsrapport_{b['id']}")
            else:
                st.write("Ikke funnet")

            with st.expander("Dokumentopplysninger"):
                analyse = {}
                if b.get("document_analysis_json"):
                    try:
                        analyse = json.loads(b["document_analysis_json"])
                    except (TypeError, ValueError):
                        analyse = {}

                if not analyse:
                    st.write("Ingen dokumentanalyse tilgjengelig ennå.")
                else:
                    for komponent in COMPONENT_ORDER:
                        komponent_data = analyse.get(komponent)
                        if not komponent_data:
                            continue

                        st.markdown(f"**{COMPONENT_DISPLAY_NAVN.get(komponent, komponent)}**")

                        if komponent_data.get("tg"):
                            st.write(komponent_data["tg"])
                        if komponent_data.get("remark"):
                            st.write(komponent_data["remark"])

                        detaljer = []
                        if komponent_data.get("year"):
                            detaljer.append(f"Nevnt år: {komponent_data['year']}")
                        if komponent_data.get("cost"):
                            detaljer.append(f"Nevnt kostnad: {komponent_data['cost']}")
                        if detaljer:
                            st.caption(" · ".join(detaljer))

                    nokkelord = analyse.get("keywords")
                    if nokkelord:
                        st.markdown("**Nøkkelord**")
                        for kw in nokkelord:
                            st.write(f"• {kw.capitalize()}")

                if b.get("salgsoppgave_local_path") and os.path.exists(b["salgsoppgave_local_path"]):
                    if st.button("Analyser dokumenter på nytt", key=f"reanalyze_{b['id']}"):
                        analyser_og_lagre_dokumentanalyse(b["id"], b["salgsoppgave_local_path"], force=True)
                        st.rerun()

            with st.expander("Åpne / rediger bolig"):
                st.info("Redigering av boligdata beholdes som før. Avstand beregnes automatisk når ny bolig lagres.")

                e1, e2 = st.columns(2)

                with e1:
                    ny_adresse = st.text_input("Adresse", b["adresse"], key=f"adresse_{b['id']}")
                    ny_postnummer = st.text_input("Postnummer", b["postnummer"], key=f"post_{b['id']}")
                    ny_by = st.text_input("By", b["by"], key=f"by_{b['id']}")
                    ny_pris = st.number_input("Pris", value=b["pris"], step=10000, key=f"pris_{b['id']}")
                    ny_felleskost = st.number_input("Felleskost", value=b["felleskost"], step=500, key=f"felles_{b['id']}")
                    ny_eieform = st.selectbox("Eieform", ["Ukjent", "Selveier", "Andel"], index=["Ukjent", "Selveier", "Andel"].index(b["eieform"]), key=f"eieform_{b['id']}")
                    ny_solgt = st.checkbox("Solgt", value=bool(b["solgt"]), key=f"solgt_{b['id']}")

                with e2:
                    ny_soverom = st.number_input("Soverom", value=b["soverom"], key=f"soverom_{b['id']}")
                    ny_leie = st.number_input("Leie", value=b["leie"], step=500, key=f"leie_{b['id']}")
                    ny_strom = st.number_input("Strøm", value=b["strom"], step=500, key=f"strom_{b['id']}")
                    ny_kommunale = st.number_input("Kommunale", value=b["kommunale"], step=500, key=f"komm_{b['id']}")
                    ny_andre = st.number_input("Andre", value=b["andre"], step=500, key=f"andre_{b['id']}")

                vis_analyse(
                    ny_pris, ny_felleskost, ny_leie, ny_strom, ny_kommunale, ny_andre,
                    ny_eieform, rente, nedbetaling_ar, ek_prosent, bruk_makslaan, maks_laan
                )

                b1, b2 = st.columns(2)

                with b1:
                    if st.button("Lagre endringer", key=f"lagre_{b['id']}"):
                        skoledata = finn_naermeste_skole(ny_by, ny_adresse, ny_postnummer)

                        c.execute("""
                        UPDATE boliger
                        SET adresse=?, postnummer=?, by=?, pris=?, felleskost=?,
                            soverom=?, leie=?, strom=?, kommunale=?, andre=?, eieform=?, solgt=?,
                            lat=?, lon=?, nearest_school=?, nearest_school_km=?, nearest_school_min=?
                        WHERE id=?
                        """, (
                            ny_adresse, ny_postnummer, ny_by, ny_pris, ny_felleskost,
                            ny_soverom, ny_leie, ny_strom, ny_kommunale, ny_andre,
                            ny_eieform, 1 if ny_solgt else 0,
                            skoledata["lat"], skoledata["lon"], skoledata["nearest_school"],
                            skoledata["nearest_school_km"], skoledata["nearest_school_min"],
                            b["id"]
                        ))
                        conn.commit()
                        st.success("Endringer lagret.")
                        st.rerun()

                with b2:
                    if st.button("Slett bolig", key=f"slett_{b['id']}"):
                        c.execute("DELETE FROM boliger WHERE id=?", (b["id"],))
                        conn.commit()
                        st.warning("Bolig slettet.")
                        st.rerun()

if total_boliger > per_page:
    st.divider()
    pagination_buttons("bottom")