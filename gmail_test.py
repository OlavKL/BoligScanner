"""(Re)autentiser Gmail-integrasjonen - MÅ kjøres PÅ VERTEN, IKKE inne i
Docker.

get_gmail_service() i boligscan_core.py (brukt av både boligscanner-UI-et og
boligscanner-worker) gjør bevisst aldri en interaktiv nettleser-innlogging,
fordi begge de containerne kjører headless: ingen nettleser, og den
tilfeldige lokale porten InstalledAppFlow.run_local_server() under lytter på
er ikke videresendt ut av containeren (kun 8501 er det, se
docker-compose.yml). Dette skriptet gjør selve den interaktive flyten - kjør
det direkte på Windows-verten, i en vanlig terminal (ikke via `docker exec`),
der en ekte nettleser finnes:

    python gmail_test.py

Prosjektmappen er volum-montert inn i begge containerne (`.:/app` i
docker-compose.yml), så det nye token.json plukkes opp umiddelbart av dem -
ingen restart nødvendig.

Hvis dette må gjøres ofte (typisk hver 7. dag med feilen "invalid_grant:
Token has been expired or revoked."), skyldes det at Google Cloud-prosjektet
fortsatt står i "Testing"-status i OAuth-samtykkeskjermen, der Google lar
refresh-tokens utløpe etter 7 dager uansett bruk. Publiser samtykkeskjermen
til "In production" i Google Cloud Console for å slippe dette.
"""

import os.path
import base64
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def get_service():
    creds = None

    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)

        with open("token.json", "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_body(payload):
    if "parts" in payload:
        for part in payload["parts"]:
            body = get_body(part)
            if body:
                return body

    data = payload.get("body", {}).get("data")
    if data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

    return ""


service = get_service()

results = service.users().messages().list(
    userId="me",
    q='from:finn.no',
    maxResults=5
).execute()

messages = results.get("messages", [])

print(f"Fant {len(messages)} e-poster")

for msg in messages:
    message = service.users().messages().get(
        userId="me",
        id=msg["id"],
        format="full"
    ).execute()

    body = get_body(message["payload"])

    links = re.findall(r"https://www\.finn\.no/[^\s\"<>]+", body)

    print("----")
    for link in links:
        print(link)