"""Polish National Court Register (KRS) current extract: legal_krs_extract."""

from __future__ import annotations

import re
from typing import Any, Literal

from fastmcp.exceptions import ToolError

from ._server import legal_mcp
from .client import get_client

_KRS = re.compile(r"\d{1,10}")
MAX_ACTIVITIES = 10


def _address(adres: dict[str, Any]) -> str | None:
    if not adres:
        return None
    street = " ".join(p for p in (adres.get("ulica"), adres.get("nrDomu")) if p)
    if adres.get("nrLokalu"):
        street += f"/{adres['nrLokalu']}"
    city = " ".join(p for p in (adres.get("kodPocztowy"), adres.get("miejscowosc")) if p)
    return ", ".join(p for p in (street, city, adres.get("kraj")) if p) or None


def _activities(dzial3: dict[str, Any]) -> list[dict[str, Any]]:
    pd = dzial3.get("przedmiotDzialalnosci") or {}
    out: list[dict[str, Any]] = []
    for key, predominant in (
        ("przedmiotPrzewazajacejDzialalnosci", True),
        ("przedmiotPozostalejDzialalnosci", False),
    ):
        for a in pd.get(key) or []:
            code = ".".join(
                p for p in (a.get("kodDzial"), a.get("kodKlasa"), a.get("kodPodklasa")) if p
            )
            out.append({"code": code, "description": a.get("opis"), "predominant": predominant})
    return out[:MAX_ACTIVITIES]


def _representation(dzial2: dict[str, Any]) -> dict[str, Any]:
    rep = dzial2.get("reprezentacja") or {}
    if isinstance(rep, list):  # some entities list several bodies
        rep = rep[0] if rep else {}
    members = []
    for m in rep.get("sklad") or []:
        first = (m.get("imiona") or {}).get("imie")
        last = (m.get("nazwisko") or {}).get("nazwiskoICzlon")
        members.append(
            {
                "name": " ".join(p for p in (first, last) if p) or None,
                "function": m.get("funkcjaWOrganie"),
            }
        )
    return {
        "body": rep.get("nazwaOrganu"),
        "method": rep.get("sposobReprezentacji"),
        "members": members,
    }


@legal_mcp.tool()
async def krs_extract(krs: str, register: Literal["P", "S"] = "P") -> dict:
    """Current KRS extract of a Polish company / organisation by KRS number (register "P" =
    entrepreneurs, "S" = associations/foundations): name, legal form, NIP, REGON, address,
    share capital, who represents it and how, main business activities. Personal names are
    masked by the source."""
    if not isinstance(krs, str) or not _KRS.fullmatch(krs.strip()):
        raise ToolError(f"[permanent] invalid krs: {str(krs)[:40]!r} (expected up to 10 digits)")
    number = krs.strip().zfill(10)
    r = await get_client("krs").get_json(
        f"/api/krs/OdpisAktualny/{number}",
        {"rejestr": register, "format": "json"},
        not_found="KRS entry not found in this register",
    )
    odpis = r.get("odpis") or {}
    header = odpis.get("naglowekA") or {}
    dane = odpis.get("dane") or {}
    d1 = dane.get("dzial1") or {}
    podmiot = d1.get("danePodmiotu") or {}
    ids = podmiot.get("identyfikatory") or {}
    capital = (d1.get("kapital") or {}).get("wysokoscKapitaluZakladowego") or {}
    return {
        "krs": header.get("numerKRS", number),
        "as_of": header.get("stanZDnia"),
        "name": podmiot.get("nazwa"),
        "legal_form": podmiot.get("formaPrawna"),
        "nip": ids.get("nip"),
        "regon": ids.get("regon"),
        "address": _address((d1.get("siedzibaIAdres") or {}).get("adres") or {}),
        "capital": (
            {"amount": capital.get("wartosc"), "currency": capital.get("waluta")}
            if capital
            else None
        ),
        "representation": _representation(dane.get("dzial2") or {}),
        "business_activity": _activities(dane.get("dzial3") or {}),
    }
