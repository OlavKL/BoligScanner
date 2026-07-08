"""Fallback: finn samme boligannonse på meglerens egen nettside ved å søke på
adresse, og last ned tillatte dokumenter derfra.

NB: Dette er det sekundære fallback-sporet. Det primære sporet er langt mer
pålitelig og går via FINN sin "Se komplett salgsoppgave"-knapp, som peker
direkte til riktig eiendom hos megler (se
salgsoppgave_downloader.find_broker_property_link() og
app.hent_salgsoppgave_med_broker_fallback()). Denne modulen brukes kun når
FINN-annonsen ikke har noen slik direktelenke.

Bevisst holdt enkel og generisk siden vi ikke kjenner den eksakte
side-strukturen til hvert meglerkontor. Strategien er:

1. Prøv noen vanlige gjetninger på søke-URL-er hos meglerdomenet
   (GENERIC_SEARCH_URL_TEMPLATES), med adressen som søketekst.
2. Se også på forsiden til meglerdomenet etter interne lenker som
   inneholder gatenavnet (dekker "nyeste boliger"-lister o.l.).
3. For hver kandidatside: krev at gatenavn (+ helst husnummer) finnes i
   sidens tekst, og at enten postnummer eller by også stemmer, før den
   godtas som en bekreftet match ("konservativ matching").
4. Når en match er bekreftet lagres broker_listing_url med en gang - selv
   om det ikke finnes noen nedlastbare dokumenter der.
5. Selve dokumentsøket/-nedlastingen på den godtatte siden gjøres av den
   delte, gjenbrukbare parseren i broker_document_parser.py.

For å utvide med spesialhåndtering av et bestemt meglerdomene (f.eks. en
kjent søke-endepunkt-URL eller et annet HTML-oppsett), legg til en egen
gren i _søk_kandidat_sider() eller en oppføring i BROKER_SEARCH_OVERRIDES
uten å måtte endre resten av modulen.
"""

import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urljoin, quote_plus

import requests
from bs4 import BeautifulSoup

from salgsoppgave_downloader import USER_AGENT, REQUEST_TIMEOUT, DEFAULT_DOWNLOAD_DIR
from broker_document_parser import download_broker_documents, BrokerDocument, er_megler_side_solgt_eller_inaktiv

# Meglerdomener denne fallback-strategien er prioritert testet mot først.
# Den generiske søkestrategien fungerer i prinsippet for et hvilket som
# helst domene som blir gitt inn, så listen er informativ/dokumenterende
# heller enn en hard begrensning.
PRIORITERTE_MEGLERDOMENER = [
    "eiendomsmegler1.no",
    "dnbeiendom.no",
    "privatmegleren.no",
    "krogsveen.no",
    "aktiv.no",
    "eie.no",
    "nordvikbolig.no",
    "sem-johnsen.no",
    "proaktiv.no",
]

# Gjetning på vanlige søke-URL-mønstre. {domain} og {query} fylles inn.
# Utvid denne listen etter hvert som faktiske mønstre observeres i praksis.
GENERIC_SEARCH_URL_TEMPLATES = [
    "https://www.{domain}/sok?q={query}",
    "https://www.{domain}/sok?query={query}",
    "https://www.{domain}/soek?q={query}",
    "https://www.{domain}/eiendommer?sok={query}",
    "https://www.{domain}/search?query={query}",
]

# Mulighet for å overstyre søke-URL-mal per domene når faktisk oppsett er
# kjent/testet. Tom som standard - fylles inn etter hvert som det verifiseres.
BROKER_SEARCH_OVERRIDES = {}

BROKER_REQUEST_DELAY = 1.5  # sekunder mellom hver forespørsel mot meglersiden
MAKS_KANDIDATER_Å_SJEKKE = 5


@dataclass
class BrokerSiteResult:
    listing_url: Optional[str] = None
    documents: List[BrokerDocument] = field(default_factory=list)
    listing_sold_or_inactive: bool = False
    match_detail: str = ""
    # Debug-spor - fylt ut underveis slik at kalleren (f.eks. "Test broker
    # fallback"-seksjonen i app.py) kan vise nøyaktig hva som ble forsøkt,
    # uten å måtte instrumentere denne modulen på nytt.
    search_urls_attempted: List[str] = field(default_factory=list)
    candidate_urls: List[str] = field(default_factory=list)
    candidate_evaluations: List[dict] = field(default_factory=list)  # [{"url", "accepted", "reason"}]
    doc_links_found: List[dict] = field(default_factory=list)  # [{"type", "url", "text", "download_status"}] - alle klassifiserte, ikke bare nedlastede

    def as_dict(self) -> dict:
        return {
            "listing_url": self.listing_url,
            "documents": [doc.as_dict() for doc in self.documents],
            "listing_sold_or_inactive": self.listing_sold_or_inactive,
            "match_detail": self.match_detail,
            "search_urls_attempted": self.search_urls_attempted,
            "candidate_urls": self.candidate_urls,
            "candidate_evaluations": self.candidate_evaluations,
            "doc_links_found": self.doc_links_found,
        }


def _headers() -> dict:
    return {
        "User-Agent": USER_AGENT,
        "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.5",
    }


def _safe_get(url: str) -> Optional[requests.Response]:
    try:
        resp = requests.get(url, headers=_headers(), timeout=REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        return None

    if resp.status_code >= 400:
        return None

    return resp


def _parse_gatenavn_husnummer(adresse: str) -> Tuple[str, str]:
    adresse = (adresse or "").strip()
    match = re.match(r"^(.*?)\s+(\d+[A-Za-z]?)\b", adresse)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return adresse, ""


def _match_confidence(page_text: str, adresse: str, postnummer: str, by: str) -> Tuple[bool, str]:
    """Konservativ matching: krev gatenavn (+ helst husnummer), og enten
    postnummer eller by, før en kandidatside godtas som samme bolig."""
    page_text_l = (page_text or "").lower()
    gatenavn, husnummer = _parse_gatenavn_husnummer(adresse)

    if not gatenavn:
        return False, "Mangler gatenavn å matche mot."

    gatenavn_l = gatenavn.lower()
    if gatenavn_l not in page_text_l:
        return False, f"Fant ikke gatenavnet '{gatenavn}' på siden."

    if husnummer and husnummer.lower() not in page_text_l:
        return False, f"Fant gatenavn '{gatenavn}', men ikke husnummer '{husnummer}' på siden."

    postnummer_treff = bool(postnummer) and postnummer.strip() in page_text_l
    by_treff = bool(by) and by.strip().lower() in page_text_l

    if not postnummer_treff and not by_treff:
        return False, "Fant adresse, men verken postnummer eller by stemte - for usikkert til å godta."

    detaljer = [f"adresse '{adresse}' bekreftet"]
    if postnummer_treff:
        detaljer.append(f"postnummer {postnummer} stemte")
    if by_treff:
        detaljer.append(f"by '{by}' stemte")

    return True, "Match: " + ", ".join(detaljer) + "."


def _score_gatenavn_lenke(text: str, href: str, gatenavn: str) -> int:
    text_l = (text or "").lower()
    href_l = (href or "").lower()
    gatenavn_l = gatenavn.lower()

    if not gatenavn_l:
        return 0

    if gatenavn_l in text_l or gatenavn_l in href_l:
        return 10

    return 0


def _finn_kandidat_lenker(html: str, base_url: str, gatenavn: str) -> List[Tuple[int, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue

        text = a.get_text(" ", strip=True)
        score = _score_gatenavn_lenke(text, href, gatenavn)

        if score <= 0:
            continue

        candidates.append((score, urljoin(base_url, href), text))

    return candidates


def _søk_kandidat_sider(
    broker_domain: str, adresse: str, gatenavn: str
) -> Tuple[List[Tuple[int, str, str]], List[str]]:
    candidates = []
    attempted_urls = []
    query = quote_plus(adresse)

    search_templates = BROKER_SEARCH_OVERRIDES.get(broker_domain, GENERIC_SEARCH_URL_TEMPLATES)

    for template in search_templates:
        search_url = template.format(domain=broker_domain, query=query)
        attempted_urls.append(search_url)
        resp = _safe_get(search_url)
        time.sleep(BROKER_REQUEST_DELAY)

        if resp is None:
            continue

        candidates.extend(_finn_kandidat_lenker(resp.text, search_url, gatenavn))

    homepage_url = f"https://www.{broker_domain}/"
    attempted_urls.append(homepage_url)
    resp = _safe_get(homepage_url)
    time.sleep(BROKER_REQUEST_DELAY)

    if resp is not None:
        candidates.extend(_finn_kandidat_lenker(resp.text, homepage_url, gatenavn))

    return candidates, attempted_urls


def find_and_download_from_broker_site(
    broker_domain: str,
    adresse: str,
    postnummer: str = "",
    by: str = "",
    finn_ad_id: Optional[str] = None,
    download_dir: str = DEFAULT_DOWNLOAD_DIR,
) -> BrokerSiteResult:
    """Beste-innsats-forsøk på å finne samme bolig på meglerens egen nettside
    og laste ned salgsoppgave/prospekt derfra.

    Returnerer alltid en BrokerSiteResult - kaster aldri unntak videre.
    listing_url settes så snart en trygg adressematch er bekreftet, selv om
    ingen nedlastbar PDF blir funnet på den siden etterpå.
    """
    result = BrokerSiteResult()

    if not broker_domain:
        result.match_detail = "Mangler meglerdomene å søke hos."
        return result

    try:
        gatenavn, _husnummer = _parse_gatenavn_husnummer(adresse)

        if not gatenavn:
            result.match_detail = "Mangler adresse å søke etter hos megler."
            return result

        kandidat_lenker, attempted_urls = _søk_kandidat_sider(broker_domain, adresse, gatenavn)
        result.search_urls_attempted = attempted_urls

        if not kandidat_lenker:
            result.match_detail = (
                f"Fant ingen lenker med gatenavnet '{gatenavn}' hos megler {broker_domain}."
            )
            return result

        beste_per_url = {}
        for score, url, text in kandidat_lenker:
            if url not in beste_per_url or score > beste_per_url[url][0]:
                beste_per_url[url] = (score, text)

        sorterte_kandidater = sorted(
            beste_per_url.items(), key=lambda item: item[1][0], reverse=True
        )[:MAKS_KANDIDATER_Å_SJEKKE]

        result.candidate_urls = [url for url, _ in sorterte_kandidater]

        for listing_url, (_score, _text) in sorterte_kandidater:
            listing_resp = _safe_get(listing_url)
            time.sleep(BROKER_REQUEST_DELAY)

            if listing_resp is None:
                result.candidate_evaluations.append({
                    "url": listing_url,
                    "accepted": False,
                    "reason": "Klarte ikke å hente siden (nettverksfeil eller HTTP-feilkode).",
                })
                continue

            page_text = BeautifulSoup(listing_resp.text, "html.parser").get_text(" ", strip=True)
            match_ok, match_detail = _match_confidence(page_text, adresse, postnummer, by)
            result.candidate_evaluations.append({
                "url": listing_url, "accepted": match_ok, "reason": match_detail
            })

            if not match_ok:
                continue

            # Match bekreftet - lagre listing_url med en gang, uansett hva
            # som skjer videre med selve dokument-nedlastingen.
            result.listing_url = listing_url
            result.match_detail = match_detail

            if er_megler_side_solgt_eller_inaktiv(page_text):
                result.listing_sold_or_inactive = True
                result.match_detail += " Meglersiden indikerer at boligen er solgt/ikke lenger aktiv."
                return result

            documents = download_broker_documents(
                listing_resp.text, listing_url, finn_ad_id=finn_ad_id, download_dir=download_dir
            )
            result.documents = documents
            result.doc_links_found = [
                {"type": doc.doc_type, "url": doc.url, "text": doc.text, "download_status": doc.download_status}
                for doc in documents
            ]

            if any(doc.local_path for doc in documents):
                result.match_detail += " Dokument(er) funnet og lastet ned fra meglersiden."
            else:
                result.match_detail += " Fant meglerannonse, men ingen nedlastbare dokumenter der."

            return result

        result.match_detail = (
            f"Fant {len(sorterte_kandidater)} kandidat-lenke(r) hos megler {broker_domain}, "
            f"men ingen matchet adressen med god nok sikkerhet."
        )
        return result

    except Exception as e:  # megler-fallback skal aldri velte hoved-flyten
        result.match_detail = f"Uventet feil under megler-søk: {e}"
        return result
