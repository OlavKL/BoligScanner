import sqlite3
import pandas as pd
import streamlit as st

from gmail_healthcheck import hent_gmail_status

st.set_page_config(page_title="Dashboard", layout="wide")

conn = sqlite3.connect("boliger.db", check_same_thread=False)
c = conn.cursor()

st.title("📊 Boligscanner Dashboard")

# ---------------- GMAIL API-HELSE ----------------

st.header("📧 Gmail API-helse")

gmail_status = hent_gmail_status()

if gmail_status is None:
    st.info("Ingen helsesjekk kjørt enda. Kjøres automatisk hver dag kl. 12:00 (Europe/Oslo).")
else:
    g1, g2, g3 = st.columns(3)
    g1.metric("Status", "OK ✅" if gmail_status["status"] == "ok" else "Feilet ❌")
    g2.metric("Sist sjekket", gmail_status["checked_at"])
    g3.metric("Sist vellykket", gmail_status["last_success_at"] or "aldri")

    if gmail_status["status"] != "ok":
        st.error(f"Feiltype: {gmail_status['error_type']} — {gmail_status['error_message']}")
        if gmail_status["krever_reautentisering"]:
            st.warning(
                "⚠️ Gmail må autentiseres på nytt. Tokenet er ugyldig eller utløpt og kan "
                "ikke fornyes automatisk. Slett token.json og kjør den lokale "
                "innloggingsflyten på nytt for å generere et gyldig token."
            )

gmail_history = c.execute("""
    SELECT checked_at, status, error_type
    FROM gmail_health_log
    ORDER BY id DESC LIMIT 10
""").fetchall()

if gmail_history:
    df_gmail = pd.DataFrame(gmail_history, columns=["Tidspunkt", "Status", "Feiltype"])
    st.dataframe(df_gmail, width="stretch", hide_index=True)

st.divider()

# ---------------- TOTALER ----------------

total_boliger = c.execute("SELECT COUNT(*) FROM boliger").fetchone()[0]

total_analysert = c.execute("""
SELECT COUNT(*) FROM event_log
WHERE event_type = 'bolig_lagret'
""").fetchone()[0]

total_slack = c.execute("""
SELECT COUNT(*) FROM event_log
WHERE event_type = 'slack_varsel'
""").fetchone()[0]

auto_slack = c.execute("""
SELECT COUNT(*) FROM event_log
WHERE event_type = 'slack_varsel'
AND filter_name LIKE '%auto_gmail%'
""").fetchone()[0]

manual_slack = c.execute("""
SELECT COUNT(*) FROM event_log
WHERE event_type = 'slack_varsel'
AND filter_name LIKE '%manuell_test%'
""").fetchone()[0]

c1, c2, c3, c4, c5 = st.columns(5)

c1.metric("🏠 Boliger i databasen", total_boliger)
c2.metric("📈 Boliger analysert", total_analysert)
c3.metric("🚨 Slack totalt", total_slack)
c4.metric("🤖 Auto Gmail", auto_slack)
c5.metric("🧪 Manuell test", manual_slack)

st.divider()

# ---------------- SLACK PER UKE ----------------

st.header("📅 Slack-varsler per uke")

weekly_slack = c.execute("""
SELECT 
    strftime('%Y', created_at) AS aar,
    strftime('%W', created_at) AS uke,
    COUNT(*) AS antall
FROM event_log
WHERE event_type = 'slack_varsel'
GROUP BY aar, uke
ORDER BY aar, uke
""").fetchall()

if weekly_slack:
    df_weekly = pd.DataFrame(weekly_slack, columns=["År", "Uke", "Slack-varsler"])
    df_weekly["Uke"] = df_weekly["Uke"].astype(int)
    df_weekly["År-uke"] = df_weekly["År"] + " - uke " + df_weekly["Uke"].astype(str)

    st.bar_chart(df_weekly.set_index("År-uke")["Slack-varsler"])
else:
    st.info("Ingen Slack-varsler loggført enda.")

st.divider()

# ---------------- SLACK PER KILDE ----------------

st.header("🔀 Slack-varsler etter kilde")

source_rows = c.execute("""
SELECT
    CASE
        WHEN filter_name LIKE '%auto_gmail%' THEN 'Auto Gmail'
        WHEN filter_name LIKE '%manuell_test%' THEN 'Manuell test'
        ELSE 'Ukjent / eldre logging'
    END AS kilde,
    COUNT(*) AS antall
FROM event_log
WHERE event_type = 'slack_varsel'
GROUP BY kilde
ORDER BY antall DESC
""").fetchall()

if source_rows:
    df_source = pd.DataFrame(source_rows, columns=["Kilde", "Antall"])
    st.bar_chart(df_source.set_index("Kilde")["Antall"])
else:
    st.info("Ingen kildedata enda.")

st.divider()

# ---------------- BOLIGER PER BY ----------------

st.header("🏙️ Boliger i databasen per by")

city_rows = c.execute("""
SELECT by, COUNT(*) AS antall
FROM boliger
WHERE by IS NOT NULL AND by != ''
GROUP BY by
ORDER BY antall DESC
""").fetchall()

if city_rows:
    df_city = pd.DataFrame(city_rows, columns=["By", "Antall boliger"])
    st.bar_chart(df_city.set_index("By")["Antall boliger"])
else:
    st.info("Ingen bydata funnet.")

st.divider()

# ---------------- SLACK-VARSLER PER BY ----------------

st.header("🚨 Slack-varsler per by")

slack_city_rows = c.execute("""
SELECT 
    b.by,
    COUNT(*) AS antall
FROM event_log e
JOIN boliger b ON e.bolig_id = b.id
WHERE e.event_type = 'slack_varsel'
AND b.by IS NOT NULL 
AND b.by != ''
GROUP BY b.by
ORDER BY antall DESC
""").fetchall()

if slack_city_rows:
    df_slack_city = pd.DataFrame(slack_city_rows, columns=["By", "Slack-varsler"])
    st.bar_chart(df_slack_city.set_index("By")["Slack-varsler"])
else:
    st.info("Ingen Slack-varsler per by loggført enda.")

st.divider()

# ---------------- TOPP FILTER ----------------

st.header("📌 Topp filtre")

top_filters = c.execute("""
SELECT filter_name, COUNT(*) AS antall
FROM event_log
WHERE event_type = 'slack_varsel'
GROUP BY filter_name
ORDER BY antall DESC
LIMIT 10
""").fetchall()

if top_filters:
    df_filters = pd.DataFrame(top_filters, columns=["Filter", "Antall varsler"])
    st.bar_chart(df_filters.set_index("Filter")["Antall varsler"])
else:
    st.info("Ingen filterdata loggført enda.")