"""Finner og laster ned salgsoppgave/prospekt (PDF) fra en FINN.no boligannonse.

Modulen er bevisst holdt fri for Streamlit/SQLite-avhengigheter slik at den kan
testes og gjenbrukes uavhengig av app.py. Kalleren (app.py) er ansvarlig for å
lagre metadata om forsøket (SalgsoppgaveResult) i egen database.

For å utvide med spesialhåndtering av FINN (f.eks. nytt HTML-oppsett, nye
lenketekster, cookie-vegg e.l.) er det naturlig å:
- legge til flere søkeord i SALGSOPPGAVE_KEYWORDS
- utvide _score_link() med flere heuristikker
- legge til egen håndtering i fetch_finn_page()/download_pdf() ved behov
"""

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

DEFAULT_DOWNLOAD_DIR = os.path.join("data", "salgsoppgaver")
REQUEST_TIMEOUT = 15  # sekunder – vær tålmodig, men ikke heng appen

USER_AGENT = (
    "BoligScannerBot/1.0 (+privat boliganalyse-verktøy; kontakt: olavleek@gmail.com)"
)

# Alle disse tekstene inneholder "salgsoppgave" eller "prospekt" som substring,
# men listes eksplisitt fordi kravspesifikasjonen ber om det og det gjør det
# enkelt å legge til flere varianter senere uten å endre matchelogikken.
SALGSOPPGAVE_KEYWORDS = [
    "komplett salgsoppgave",
    "last ned salgsoppgave",
    "salgsoppgave",
    "prospekt",
]

PDF_MAGIC_BYTES = b"%PDF-"

# Kjente meglerkjeder/-domener som FINN-annonser ofte lenker til. Utvid denne
# listen etter hvert som flere meglerkontor observeres i praksis.
KNOWN_BROKER_DOMAINS = [
    "eiendomsmegler1.no",
    "dnbeiendom.no",
    "privatmegleren.no",
    "krogsveen.no",
    "nordvikbolig.no",
    "aktiv.no",
    "emvest.no",
    "eie.no",
    "sem-johnsen.no",
    "proaktiv.no",
    "lokalmegleren.no",
    "rede-eiendom.no",
    "boaeiendom.no",
]

# Brukes til å finne tekstpartier på siden som omtaler megleren, slik at vi
# kan plukke ut et navn selv når det ikke ligger bak en lenke til et kjent domene.
BROKER_LABEL_KEYWORDS = [
    "ansvarlig megler",
    "kontakt megler",
    "eiendomsmegler",
    "meglerkontor",
    "megler",
]


class SalgsoppgaveError(Exception):
    """Basefeil for alt som kan gå galt under henting av salgsoppgave."""


class InvalidFinnUrlError(SalgsoppgaveError):
    """URL-en er ikke en gyldig FINN.no-boligannonse-URL."""


class PageRequestError(SalgsoppgaveError):
    """Kunne ikke hente selve FINN-annonsesiden (nettverk, timeout, 4xx/5xx)."""


class BlockedError(PageRequestError):
    """FINN svarte med 403/429 - trolig blokkert som bot."""


class DownloadError(SalgsoppgaveError):
    """Kunne ikke laste ned eller verifisere PDF-dokumentet."""


# ---------------- STATUS-VOKABULAR ----------------
# Delt mellom denne modulen, broker_document_parser.py og broker_site_fallback.py
# (som importerer disse herfra for å unngå sirkulære avhengigheter og duplisering).
#
# Brukt som verdi for SalgsoppgaveResult.status / boliger.salgsoppgave_status:
#   found_from_finn                       - PDF lastet ned direkte fra FINN
#   found_from_broker_site                - PDF lastet ned fra meglerens side
#   document_link_found_but_not_direct_pdf - dokument funnet, men kun som digital
#                                            visning/fil-proxy (f.eks. Aktiv)
#   invalid_pdf_response                  - fant en dokumentlenke, men innholdet
#                                            var verken en gyldig PDF eller en
#                                            gjenkjent digital visning
#   listing_sold_or_inactive              - meglersiden viser at boligen er solgt
#   not_found                             - ingen dokumentlenke funnet i det hele tatt
#   error                                 - teknisk feil (nettverk, blokkert, ugyldig url)
#
# BrokerDocument.download_status (broker_document_parser.py) bruker et lite
# undersett av disse (downloaded_valid_pdf/document_link_found_but_not_direct_pdf/
# invalid_pdf_response) for å beskrive utfallet av ett enkelt dokumentforsøk.
STATUS_DOCUMENT_NOT_FOUND = "document_not_found"
STATUS_LINK_FOUND_NOT_PDF = "document_link_found_but_not_direct_pdf"
STATUS_DOWNLOADED_VALID_PDF = "downloaded_valid_pdf"
STATUS_INVALID_PDF_RESPONSE = "invalid_pdf_response"
STATUS_LISTING_SOLD_OR_INACTIVE = "listing_sold_or_inactive"

# De to statusene som betyr at en PDF faktisk ble lastet ned og lagret,
# uansett om den kom fra FINN direkte eller via megler-fallback.
DOWNLOADED_STATUSES = ("found_from_finn", "found_from_broker_site")

# "Ferdigbehandlet" i den forstand at et nytt forsøk neppe vil gi mer
# informasjon - brukes til å avgjøre hva backfill/resume-logikk kan hoppe over.
# invalid_pdf_response holdes bevisst UTENFOR - det kan skyldes en forbigående
# feil og bør forsøkes på nytt.
RESOLVED_STATUSES = DOWNLOADED_STATUSES + (STATUS_LINK_FOUND_NOT_PDF, STATUS_LISTING_SOLD_OR_INACTIVE)


def is_downloaded_status(status: Optional[str]) -> bool:
    return status in DOWNLOADED_STATUSES


def is_resolved_status(status: Optional[str]) -> bool:
    return status in RESOLVED_STATUSES


@dataclass
class SalgsoppgaveResult:
    finn_url: str
    finn_ad_id: Optional[str] = None
    found_document_url: Optional[str] = None
    local_pdf_path: Optional[str] = None
    status: str = "not_found"  # se STATUS-VOKABULAR-kommentaren over
    attempted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    error_message: Optional[str] = None
    broker_name: Optional[str] = None
    broker_office: Optional[str] = None
    broker_profile_url: Optional[str] = None
    broker_source_domain: Optional[str] = None
    broker_listing_url: Optional[str] = None
    salgsoppgave_source: Optional[str] = None  # finn | broker_site | not_found | error
    salgsoppgave_source_detail: Optional[str] = None
    tilstandsrapport_local_path: Optional[str] = None
    tilstandsrapport_document_url: Optional[str] = None
    tilstandsrapport_status: Optional[str] = None  # samme vokabular som status, men kun for tilstandsrapport
    downloaded_documents: List[dict] = field(default_factory=list)
    # Kun til intern bruk i orkestreringen (app.py) for å avgjøre om
    # megler-fallback bør forsøkes, og hvilken strategi som skal brukes.
    # Persisteres ikke direkte i databasen.
    finn_listing_active: Optional[bool] = None
    broker_property_link: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


def get_default_headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.5",
    }


def validate_finn_url(url: str) -> str:
    """Kaster InvalidFinnUrlError hvis url ikke ser ut som en FINN.no-lenke."""
    if not url or not isinstance(url, str) or not url.strip():
        raise InvalidFinnUrlError("Mangler FINN-URL.")

    url = url.strip()
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise InvalidFinnUrlError(f"Ugyldig URL-format: {url}")

    if "finn.no" not in parsed.netloc.lower():
        raise InvalidFinnUrlError(f"URL-en er ikke en FINN.no-adresse: {url}")

    return url


def extract_finn_ad_id(url: str) -> Optional[str]:
    """Plukker ut FINN-annonse-ID-en, f.eks. finnkode=123456789 eller /123456789."""
    match = re.search(r"finnkode=(\d+)", url)
    if match:
        return match.group(1)

    match = re.search(r"/(\d{6,})(?:[/?#]|$)", url)
    if match:
        return match.group(1)

    return None


def fetch_finn_page(url: str) -> requests.Response:
    try:
        resp = requests.get(url, headers=get_default_headers(), timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout as e:
        raise PageRequestError(f"Tidsavbrudd ved henting av FINN-siden: {e}") from e
    except requests.exceptions.RequestException as e:
        raise PageRequestError(f"Klarte ikke å hente FINN-siden: {e}") from e

    if resp.status_code in (403, 429):
        raise BlockedError(
            f"FINN blokkerte forespørselen (HTTP {resp.status_code})."
        )

    if resp.status_code >= 400:
        raise PageRequestError(f"FINN-siden svarte med feilkode {resp.status_code}.")

    return resp


# Samme "Solgt"-mønster som brukes i app.py sin scrape_finn(), duplisert med
# vilje her siden denne modulen ikke skal avhenge av/endre den eksisterende
# scrape-flyten, men fortsatt trenger å vite om annonsen er aktiv for å
# avgjøre om megler-fallback er verdt å forsøke.
_SOLGT_PATTERN = re.compile(
    r"\bSolgt\b\s+.{0,250}?,\s*\d{4}\s+[A-ZÆØÅa-zæøå\s\-]+"
)


def er_finn_annonse_solgt(text: str) -> bool:
    return bool(_SOLGT_PATTERN.search(text or ""))


def _score_link(text: str, href: str) -> int:
    text_l = (text or "").lower()
    href_l = (href or "").lower()

    score = 0

    if "salgsoppgave" in text_l or "salgsoppgave" in href_l:
        score += 10
    if "prospekt" in text_l or "prospekt" in href_l:
        score += 10

    if score == 0:
        return 0

    if "komplett" in text_l:
        score += 3
    if "last ned" in text_l:
        score += 3
    if href_l.endswith(".pdf") or ".pdf" in href_l:
        score += 5

    return score


def find_salgsoppgave_links(html: str, base_url: str) -> List[Tuple[int, str, str]]:
    """Returnerer (score, absolutt_url, lenketekst) sortert høyest score først."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue

        text = a.get_text(" ", strip=True)
        score = _score_link(text, href)

        if score <= 0:
            continue

        absolute_url = urljoin(base_url, href)
        candidates.append((score, absolute_url, text))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates


# For aktive FINN-annonser med megler er det som regel en knapp/lenke med
# nettopp denne teksten som peker direkte til boligen på meglerens egen side.
# Dette er en langt mer pålitelig kilde enn å gjette søke-URL-er hos megleren
# (broker_site_fallback sin adressesøk-strategi), siden den garantert treffer
# riktig eiendom.
BROKER_PROPERTY_LINK_PHRASES = [
    "se komplett salgsoppgave",
    "komplett salgsoppgave",
]


def find_broker_property_link(html: str, base_url: str) -> Optional[str]:
    """Finner en direkte lenke fra FINN-annonsen til boligens side hos megleren
    (typisk knappen "Se komplett salgsoppgave"). Returnerer None hvis lenken
    peker til en PDF (fanges allerede opp av find_salgsoppgave_links) eller til
    finn.no selv (interne anker-lenker), siden det da ikke er en meglerside.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")

        for a in soup.find_all("a"):
            href = a.get("href")
            if not href:
                continue

            text = a.get_text(" ", strip=True).lower()
            if not any(phrase in text for phrase in BROKER_PROPERTY_LINK_PHRASES):
                continue

            absolute_url = urljoin(base_url, href)

            if absolute_url.lower().split("?")[0].endswith(".pdf"):
                continue

            if "finn.no" in urlparse(absolute_url).netloc.lower():
                continue

            return absolute_url

    except Exception:
        # Deteksjon er en bonus - skal aldri velte hoved-flyten.
        pass

    return None


# ---------------- MEGLER/BROKER-DETEKSJON ----------------

def _domain_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


def _match_known_broker_domain(domain: str) -> Optional[str]:
    for known in KNOWN_BROKER_DOMAINS:
        if domain == known or domain.endswith("." + known):
            return known
    return None


def extract_broker_info(html: str, base_url: str) -> dict:
    """Beste-innsats-forsøk på å finne megler/meglerkontor på en FINN-annonseside.

    Returnerer alltid en dict med nøklene broker_name, broker_office,
    broker_profile_url og broker_source_domain (None der ingenting ble funnet).
    Kaster aldri unntak - kalles best-effort og skal ikke stoppe resten av flyten.
    """
    info = {
        "broker_name": None,
        "broker_office": None,
        "broker_profile_url": None,
        "broker_source_domain": None,
    }

    try:
        soup = BeautifulSoup(html, "html.parser")

        # 1) Mest pålitelige signal: en lenke til et kjent meglerdomene.
        for a in soup.find_all("a"):
            href = a.get("href")
            if not href:
                continue

            absolute_url = urljoin(base_url, href)
            domain = _domain_from_url(absolute_url)
            known_domain = _match_known_broker_domain(domain)

            if known_domain:
                info["broker_source_domain"] = known_domain
                info["broker_profile_url"] = absolute_url
                text = a.get_text(" ", strip=True)
                if text:
                    info["broker_name"] = text
                break

        # 2) Se etter tekst i nærheten av kjente megler-nøkkelord for å finne
        #    navn/kontor selv når det ikke ligger bak en lenke til et kjent domene.
        for keyword in BROKER_LABEL_KEYWORDS:
            element = soup.find(string=re.compile(re.escape(keyword), re.IGNORECASE))
            if not element:
                continue

            container = element.parent
            if container is None:
                continue

            context_text = container.get_text(" ", strip=True)
            match = re.search(
                rf"{re.escape(keyword)}\s*[:\-]?\s*([A-ZÆØÅ][\wÆØÅæøå.\-' ]{{2,60}})",
                context_text,
                re.IGNORECASE,
            )

            if match and not info["broker_name"]:
                candidate = match.group(1).strip().rstrip(".,")
                if candidate.lower() != keyword.lower():
                    info["broker_name"] = candidate

            if not info["broker_office"]:
                sibling_link = container.find("a")
                if sibling_link and sibling_link.get_text(strip=True):
                    info["broker_office"] = sibling_link.get_text(" ", strip=True)
                    if not info["broker_profile_url"]:
                        sibling_href = sibling_link.get("href")
                        if sibling_href:
                            info["broker_profile_url"] = urljoin(base_url, sibling_href)

            if info["broker_name"]:
                break

    except Exception:
        # Megler-deteksjon er en bonus - skal aldri velte hoved-flyten.
        pass

    return info


def looks_like_pdf(content_type: str, content: bytes) -> bool:
    if content_type and "pdf" in content_type.lower():
        return True
    if content[:5] == PDF_MAGIC_BYTES:
        return True
    return False


def download_pdf(document_url: str, dest_path: str) -> str:
    """Laster ned document_url til dest_path. Kaster DownloadError/BlockedError."""
    try:
        resp = requests.get(document_url, headers=get_default_headers(), timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout as e:
        raise DownloadError(f"Tidsavbrudd ved nedlasting av dokument: {e}") from e
    except requests.exceptions.RequestException as e:
        raise DownloadError(f"Klarte ikke å laste ned dokument: {e}") from e

    if resp.status_code in (403, 429):
        raise BlockedError(
            f"Blokkert (HTTP {resp.status_code}) ved nedlasting av dokument."
        )

    if resp.status_code >= 400:
        raise DownloadError(f"Dokumentnedlasting feilet med kode {resp.status_code}.")

    content = resp.content
    content_type = resp.headers.get("Content-Type", "")

    if not looks_like_pdf(content_type, content):
        raise DownloadError(
            "Den nedlastede filen ser ikke ut til å være en gyldig PDF."
        )

    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)

    with open(dest_path, "wb") as f:
        f.write(content)

    return dest_path


def build_dest_path(download_dir: str, finn_ad_id: Optional[str]) -> str:
    filename = f"{finn_ad_id or 'ukjent'}_salgsoppgave.pdf"
    return os.path.join(download_dir, filename)


def hent_salgsoppgave(
    finn_url: str, download_dir: str = DEFAULT_DOWNLOAD_DIR
) -> SalgsoppgaveResult:
    """Hovedinngang: prøver å finne og laste ned salgsoppgave/prospekt direkte fra
    en FINN-annonse (ingen megler-fallback her - det håndteres av
    app.hent_salgsoppgave_med_broker_fallback()).

    Returnerer alltid en SalgsoppgaveResult - kaster aldri unntak videre til kalleren.
    """
    result = SalgsoppgaveResult(finn_url=finn_url)

    try:
        validated_url = validate_finn_url(finn_url)
        result.finn_ad_id = extract_finn_ad_id(validated_url)

        page = fetch_finn_page(validated_url)
        result.finn_listing_active = not er_finn_annonse_solgt(page.text)

        broker_info = extract_broker_info(page.text, validated_url)
        result.broker_name = broker_info["broker_name"]
        result.broker_office = broker_info["broker_office"]
        result.broker_profile_url = broker_info["broker_profile_url"]
        result.broker_source_domain = broker_info["broker_source_domain"]
        result.broker_property_link = find_broker_property_link(page.text, validated_url)

        candidates = find_salgsoppgave_links(page.text, validated_url)

        if not candidates:
            result.status = "not_found"
            result.salgsoppgave_source = "not_found"
            result.salgsoppgave_source_detail = "Ingen salgsoppgave-/prospekt-lenke funnet på FINN-annonsen."
            return result

        last_error = None

        for _score, doc_url, _text in candidates:
            dest_path = build_dest_path(download_dir, result.finn_ad_id)
            try:
                local_path = download_pdf(doc_url, dest_path)
            except SalgsoppgaveError as e:
                last_error = e
                continue

            result.found_document_url = doc_url
            result.local_pdf_path = local_path
            result.status = "found_from_finn"
            result.salgsoppgave_source = "finn"
            result.salgsoppgave_source_detail = "Lastet ned direkte fra FINN-annonsen."
            return result

        # Fant lenker som så ut som salgsoppgave, men ingen kunne lastes ned som PDF.
        result.status = "not_found"
        result.salgsoppgave_source = "not_found"
        if last_error:
            result.error_message = str(last_error)
            result.salgsoppgave_source_detail = f"Fant lenke(r) på FINN, men nedlasting feilet: {last_error}"
        else:
            result.salgsoppgave_source_detail = "Fant lenke(r) på FINN, men ingen kunne lastes ned som gyldig PDF."
        return result

    except SalgsoppgaveError as e:
        result.status = "error"
        result.error_message = str(e)
        result.salgsoppgave_source = "error"
        result.salgsoppgave_source_detail = str(e)
        return result
    except Exception as e:  # uventede feil skal aldri kræsje appen
        result.status = "error"
        result.error_message = f"Uventet feil: {e}"
        result.salgsoppgave_source = "error"
        result.salgsoppgave_source_detail = f"Uventet feil: {e}"
        return result


# Alias som matcher navnet brukt av batch-nedlastingsflyten i app.py.
download_salgsoppgave = hent_salgsoppgave
