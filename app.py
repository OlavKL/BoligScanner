import re
import base64
import sqlite3
import requests
import streamlit as st
from bs4 import BeautifulSoup

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


st.set_page_config(page_title="Boligscanner", layout="wide")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

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
        alerted_rules, lat, lon, nearest_school, nearest_school_km, nearest_school_min
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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

alert_name = st.sidebar.text_input("Navn på filter", value="Grimstad min 8% yield")
min_yield_alert = st.sidebar.number_input("Varsel: minimum yield %", value=8.0, step=0.5)
min_netto_alert = st.sidebar.number_input("Varsel: minimum netto etter lån", value=-100000, step=500)
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

    st.rerun()


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
SELECT id, url, adresse, postnummer, by, pris, felleskost, soverom, leie, strom, kommunale, andre, image_url, eieform, solgt, lat, lon, nearest_school, nearest_school_km, nearest_school_min
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
    bolig_id, saved_url, adresse, postnummer, by, pris, felleskost, soverom, leie, strom, kommunale, andre, image_url, eieform, solgt, lat, lon, nearest_school, nearest_school_km, nearest_school_min = row

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