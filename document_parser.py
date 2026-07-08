"""Leser en salgsoppgave-PDF og trekker ut strukturert byggteknisk informasjon.

Dette er IKKE en AI-oppsummering. Alt er deterministisk, regelbasert
tekstsøk: finn overskrifter/nøkkelord for hver bygningskomponent, se etter en
TG-grad (TG0-TG3/TGIU) i nærheten, og plukk med et kort utdrag som merknad.
Ingenting gjettes - hvis en komponent ikke nevnes i teksten, utelates den helt
fra resultatet.

For å forbedre gjenkjenningen over tid: utvid COMPONENT_HEADINGS eller
KEYWORDS, eller juster mønstrene i TG_PATTERN/YEAR_PATTERN/COST_PATTERN.
Ingen annen del av modulen trenger å endres for å støtte nye ord/fraser.
"""

import re
from typing import List, Optional

from pypdf import PdfReader

# Komponent-nøkkel -> overskrifter/nøkkelfraser som identifiserer seksjonen i
# teksten. Rekkefølgen på nøklene er også visningsrekkefølgen i UI.
COMPONENT_HEADINGS = {
    "bad": ["våtrom", "bad"],
    "kjokken": ["kjøkken"],
    "tak": ["taktekking", "takkonstruksjon", "tak"],
    "vinduer": ["vinduer og dører", "vinduer"],
    "drenering": ["drenering"],
    "kjeller": ["kjeller", "krypkjeller"],
    "elektrisk_anlegg": ["elektrisk anlegg", "elanlegg", "elektro"],
    "ror_vvs": ["rør og vvs", "vvs-installasjoner", "rør/vvs", "sanitær"],
    "ventilasjon": ["ventilasjon"],
    "yttervegger": ["yttervegger", "yttervegg"],
    "grunnmur": ["grunnmur og fundamenter", "grunnmur"],
    "balkong_terrasse": ["balkong/terrasse", "terrasse", "balkong"],
    "pipe_ildsted": ["skorstein og ildsted", "pipe og ildsted", "ildsted", "pipe"],
    "radon": ["radon"],
}

# Pen visningstekst for hver komponent-nøkkel, brukt av UI-en i app.py.
COMPONENT_DISPLAY_NAVN = {
    "bad": "Bad / Våtrom",
    "kjokken": "Kjøkken",
    "tak": "Tak",
    "vinduer": "Vinduer",
    "drenering": "Drenering",
    "kjeller": "Kjeller",
    "elektrisk_anlegg": "Elektrisk anlegg",
    "ror_vvs": "Rør / VVS",
    "ventilasjon": "Ventilasjon",
    "yttervegger": "Yttervegger",
    "grunnmur": "Grunnmur",
    "balkong_terrasse": "Balkong / Terrasse",
    "pipe_ildsted": "Pipe / Ildsted",
    "radon": "Radon",
}

# Rekkefølgen komponentene skal vises i (samme rekkefølge som kravspesifikasjonen).
COMPONENT_ORDER = list(COMPONENT_HEADINGS.keys())

# Viktige stikkord som samles i en egen liste uansett hvilken komponent de
# dukker opp under (eller om de dukker opp helt uavhengig av en komponent).
KEYWORDS = [
    "fukt",
    "lekkasje",
    "råte",
    "setningsskader",
    "sprekk",
    "fuktskade",
    "manglende dokumentasjon",
    "ikke undersøkt",
    "utbedring anbefales",
    "avvik",
    "høy risiko",
]

TG_PATTERN = re.compile(r"\bTG\s*-?\s*(0|1|2|3)\b|\bTGIU\b", re.IGNORECASE)
YEAR_PATTERN = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
COST_PATTERN = re.compile(
    r"(?:kr\.?|NOK)\s?([\d][\d\s.,]{2,})|([\d][\d\s.,]{2,})\s?(?:kr\.?|NOK)\b",
    re.IGNORECASE,
)

# Hvor mange tegn en seksjon maks strekker seg hvis neste overskrift er langt unna.
MAKS_SEKSJON_LENGDE = 1200


def extract_text_from_pdf(pdf_path: str) -> str:
    """Leser all tekst fra PDF-en, side for side. Returnerer tom streng ved
    lesefeil eller hvis PDF-en ikke inneholder uttrekkbar tekst (f.eks. rene
    skannede bilder) - kaster aldri unntak videre."""
    try:
        reader = PdfReader(pdf_path)
    except Exception:
        return ""

    sider = []
    for page in reader.pages:
        try:
            sider.append(page.extract_text() or "")
        except Exception:
            continue

    return "\n".join(sider)


def _finn_overskrifter(text_l: str) -> List[tuple]:
    """Finner (start, slutt, komponent) for hvert treff av en komponent-
    overskrift i teksten, sortert etter posisjon i dokumentet.

    Krever at treffet står i starten av en linje (evt. med en kort
    tall-/punktum-prefiks som "3.2 "), slik at f.eks. "grunnmur" i løpende
    tekst ("...fukt i grunnmur...") ikke feiltolkes som overskriften
    "Grunnmur". PDF-tekstuttrekk legger som regel overskrifter på egen linje,
    så dette er en enkel og robust heuristikk.
    """
    treff = []

    for komponent, overskrifter in COMPONENT_HEADINGS.items():
        for overskrift in overskrifter:
            pattern = re.compile(r"(?m)^[ \t]*(?:[\d.]{1,6}[ \t]*)?" + re.escape(overskrift.lower()))
            for m in pattern.finditer(text_l):
                treff.append((m.start(), m.end(), komponent))

    treff.sort(key=lambda t: t[0])
    return treff


def _hent_seksjonstekst(text: str, alle_treff: List[tuple], indeks: int) -> str:
    """Teksten fra denne overskriften til neste overskrift som tilhører en
    ANNEN komponent (hopper over gjentatte treff på samme komponent rett
    etter hverandre, f.eks. en overskrift etterfulgt av samme ord i løpende
    tekst), begrenset til MAKS_SEKSJON_LENGDE tegn."""
    start = alle_treff[indeks][1]
    komponent = alle_treff[indeks][2]

    slutt = min(len(text), start + MAKS_SEKSJON_LENGDE)

    for j in range(indeks + 1, len(alle_treff)):
        if alle_treff[j][2] != komponent:
            slutt = min(alle_treff[j][0], start + MAKS_SEKSJON_LENGDE)
            break

    return text[start:slutt]


def _normaliser_tg(match: "re.Match") -> str:
    if match.group(1):
        return f"TG{match.group(1)}"
    return "TGIU"


def _er_triviell_setning(setning: str) -> bool:
    """En setning som ikke inneholder noe utover selve TG-koden (f.eks. "TG2.")
    er ikke en nyttig merknad i seg selv - lag heller merknaden av setningen(e)
    som følger etter."""
    uten_tg = TG_PATTERN.sub("", setning).strip(" .:-")
    return len(uten_tg) < 4


def _hent_kort_merknad(seksjon: str, tg_match: Optional["re.Match"]) -> Optional[str]:
    """Plukker en kort merknad - fortrinnsvis setningen(e) rundt TG-graden,
    ellers de første setningene i seksjonen. Hopper over trivielle setninger
    som bare består av selve TG-koden ("TG2.")."""
    setninger = [s.strip() for s in re.split(r"(?<=[.!?])\s+", seksjon.strip()) if s.strip()]

    if not setninger:
        return None

    start_idx = 0
    if tg_match:
        for idx, s in enumerate(setninger):
            if tg_match.group(0).lower() in s.lower():
                start_idx = idx
                break

    meningsfulle = [s for s in setninger[start_idx:] if not _er_triviell_setning(s)]
    if not meningsfulle:
        meningsfulle = [s for s in setninger if not _er_triviell_setning(s)] or setninger

    merknad = " ".join(meningsfulle[:2]).strip()
    return merknad[:250] if merknad else None


def _hent_kostnad(seksjon: str) -> Optional[str]:
    match = COST_PATTERN.search(seksjon)
    if not match:
        return None

    belop = (match.group(1) or match.group(2) or "").strip().strip(".,")
    if not belop:
        return None

    return f"{belop} kr"


def analyser_seksjon(seksjon: str) -> dict:
    """Trekker ut tg/remark/year/cost fra en enkelt seksjonstekst. Inkluderer
    kun nøkler for det som faktisk ble funnet."""
    resultat = {}

    tg_match = TG_PATTERN.search(seksjon)
    if tg_match:
        resultat["tg"] = _normaliser_tg(tg_match)

    remark = _hent_kort_merknad(seksjon, tg_match)
    if remark:
        resultat["remark"] = remark

    year_match = YEAR_PATTERN.search(seksjon)
    if year_match:
        resultat["year"] = int(year_match.group(1))

    cost = _hent_kostnad(seksjon)
    if cost:
        resultat["cost"] = cost

    return resultat


def finn_nokkelord(text_l: str) -> List[str]:
    """Returnerer stikkordene fra KEYWORDS som faktisk forekommer i teksten,
    i samme rekkefølge som KEYWORDS-listen."""
    return [kw for kw in KEYWORDS if kw in text_l]


def analyze_document_text(text: str) -> dict:
    """Kjernelogikken - ren tekst inn, strukturert dict ut. Brukes både av
    analyze_salgsoppgave_pdf() og direkte av tester (uten å måtte lage en
    ekte PDF-fil)."""
    if not text or not text.strip():
        return {}

    text_l = text.lower()
    alle_treff = _finn_overskrifter(text_l)

    resultat = {}

    for komponent in COMPONENT_ORDER:
        beste = None

        for i, (_start, _end, k) in enumerate(alle_treff):
            if k != komponent:
                continue

            seksjon = _hent_seksjonstekst(text, alle_treff, i)
            analyse = analyser_seksjon(seksjon)

            if not analyse:
                continue

            if "tg" in analyse:
                beste = analyse
                break

            if beste is None:
                beste = analyse

        if beste:
            resultat[komponent] = beste

    nokkelord = finn_nokkelord(text_l)
    if nokkelord:
        resultat["keywords"] = nokkelord

    return resultat


def analyze_salgsoppgave_pdf(pdf_path: str) -> dict:
    """Hovedinngang: leser PDF-en på pdf_path og returnerer en strukturert
    analyse-dict (se modulens docstring for format). Returnerer alltid en
    dict - tom hvis ingenting kunne leses/gjenkjennes - og kaster aldri
    unntak videre til kalleren.
    """
    try:
        text = extract_text_from_pdf(pdf_path)
        return analyze_document_text(text)
    except Exception:
        return {}
