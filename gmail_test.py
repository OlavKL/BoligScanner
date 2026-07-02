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