"""Frittstående bakgrunnsprosess: kjører den daglige boligscanen og
Gmail-helsesjekken helt uavhengig av Streamlit-UI-en.

Dette er selve fiksen for at BoligScanner skal kjøre autonomt så snart
Docker-containeren starter, uten at noen trenger å åpne appen i en
nettleser først. Bekreftet direkte i praksis: Streamlit kjører ALDRI
skriptets kode før en nettleser-økt kobler seg til - verken en kjørende
container, et rått HTTP-kall eller en rå WebSocket-tilkobling trigget noe
scan i den gamle løsningen. Alt som skal skje uten en bruker til stede kan
derfor ikke bo i app.py sin prosess.

Denne filen er et helt separat, alltid-kjørende Python-program - startet som
sin egen tjeneste i docker-compose.yml (samme image som boligscanner-appen,
bare med en annen kommando), som importerer den delte kjernelogikken fra
boligscan_core.py og gmail_healthcheck.py: nøyaktig de samme funksjonene
Streamlit-knappene bruker, bare uten en nettleser til stede.

Kjøres med: python worker.py
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from gmail_healthcheck import kjor_gmail_helsesjekk
from boligscan_core import kjor_planlagt_boligscan, siste_boligscan_dato

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [worker] %(message)s",
)
logger = logging.getLogger("boligscan_worker")

OSLO = ZoneInfo("Europe/Oslo")


def main():
    scheduler = BlockingScheduler(timezone=OSLO)

    scheduler.add_job(
        kjor_gmail_helsesjekk,
        trigger="cron",
        hour=12,
        minute=0,
        id="gmail_daglig_helsesjekk",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    scheduler.add_job(
        kjor_planlagt_boligscan,
        trigger="cron",
        hour=8,
        minute=0,
        id="daglig_boligscan",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    logger.info(
        "Bakgrunnsjobber planlagt: Gmail-helsesjekk kl. 12:00, boligscan kl. 08:00 (Europe/Oslo)."
    )

    # Innhenting: hvis dagens planlagte scan ikke har kjørt ennå - f.eks. fordi
    # PC-en/Docker ikke var i gang på det tidspunktet - kjør den én gang med
    # det samme i stedet for å vente til i morgen kl. 08:00.
    today = datetime.now(OSLO).date()
    siste = siste_boligscan_dato()

    if siste != today:
        logger.info("Ingen vellykket scan registrert i dag (siste: %s) - kjører innhenting nå.", siste)
        try:
            kjor_planlagt_boligscan()
            logger.info("Innhentingsscan fullført.")
        except Exception:
            logger.exception("Innhentingsscan feilet.")
    else:
        logger.info("Dagens scan er allerede kjørt (%s) - venter til neste planlagte tidspunkt.", siste)

    logger.info("Starter planlegger (blokkerer denne prosessen for alltid).")
    scheduler.start()


if __name__ == "__main__":
    main()
