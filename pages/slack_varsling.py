import sqlite3
import streamlit as st
import requests

st.set_page_config(page_title="Slack varsling", layout="wide")

conn = sqlite3.connect("boliger.db", check_same_thread=False)
c = conn.cursor()

st.title("🚨 Slack varsling")

# ---------------- HENT WEBHOOK ----------------

def get_slack_webhook():
    try:
        return st.secrets["SLACK_WEBHOOK_URL"]
    except Exception:
        return ""


def send_slack_message(text):
    webhook_url = get_slack_webhook()

    if not webhook_url:
        return False, "Mangler SLACK_WEBHOOK_URL i secrets"

    try:
        res = requests.post(webhook_url, json={"text": text}, timeout=10)
        if res.status_code >= 400:
            return False, f"Slack-feil: {res.status_code} - {res.text}"
        return True, "Varsel sendt"
    except Exception as e:
        return False, str(e)


# ---------------- TEST WEBHOOK ----------------

st.header("🔌 Test Slack-tilkobling")

if st.button("Send testmelding"):
    ok, msg = send_slack_message("✅ Slack er koblet til boligscanner!")
    if ok:
        st.success(msg)
    else:
        st.error(msg)

st.divider()

# ---------------- MANUELL MELDING ----------------

st.header("✍️ Send manuell Slack-melding")

custom_msg = st.text_area(
    "Skriv melding",
    value="🏠 Test fra boligscanner",
    height=150
)

if st.button("Send melding"):
    ok, msg = send_slack_message(custom_msg)
    if ok:
        st.success("Sendt!")
    else:
        st.error(msg)

st.divider()

# ---------------- PREVIEW MAL ----------------

st.header("🧪 Preview Slack-mal")

example = {
    "adresse": "Testveien 12",
    "postnummer": "4879",
    "by": "Grimstad",
    "pris": 2500000,
    "leie": 14000,
    "yield": 8.2,
    "netto": 2500,
    "kapital": 500000,
    "hopp": 6,
    "skole": "UiA Grimstad – 1.2 km – 3 min",
    "url": "https://finn.no/test"
}

preview_text = f"""
🏠 *Ny bolig matcher filter*

📍 *Adresse:* {example["adresse"]}
🌍 *Område:* {example["postnummer"]} {example["by"]}

💰 *Kjøpspris:* {example["pris"]:,} kr
🏦 *Kapitalbehov:* {example["kapital"]:,} kr

📊 *Estimatorer*
• Leie: {example["leie"]:,} kr / mnd
• Yield: {example["yield"]:.2f} %
• Netto: {example["netto"]:,} kr / mnd
• Tåler rentehopp: {example["hopp"]} stk

🎓 *Skole:* {example["skole"]}

🔗 {example["url"]}
""".strip()

st.code(preview_text)

if st.button("Send preview til Slack"):
    ok, msg = send_slack_message(preview_text)
    if ok:
        st.success("Preview sendt!")
    else:
        st.error(msg)

st.divider()

# ---------------- SEND FRA LAGREDE BOLIGER ----------------

st.header("📤 Send Slack-varsler fra lagrede boliger")

rows = c.execute("""
SELECT id, adresse, by, pris, leie
FROM boliger
ORDER BY id DESC
LIMIT 50
""").fetchall()

if not rows:
    st.info("Ingen boliger i databasen.")
else:
    valgt_id = st.selectbox(
        "Velg bolig",
        options=[r[0] for r in rows],
        format_func=lambda x: next(f"{r[1]} ({r[2]})" for r in rows if r[0] == x)
    )

    if st.button("Send valgt bolig til Slack"):
        bolig = next(r for r in rows if r[0] == valgt_id)

        text = f"""
🏠 *Manuell bolig*

📍 {bolig[1]} – {bolig[2]}
💰 Pris: {bolig[3]:,} kr
📊 Leie: {bolig[4]:,} kr
""".strip()

        ok, msg = send_slack_message(text)

        if ok:
            st.success("Sendt!")
        else:
            st.error(msg)