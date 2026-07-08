"""Generisk parser for nedlastbare dokumenter på en meglers boligside.

Fungerer uavhengig av om megleren viser dokumentene som knapper, kort,
"accordions" eller en enkel dokumentliste - så lenge dokumentet ligger bak en
vanlig <a href="..."> lenke et sted på siden (uansett hvor dypt i markupen).

Å støtte et nytt meglernettsted er dermed i praksis bare å justere
nøkkelordlistene under (ALLOWED_DOCUMENT_KEYWORDS/EXCLUDED_DOCUMENT_KEYWORDS)
eller legge til domenespesifikke uttrekksregler her, ikke å skrive en helt ny
skraper per megler.
"""

import os
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from salgsoppgave_downloader import (
    DEFAULT_DOWNLOAD_DIR,
    REQUEST_TIMEOUT,
    SalgsoppgaveError,
    get_default_headers,
    looks_like_pdf,
    STATUS_DOWNLOADED_VALID_PDF,
    STATUS_LINK_FOUND_NOT_PDF,
    STATUS_INVALID_PDF_RESPONSE,
)

# Rekkefølgen er prioritet når en lenketekst matcher flere kategorier samtidig.
# Verdien i hvert par er nøkkelord som identifiserer dokumenttypen.
ALLOWED_DOCUMENT_KEYWORDS = [
    ("salgsoppgave", ["komplett salgsoppgave", "salgsoppgave"]),
    ("prospekt", ["boligprospekt", "prospekt"]),
    ("tilstandsrapport", ["tilstandsrapport"]),
    ("takst", ["verditakst", "lånetakst", "boligtakst", "takst"]),
    ("boligsalgsrapport", ["boligsalgsrapport"]),
]

# Disse skal ALDRI lastes ned, selv om de dukker opp i samme dokumentliste.
# Sjekkes før ALLOWED_DOCUMENT_KEYWORDS slik at en lenketekst som nevner både
# en tillatt og en ekskludert kategori trygt blir avvist.
EXCLUDED_DOCUMENT_KEYWORDS = [
    "egenerklæring",
    "energiattest",
    "vedtekter",
    "reguleringskart",
    "nabolagsprofil",
]

# De to dokumentkategoriene som får en dedikert kolonne på bolig-raden i
# app.py. Resten havner kun i downloaded_documents_json.
PRIMARY_DOCUMENT_TYPES = ("salgsoppgave", "prospekt")
CONDITION_REPORT_DOCUMENT_TYPES = ("tilstandsrapport", "takst", "boligsalgsrapport")

DOCUMENT_REQUEST_DELAY = 1.5  # sekunder mellom hver dokumentnedlasting

# URL-mønstre som kjennetegner en "digital salgsoppgave"/fil-proxy-lenke. Disse
# åpner ofte en dokumentviser i nettleseren i stedet for å returnere en ren PDF
# på første forespørsel (Aktiv sine salgsoppgaver er et kjent eksempel).
DIGITAL_DOCUMENT_URL_KEYWORDS = [
    "file-proxy",
    "digital",
    "salgsoppgave",
    "aktiv",
]

# Tekst som indikerer at boligen er solgt/annonsen er avsluttet hos megler.
BROKER_SOLD_KEYWORDS = [
    "solgt",
    "ikke lenger aktiv",
    "annonsen er deaktivert",
]


def is_digital_document_url(url: str) -> bool:
    url_l = (url or "").lower()
    return any(kw in url_l for kw in DIGITAL_DOCUMENT_URL_KEYWORDS)


def er_megler_side_solgt_eller_inaktiv(text: str) -> bool:
    text_l = (text or "").lower()
    return any(kw in text_l for kw in BROKER_SOLD_KEYWORDS)


@dataclass
class BrokerDocument:
    doc_type: str
    url: str
    text: str
    local_path: Optional[str] = None
    # downloaded_valid_pdf | document_link_found_but_not_direct_pdf |
    # invalid_pdf_response | None (dokumentforespørselen feilet teknisk)
    download_status: Optional[str] = None
    error_message: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "type": self.doc_type,
            "url": self.url,
            "text": self.text,
            "local_path": self.local_path,
            "download_status": self.download_status,
        }


def classify_document(text: str, href: str) -> Optional[str]:
    """Returnerer dokumenttype-nøkkelen (f.eks. "tilstandsrapport") hvis lenken
    matcher en tillatt kategori, ellers None. Ekskluderte dokumenttyper
    (egenerklæring, energiattest, o.l.) returnerer alltid None, selv om de
    også inneholder et tillatt nøkkelord."""
    combined = f"{(text or '').lower()} {(href or '').lower()}"

    for excluded in EXCLUDED_DOCUMENT_KEYWORDS:
        if excluded in combined:
            return None

    for doc_type, keywords in ALLOWED_DOCUMENT_KEYWORDS:
        for kw in keywords:
            if kw in combined:
                return doc_type

    return None


def find_documents_on_page(html: str, base_url: str) -> List[BrokerDocument]:
    """Finner alle lenker på siden som matcher en tillatt dokumentkategori.

    Ser kun på <a>-tagger og deres tekst/href, så den er uavhengig av om
    megleren pakker lenkene inn i knapper, kort eller accordion-paneler.
    """
    soup = BeautifulSoup(html, "html.parser")
    documents = []

    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue

        text = a.get_text(" ", strip=True)
        doc_type = classify_document(text, href)

        if not doc_type:
            continue

        documents.append(BrokerDocument(doc_type=doc_type, url=urljoin(base_url, href), text=text))

    return documents


def _unik_filsti(download_dir: str, finn_ad_id: Optional[str], doc_type: str, brukte_navn: set) -> str:
    base_filename = f"{finn_ad_id or 'ukjent'}_{doc_type}"
    filename = f"{base_filename}.pdf"
    teller = 2

    while filename in brukte_navn:
        filename = f"{base_filename}_{teller}.pdf"
        teller += 1

    brukte_navn.add(filename)
    return os.path.join(download_dir, filename)


def _hent_og_klassifiser(url: str) -> Tuple[str, Optional[bytes]]:
    """Henter url og klassifiserer responsen.

    Kaster SalgsoppgaveError KUN ved ekte nettverks-/HTTP-feil. Et innhold som
    ikke er en gyldig PDF er IKKE en feil her - det er bare et annet mulig
    utfall (f.eks. en digital salgsoppgave-visning hos Aktiv), og klassifiseres
    i stedet som STATUS_LINK_FOUND_NOT_PDF eller STATUS_INVALID_PDF_RESPONSE.

    Returnerer (download_status, innhold_eller_None). Innhold er kun satt når
    status er STATUS_DOWNLOADED_VALID_PDF.
    """
    try:
        resp = requests.get(url, headers=get_default_headers(), timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise SalgsoppgaveError(f"Klarte ikke å hente dokumentet: {e}") from e

    if resp.status_code >= 400:
        raise SalgsoppgaveError(f"Dokumentnedlasting feilet med kode {resp.status_code}.")

    content_type = resp.headers.get("Content-Type", "")
    content = resp.content

    if looks_like_pdf(content_type, content):
        return STATUS_DOWNLOADED_VALID_PDF, content

    # Kjent digital salgsoppgave/fil-proxy-mønster, eller en HTML-side i det
    # hele tatt - dette er et FUNNET dokument, bare ikke en direkte PDF.
    if is_digital_document_url(url) or "text/html" in content_type.lower():
        return STATUS_LINK_FOUND_NOT_PDF, None

    return STATUS_INVALID_PDF_RESPONSE, None


def download_broker_documents(
    html: str,
    base_url: str,
    finn_ad_id: Optional[str] = None,
    download_dir: str = DEFAULT_DOWNLOAD_DIR,
) -> List[BrokerDocument]:
    """Finner og laster ned alle tillatte dokumenter på en meglers boligside.

    Returnerer alle klassifiserte dokumenter som ble funnet, med download_status
    satt for hver (se BrokerDocument). local_path er kun satt for dokumenter som
    faktisk validerte som en ekte PDF. Et enkeltdokument som feiler (teknisk feil,
    eller viser seg å være en digital visning i stedet for en PDF) stopper aldri
    behandlingen av de andre dokumentene.
    """
    documents = find_documents_on_page(html, base_url)
    brukte_navn = set()

    for i, doc in enumerate(documents):
        try:
            status, content = _hent_og_klassifiser(doc.url)
            doc.download_status = status

            if status == STATUS_DOWNLOADED_VALID_PDF:
                dest_path = _unik_filsti(download_dir, finn_ad_id, doc.doc_type, brukte_navn)
                os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
                with open(dest_path, "wb") as f:
                    f.write(content)
                doc.local_path = dest_path

        except SalgsoppgaveError as e:
            doc.error_message = str(e)
        finally:
            if i < len(documents) - 1:
                time.sleep(DOCUMENT_REQUEST_DELAY)

    return documents


def velg_primaert_dokument(documents: List[BrokerDocument], types: tuple) -> Optional[BrokerDocument]:
    """Velger beste dokument for en dokumentkategori-gruppe (f.eks. salgsoppgave
    + prospekt, eller tilstandsrapport + takst + boligsalgsrapport).

    Prioriterer en faktisk nedlastet PDF. Hvis ingen slik finnes, faller den
    tilbake til en digital/fil-proxy-lenke (funnet, men ikke direkte
    nedlastbar) - en slik lenke skal fortsatt telle som "funnet". Som siste
    utvei brukes en lenke som ga et uventet (ikke-PDF, ikke-gjenkjent) svar.
    """
    kandidater = [d for d in documents if d.doc_type in types]

    for ønsket_status in (STATUS_DOWNLOADED_VALID_PDF, STATUS_LINK_FOUND_NOT_PDF, STATUS_INVALID_PDF_RESPONSE):
        for d in kandidater:
            if d.download_status == ønsket_status:
                return d

    return None
