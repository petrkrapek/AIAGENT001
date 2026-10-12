"""Czech business register ARES (ares.gov.cz): find companies and look them up by IČO."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..config import Config
from . import Tool, ToolError, schema, truncate

BASE_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty"

# Most common legal forms (ARES code -> name).
LEGAL_FORMS = {
    "101": "fyzická osoba podnikající (OSVČ)",
    "111": "v.o.s.",
    "112": "s.r.o.",
    "113": "k.s.",
    "121": "a.s.",
    "205": "družstvo",
    "706": "spolek",
}


def _request(url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("popis", "")
        except (ValueError, AttributeError):
            detail = ""
        if exc.code == 404:
            raise ToolError("Subjekt v ARES nenalezen.") from exc
        raise ToolError(f"ARES vrátil chybu {exc.code}. {detail}".strip()) from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"ARES je nedostupný: {exc.reason}") from exc


def _summary(subject: dict[str, Any]) -> str:
    sidlo = subject.get("sidlo") or {}
    legal = str(subject.get("pravniForma") or "")
    parts = [
        f"{subject.get('obchodniJmeno', '?')} (IČO {subject.get('ico', '—')})",
        f"sídlo: {sidlo.get('textovaAdresa', '—')}",
        f"forma: {LEGAL_FORMS.get(legal, legal or '—')}",
        f"vznik: {subject.get('datumVzniku', '—')}",
    ]
    if subject.get("datumZaniku"):
        parts.append(f"ZANIKLÝ {subject['datumZaniku']}")
    return " | ".join(parts)


def build(config: Config) -> list[Tool]:
    def ares_search(
        name: str | None = None,
        city: str | None = None,
        nace: list[str] | None = None,
        legal_form: list[str] | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> str:
        payload: dict[str, Any] = {"start": max(0, offset), "pocet": max(1, min(limit, 50))}
        if name:
            payload["obchodniJmeno"] = name
        if city:
            payload["sidlo"] = {"textovaAdresa": city}
        if nace:
            payload["czNace"] = nace
        if legal_form:
            payload["pravniForma"] = legal_form
        if len(payload) == 2:
            raise ToolError("Zadejte alespoň jeden filtr (name, city, nace nebo legal_form).")

        data = _request(f"{BASE_URL}/vyhledat", payload)
        if data.get("kod"):  # e.g. too many results
            raise ToolError(f"ARES: {data.get('popis', data['kod'])}. Zužte hledání dalšími filtry.")
        subjects = data.get("ekonomickeSubjekty") or []
        total = data.get("pocetCelkem", len(subjects))
        if not subjects:
            return "Nic nenalezeno."
        lines = [f"Nalezeno celkem {total}, zobrazeno {offset + 1}–{offset + len(subjects)}:"]
        lines += [f"- {_summary(s)}" for s in subjects]
        return truncate("\n".join(lines))

    def ares_company(ico: str) -> str:
        ico = ico.strip().replace(" ", "")
        if not ico.isdigit() or len(ico) > 8:
            raise ToolError("IČO musí mít až 8 číslic.")
        subject = _request(f"{BASE_URL}/{ico.zfill(8)}")
        lines = [_summary(subject)]
        if subject.get("dic"):
            lines.append(f"DIČ: {subject['dic']}")
        nace_codes = subject.get("czNace2008") or subject.get("czNace") or []
        if nace_codes:
            lines.append(f"CZ-NACE: {', '.join(str(code) for code in nace_codes)}")
        return "\n".join(lines)

    return [
        Tool(
            name="ares_search",
            description=(
                "Vyhledá české firmy a podnikatele v obchodním rejstříku ARES. Filtry lze kombinovat: "
                "name (část obchodního jména, např. 'stavební', 'software'), city (město sídla), "
                "nace (dvoumístné oddíly CZ-NACE, např. ['62'] IT, ['41','43'] stavebnictví, "
                "['69'] právní a účetní činnosti, ['56'] restaurace), legal_form (kódy: 112 s.r.o., "
                "121 a.s., 101 OSVČ). Vrací max. 50 výsledků na stránku, dál listuj přes offset; "
                "při více než 1000 výsledcích je potřeba hledání zúžit. Filtr nace je v ARES "
                "neúplný (najde jen část firem z oboru), proto ho kombinuj s klíčovými slovy v názvu "
                "a s web_search. ARES neobsahuje e-maily ani weby; ty dohledej přes web_search."
            ),
            input_schema=schema(
                {
                    "name": {"type": "string", "description": "Část obchodního jména."},
                    "city": {"type": "string", "description": "Město sídla, např. 'Brno'."},
                    "nace": {"type": "array", "items": {"type": "string"},
                             "description": "Dvoumístné oddíly CZ-NACE, např. ['62']."},
                    "legal_form": {"type": "array", "items": {"type": "string"},
                                   "description": "Kódy právní formy, např. ['112'] pro s.r.o."},
                    "offset": {"type": "integer", "description": "Posun pro stránkování, výchozí 0."},
                    "limit": {"type": "integer", "description": "1–50, výchozí 20"},
                }
            ),
            handler=ares_search,
        ),
        Tool(
            name="ares_company",
            description="Detail firmy z ARES podle IČO: sídlo, právní forma, datum vzniku, DIČ, obory CZ-NACE.",
            input_schema=schema({"ico": {"type": "string", "description": "IČO (až 8 číslic)."}}, ["ico"]),
            handler=ares_company,
        ),
    ]
