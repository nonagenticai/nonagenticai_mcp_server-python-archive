"""Polish VAT taxpayer whitelist (Biała lista, wl-api.mf.gov.pl): legal_vat_whitelist.

The MF API caps search calls per day (~100 per IP, not verified), so successful search answers
are cached in-process for 24 h per (nip, date).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from cachetools import TTLCache
from fastmcp.exceptions import ToolError

from ._server import legal_mcp
from .client import get_client

_NIP = re.compile(r"\d{10}")
_NRB = re.compile(r"\d{26}")
_NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)
_search_cache: TTLCache = TTLCache(maxsize=2048, ttl=24 * 3600)


def nip_digits(nip: object) -> str:
    raw = re.sub(r"[\s-]", "", nip) if isinstance(nip, str) else ""
    raw = raw.removeprefix("PL")
    if not _NIP.fullmatch(raw):
        raise ToolError(f"[permanent] invalid nip: {str(nip)[:40]!r} (expected 10 digits)")
    checksum = sum(int(d) * w for d, w in zip(raw, _NIP_WEIGHTS, strict=False)) % 11
    if checksum != int(raw[9]):
        raise ToolError(f"[permanent] invalid nip: {raw} (checksum mismatch)")
    return raw


def _account(account: str) -> str:
    raw = re.sub(r"\s", "", account).removeprefix("PL")
    if not _NRB.fullmatch(raw):
        raise ToolError("[permanent] invalid account: expected a 26-digit Polish NRB")
    return raw


def _date(value: str | None) -> str:
    if value is None:
        return datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as e:
        raise ToolError("[permanent] invalid date: expected YYYY-MM-DD") from e


def clear_cache() -> None:
    _search_cache.clear()


@legal_mcp.tool()
async def vat_whitelist(nip: str, date: str | None = None, account: str | None = None) -> dict:
    """Check a Polish VAT taxpayer on the Ministry of Finance whitelist by NIP: VAT status
    ("Czynny" active, "Zwolniony" exempt, or not registered), name, REGON, KRS, dates and the
    number of registered bank accounts. With `account` (26-digit NRB) it instead answers whether
    that account is registered for the NIP on the date (default today, Europe/Warsaw)."""
    nip = nip_digits(nip)
    on = _date(date)
    client = get_client("mf_wl")
    if account is not None:
        nrb = _account(account)
        r = await client.get_json(f"/api/check/nip/{nip}/bank-account/{nrb}", {"date": on})
        result = r.get("result") or {}
        return {
            "nip": nip,
            "date": on,
            "account": nrb,
            "account_assigned": result.get("accountAssigned") == "TAK",
            "request_id": result.get("requestId"),
        }
    key = (nip, on)
    cached = _search_cache.get(key)
    if cached is not None:
        return {**cached, "cached": True}
    r = await client.get_json(f"/api/search/nip/{nip}", {"date": on})
    result = r.get("result") or {}
    subject: dict[str, Any] = result.get("subject") or {}
    out = {
        "nip": nip,
        "date": on,
        "found": bool(subject),
        "name": subject.get("name"),
        "status_vat": subject.get("statusVat"),
        "regon": subject.get("regon"),
        "krs": subject.get("krs"),
        "registration_date": subject.get("registrationLegalDate"),
        "removal_date": subject.get("removalDate"),
        "accounts_count": len(subject.get("accountNumbers") or []),
        "request_id": result.get("requestId"),
    }
    _search_cache[key] = out
    return {**out, "cached": False}
