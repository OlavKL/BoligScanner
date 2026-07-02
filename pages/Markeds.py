import sqlite3
import pandas as pd
import streamlit as st

st.set_page_config(page_title="Marked", layout="wide")

conn = sqlite3.connect("boliger.db", check_same_thread=False)
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS leiepriser (
    by TEXT PRIMARY KEY,
    leie_per_rom INTEGER NOT NULL
)
""")
conn.commit()

st.title("Marked")
st.write("Her bestemmer du markedsleie per rom. App.py bruker verdiene som er lagret her.")

rows = c.execute("""
SELECT by, leie_per_rom
FROM leiepriser
ORDER BY by
""").fetchall()

df = pd.DataFrame(rows, columns=["by", "leie_per_rom"])

edited_df = st.data_editor(
    df,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "by": st.column_config.TextColumn("By / område", required=True),
        "leie_per_rom": st.column_config.NumberColumn(
            "Leie per rom",
            min_value=0,
            step=250,
            required=True
        ),
    }
)

if st.button("Lagre leiepriser"):
    c.execute("DELETE FROM leiepriser")

    for _, row in edited_df.iterrows():
        by = str(row["by"]).strip()
        leie = int(row["leie_per_rom"])

        if by:
            c.execute("""
            INSERT INTO leiepriser (by, leie_per_rom)
            VALUES (?, ?)
            """, (by, leie))

    conn.commit()
    st.success("Leieprisene er lagret.")
st.rerun()   