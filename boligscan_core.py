"""Kjernelogikk for BoligScanner: databasetilkobling/skjema, FINN-skraping,
Gmail-henting, lagring, geokoding, Slack-varsling og finansregning.

Denne modulen er bevisst holdt fri for Streamlit-UI-kode (ingen widgets,
ingen sidebar, ingen st.button/st.write) - den eneste Streamlit-avhengigheten
er st.secrets (for Slack-webhook og ORS-API-nøkkel), som fungerer helt fint
uten en kjørende Streamlit-server (det er bare fil-lesing av
.streamlit/secrets.toml). Dette gjør modulen trygg å importere og kjøre fra
HVOR SOM HELST:

- app.py importerer alt herfra til den interaktive UI-en/de manuelle knappene.
- worker.py (en egen, alltid-på bakgrunnsprosess, se docker-compose.yml)
  importerer kjor_planlagt_boligscan()/siste_boligscan_dato() herfra for å
  kjøre den daglige scanen helt uavhengig av om noen noensinne åpner appen i
  en nettleser.

Bakgrunn for hvorfor dette måtte flyttes ut av app.py: Streamlit kjører ALDRI
skriptets kode før en nettleser-økt kobler seg til (bekreftet direkte i
praksis - verken en kjørende container, et rått HTTP-kall eller en rå
WebSocket-tilkobling trigget noe scan). Alt som skal skje uten en bruker til
stede kan derfor ikke bo i app.py sin prosess - det må ligge i en modul som
en helt separat, alltid-kjørende prosess (worker.py) kan importere og kjøre
på egen hånd.
"""

import base64
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone

import requests
import streamlit as st
from bs4 import BeautifulSoup

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from salgsoppgave_downloader import extract_broker_info

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

logger = logging.getLogger(__name__)

conn = sqlite3.connect("boliger.db", check_same_thread=False)
c = conn.cursor()


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
CREATE TABLE IF NOT EXISTS scan_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kilde TEXT,
    nye INTEGER,
    allerede INTEGER,
    feilet INTEGER,
    varslet INTEGER,
    error_message TEXT,
    run_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

for col, coltype in [
    ("gmail_alerts", "INTEGER"),
    ("finn_urls", "INTEGER"),
    ("hentet", "INTEGER"),
    ("avvist", "INTEGER"),
    ("slack_agder", "INTEGER"),
    ("slack_default", "INTEGER"),
    ("slack_feil", "INTEGER"),
    ("rader_for", "INTEGER"),
    ("rader_etter", "INTEGER"),
]:
    try:
        c.execute(f"ALTER TABLE scan_log ADD COLUMN {col} {coltype}")
        conn.commit()
    except sqlite3.OperationalError:
        pass

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

# Steder som skal varsles i #agder-boligscanner i stedet for hovedkanalen.
AGDER_BYER = {"Kristiansand", "Grimstad"}
AGDER_MAKS_SKOLE_KM = 10


def get_slack_webhook():
    try:
        return st.secrets["SLACK_WEBHOOK_URL"]
    except Exception:
        return ""


def get_slack_webhook_agder():
    try:
        return st.secrets["SLACK_WEBHOOK_URL_AGDER"]
    except Exception:
        return ""


def velg_slack_webhook(by):
    """Ruter Kristiansand/Grimstad til Agder-kanalen, alle andre steder til
    hovedkanalen. Faller tilbake til hovedkanalen hvis Agder-webhooken ikke
    er satt opp i secrets, slik at varsling ikke stopper opp."""
    if by in AGDER_BYER:
        agder_webhook = get_slack_webhook_agder()
        if agder_webhook:
            return agder_webhook

    return get_slack_webhook()


def send_slack_message(text, webhook_url=None):
    if webhook_url is None:
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

    # Agder-varsler (Kristiansand/Grimstad) skal alltid overholde
    # universitetsavstandskravet, uavhengig av om filteret over er slått på.
    if bolig["by"] in AGDER_BYER:
        km = bolig.get("nearest_school_km")
        if km is None or km > AGDER_MAKS_SKOLE_KM:
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

    ok, msg = send_slack_message(text, velg_slack_webhook(bolig["by"]))

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


def _gmail_internal_date_til_tekst(internal_date_ms):
    """Konverterer Gmail sitt internalDate-felt (ms siden epoch, UTC - satt av
    Gmail selv når eposten ble mottatt, ikke av senderen) til samme
    "YYYY-MM-DD HH:MM:SS"-tekstformat (UTC) som event_log.created_at sin
    CURRENT_TIMESTAMP-standard bruker, slik at de kan sammenlignes/sorteres
    likt. Returnerer None hvis feltet mangler eller ikke kan tolkes."""
    if not internal_date_ms:
        return None
    try:
        dt = datetime.fromtimestamp(int(internal_date_ms) / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return None


def hent_finn_lenker_fra_gmail(max_results=100, return_stats=False):
    """Returnerer en liste med {"url", "gmail_received_at"} - gmail_received_at
    er Gmail sitt eget mottakstidspunkt for eposten lenken ble funnet i (se
    _gmail_internal_date_til_tekst), til bruk for "E-post mottatt"-hendelsen i
    Dashboard-prosjektets aktivitetstidslinje. return_stats=True gir i
    tillegg (links, antall_meldinger) - kun brukt av scan-loggingen i
    kjor_gmail_boligscan, endrer ikke standardoppforselen for eksisterende
    kallere."""
    service = get_gmail_service()

    results = service.users().messages().list(
        userId="me",
        q="label:finn-boligvarsler",
        maxResults=max_results
    ).execute()

    messages = results.get("messages", [])
    links = []
    sette_urler = set()

    for msg in messages:
        message = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="full"
        ).execute()

        mottatt_tidspunkt = _gmail_internal_date_til_tekst(message.get("internalDate"))

        body = get_email_body(message["payload"])
        found = re.findall(r"https://www\.finn\.no/\d+[^\s\"<>]*", body)

        for link in found:
            link = link.replace("&amp;", "&")
            link = link.split("?")[0]

            if link not in sette_urler:
                sette_urler.add(link)
                links.append({"url": link, "gmail_received_at": mottatt_tidspunkt})

    if return_stats:
        return links, len(messages)

    return links


# ---------------- DATABASE FUNKSJONER ----------------

def url_exists(url):
    c.execute("SELECT id FROM boliger WHERE url=?", (url,))
    return c.fetchone() is not None


def log_event(event_type, bolig_id=None, bolig_url="", filter_name="", created_at=None):
    """created_at lar kalleren stemple hendelsen med et tidspunkt i fortiden
    (f.eks. når en Gmail-epost faktisk ble mottatt) i stedet for "na" -
    utelates den brukes tabellens CURRENT_TIMESTAMP-standard som for."""
    if created_at:
        c.execute("""
        INSERT INTO event_log (event_type, bolig_id, bolig_url, filter_name, created_at)
        VALUES (?, ?, ?, ?, ?)
        """, (event_type, bolig_id, bolig_url, filter_name, created_at))
    else:
        c.execute("""
        INSERT INTO event_log (event_type, bolig_id, bolig_url, filter_name)
        VALUES (?, ?, ?, ?)
        """, (event_type, bolig_id, bolig_url, filter_name))
    conn.commit()


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


# ---------------- AUTOMATISK BOLIGSCAN (Gmail -> FINN -> lagring -> Slack) ----------------

def kjor_gmail_boligscan(alert_settings, rente, nedbetaling_ar, ek_prosent, bruk_makslaan, maks_laan, max_results=100):
    """Henter nye FINN-lenker fra Gmail, lagrer nye boliger og sender
    Slack-varsler for de som matcher filteret. Delt av både den manuelle
    knappen i app.py ("Hent nye FINN-varsler fra Gmail") og den automatiske
    daglige jobben i worker.py (kjor_planlagt_boligscan) - slik unngår vi to
    kopier av samme logikk. Kaster aldri unntak videre - enkeltlenker som
    feiler telles bare som "feilet" og resten fortsetter.
    """
    rader_for = c.execute("SELECT COUNT(*) FROM boliger").fetchone()[0]
    logger.info("[SCAN START] boliger.db har %d rad(er) for scan starter.", rader_for)

    links, gmail_alerts_funnet = hent_finn_lenker_fra_gmail(max_results=max_results, return_stats=True)

    nye = 0
    allerede = 0
    feilet = 0
    varslet = 0
    hentet = 0
    avvist = 0
    slack_agder = 0
    slack_default = 0
    slack_feil = 0

    agder_webhook = get_slack_webhook_agder()

    for link_info in links:
        link = link_info["url"]
        gmail_received_at = link_info["gmail_received_at"]

        if url_exists(link):
            allerede += 1
            continue

        try:
            data = scrape_finn(link)
            hentet += 1
            lagret, bolig_id = lagre_bolig(data)

            if lagret:
                nye += 1

                if gmail_received_at:
                    log_event("gmail_mottatt", bolig_id, data["url"], "", created_at=gmail_received_at)

                row = c.execute("""
                SELECT lat, lon, nearest_school, nearest_school_km, nearest_school_min
                FROM boliger WHERE id=?
                """, (bolig_id,)).fetchone()

                data["lat"], data["lon"], data["nearest_school"], data["nearest_school_km"], data["nearest_school_min"] = row

                ok, msg = maybe_send_slack_alert(
                    bolig_id, data, alert_settings, rente, nedbetaling_ar, ek_prosent, bruk_makslaan, maks_laan
                )

                if ok:
                    varslet += 1
                    valgt_webhook = velg_slack_webhook(data["by"])
                    if agder_webhook and valgt_webhook == agder_webhook:
                        slack_agder += 1
                    else:
                        slack_default += 1
                elif msg == "Matcher ikke filter":
                    avvist += 1
                elif msg not in ("Slack-varsling er av", "Allerede varslet for dette filteret"):
                    slack_feil += 1
            else:
                allerede += 1

        except Exception:
            feilet += 1

    rader_etter = c.execute("SELECT COUNT(*) FROM boliger").fetchone()[0]

    logger.info(
        "[SCAN SUMMARY]\n"
        "Database rows before scan: %d\n"
        "Gmail alerts found: %d\n"
        "FINN URLs found: %d\n"
        "Listings fetched: %d\n"
        "New database rows: %d\n"
        "Duplicates skipped: %d\n"
        "Slack AGDER: %d\n"
        "Slack DEFAULT: %d\n"
        "Rejected: %d\n"
        "Errors: %d\n"
        "Database rows after scan: %d",
        rader_for,
        gmail_alerts_funnet,
        len(links),
        hentet,
        nye,
        allerede,
        slack_agder,
        slack_default,
        avvist,
        feilet + slack_feil,
        rader_etter,
    )

    return {
        "nye": nye,
        "allerede": allerede,
        "feilet": feilet,
        "varslet": varslet,
        "gmail_alerts": gmail_alerts_funnet,
        "finn_urls": len(links),
        "hentet": hentet,
        "avvist": avvist,
        "slack_agder": slack_agder,
        "slack_default": slack_default,
        "slack_feil": slack_feil,
        "rader_for": rader_for,
        "rader_etter": rader_etter,
    }


# Standardverdier for den AUTOMATISKE daglige scanen (worker.py). Ingen bruker
# er til stede for å justere sidebar-slidere, så disse speiler nøyaktig
# standardverdiene widgetene i app.py sin UI har (value=...) i dag. Endres
# UI-standardene der, bør disse oppdateres likt.
DAGLIG_SCAN_DEFAULT_RENTE = 5.0
DAGLIG_SCAN_DEFAULT_NEDBETALING_AR = 30
DAGLIG_SCAN_DEFAULT_EK_PROSENT = 15.0
DAGLIG_SCAN_DEFAULT_BRUK_MAKSLAAN = False
DAGLIG_SCAN_DEFAULT_MAKS_LAAN = 3000000

DAGLIG_SCAN_DEFAULT_ALERT_NAME = "Positiv kontantstrøm"
DAGLIG_SCAN_DEFAULT_MIN_YIELD = 7.0
DAGLIG_SCAN_DEFAULT_MIN_NETTO = 0
DAGLIG_SCAN_DEFAULT_MAX_PRIS = 0
DAGLIG_SCAN_DEFAULT_MIN_SOVEROM = 0
DAGLIG_SCAN_DEFAULT_MAX_SKOLE_KM = 0.0
DAGLIG_SCAN_DEFAULT_OMRADER = []
DAGLIG_SCAN_DEFAULT_VARSLE_SOLGTE = False
DAGLIG_SCAN_DEFAULT_SLACK_ALERTS_ON = True


def logg_boligscan(kilde, resultat, error_message=None):
    c.execute("""
    INSERT INTO scan_log (
        kilde, nye, allerede, feilet, varslet, error_message,
        gmail_alerts, finn_urls, hentet, avvist, slack_agder, slack_default, slack_feil,
        rader_for, rader_etter
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        kilde,
        resultat.get("nye") if resultat else None,
        resultat.get("allerede") if resultat else None,
        resultat.get("feilet") if resultat else None,
        resultat.get("varslet") if resultat else None,
        error_message,
        resultat.get("gmail_alerts") if resultat else None,
        resultat.get("finn_urls") if resultat else None,
        resultat.get("hentet") if resultat else None,
        resultat.get("avvist") if resultat else None,
        resultat.get("slack_agder") if resultat else None,
        resultat.get("slack_default") if resultat else None,
        resultat.get("slack_feil") if resultat else None,
        resultat.get("rader_for") if resultat else None,
        resultat.get("rader_etter") if resultat else None,
    ))
    conn.commit()


def siste_scan_sammendrag():
    """Henter siste scan_log-rad (uansett kilde - manuell, planlagt-via-knapp
    eller den automatiske Docker-workeren) som en dict, slik at UI-et kan vise
    siste scan-sammendrag selv etter en sideoppdatering (leses direkte fra
    boliger.db, ikke fra Streamlit sin session_state)."""
    row = c.execute("""
    SELECT kilde, nye, allerede, feilet, varslet, error_message,
           gmail_alerts, finn_urls, hentet, avvist, slack_agder, slack_default, slack_feil,
           rader_for, rader_etter, run_at
    FROM scan_log
    ORDER BY id DESC LIMIT 1
    """).fetchone()

    if not row:
        return None

    kolonner = [
        "kilde", "nye", "allerede", "feilet", "varslet", "error_message",
        "gmail_alerts", "finn_urls", "hentet", "avvist", "slack_agder", "slack_default", "slack_feil",
        "rader_for", "rader_etter", "run_at",
    ]
    return dict(zip(kolonner, row))


def siste_boligscan_dato():
    """Dato (Europe/Oslo-tolket streng - run_at lagres som lokal serverklokke)
    for siste VELLYKKEDE scan_log-oppføring, eller None hvis ingen scan er
    logget ennå. Brukes til å avgjøre om dagens planlagte scan allerede har
    kjørt (se innhentings-/catch-up-logikken i worker.py)."""
    row = c.execute("""
    SELECT run_at FROM scan_log
    WHERE error_message IS NULL
    ORDER BY id DESC LIMIT 1
    """).fetchone()

    if not row or not row[0]:
        return None

    try:
        return datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").date()
    except ValueError:
        return None


def kjor_planlagt_boligscan():
    """Kalles av worker.py sin bakgrunnsplanlegger (daglig kl. 08:00, og som
    innhenting ved oppstart hvis dagens scan ikke har kjørt). Bruker faste
    standardverdier siden ingen bruker er til stede for å justere sidebaren."""
    default_alert_settings = {
        "slack_alerts_on": DAGLIG_SCAN_DEFAULT_SLACK_ALERTS_ON,
        "alert_name": DAGLIG_SCAN_DEFAULT_ALERT_NAME,
        "min_yield_alert": DAGLIG_SCAN_DEFAULT_MIN_YIELD,
        "min_netto_alert": DAGLIG_SCAN_DEFAULT_MIN_NETTO,
        "max_pris_alert": DAGLIG_SCAN_DEFAULT_MAX_PRIS,
        "min_soverom_alert": DAGLIG_SCAN_DEFAULT_MIN_SOVEROM,
        "max_skole_km_alert": DAGLIG_SCAN_DEFAULT_MAX_SKOLE_KM,
        "varsle_solgte": DAGLIG_SCAN_DEFAULT_VARSLE_SOLGTE,
        "valgte_omrader_alert": DAGLIG_SCAN_DEFAULT_OMRADER,
        "alert_key": get_alert_key(
            DAGLIG_SCAN_DEFAULT_ALERT_NAME, DAGLIG_SCAN_DEFAULT_MIN_YIELD, DAGLIG_SCAN_DEFAULT_MIN_NETTO,
            DAGLIG_SCAN_DEFAULT_MAX_PRIS, DAGLIG_SCAN_DEFAULT_MIN_SOVEROM, DAGLIG_SCAN_DEFAULT_OMRADER,
            DAGLIG_SCAN_DEFAULT_MAX_SKOLE_KM,
        ),
    }

    try:
        resultat = kjor_gmail_boligscan(
            default_alert_settings,
            DAGLIG_SCAN_DEFAULT_RENTE,
            DAGLIG_SCAN_DEFAULT_NEDBETALING_AR,
            DAGLIG_SCAN_DEFAULT_EK_PROSENT,
            DAGLIG_SCAN_DEFAULT_BRUK_MAKSLAAN,
            DAGLIG_SCAN_DEFAULT_MAKS_LAAN,
        )
        logg_boligscan("scheduled", resultat)
        return resultat
    except Exception as e:
        logger.error("[SCAN FAILED] Den planlagte scanen feilet for den rakk a lage et sammendrag: %s", e)
        logg_boligscan("scheduled", None, error_message=str(e))
        return None
