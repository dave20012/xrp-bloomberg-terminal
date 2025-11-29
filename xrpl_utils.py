"""XRPL helpers for account inspection using Ripple Data API."""

import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from app_utils import is_safe_url, safe_get

RIPPLE_DATA_BASE = "https://data.ripple.com/v2"
CLASSIC_ADDRESS_RE = re.compile(r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$")


def parse_account_input(raw: str, explicit_tag: Optional[int] = None) -> Tuple[Optional[str], Optional[int], List[str]]:
    """Return (classic_address, tag, notes) from user-provided account strings.

    Supports "r...:123", "r...?dt=123", and direct numeric tag inputs. X-addresses
    are detected and blocked until an offline-safe converter is available.
    """

    notes: List[str] = []
    if explicit_tag is not None and explicit_tag < 0:
        notes.append("Destination tag must be non-negative.")
        explicit_tag = None

    if not raw:
        notes.append("Provide a classic XRPL account to inspect.")
        return None, explicit_tag, notes

    candidate = raw.strip()
    tag = explicit_tag

    # Parse querystring-based tags (e.g., r...?...&dt=123)
    if "?" in candidate:
        pseudo_url = candidate if "://" in candidate else f"https://placeholder/{candidate}"
        parsed = urlparse(pseudo_url)
        candidate = parsed.path.lstrip("/")
        query = parse_qs(parsed.query)
        tag_value = query.get("dt") or query.get("tag")
        if tag_value and tag is None:
            try:
                tag = int(tag_value[0])
            except (TypeError, ValueError):
                notes.append("Destination tag must be numeric.")

    if ":" in candidate and tag is None:
        base, maybe_tag = candidate.split(":", 1)
        candidate = base
        maybe_tag = maybe_tag.strip()
        if maybe_tag.isdigit():
            tag = int(maybe_tag)
        elif maybe_tag:
            notes.append("Destination tag must be numeric.")

    candidate = candidate.strip()

    if candidate.startswith(("X", "T")):
        notes.append(
            "X-address detected; supply a classic address until offline conversion is enabled."
        )
        return None, tag, notes

    if not CLASSIC_ADDRESS_RE.match(candidate):
        notes.append("Classic address format invalid.")
        return None, tag, notes

    return candidate, tag, notes


def _clean_amount(obj: Any) -> float:
    try:
        return float(obj or 0.0)
    except Exception:
        return 0.0


def fetch_account_overview(address: str, *, limit: int = 5, timeout: int = 10) -> Dict[str, Any]:
    """Fetch account metadata, trustlines, offers, and recent transactions."""

    if os.getenv("SKIP_LIVE_FETCH") or not is_safe_url(RIPPLE_DATA_BASE):
        return {"account": None, "trustlines": [], "transactions": [], "offers": []}

    account = safe_get(f"{RIPPLE_DATA_BASE}/accounts/{address}", timeout=timeout) or {}
    trustlines = safe_get(
        f"{RIPPLE_DATA_BASE}/accounts/{address}/trustlines",
        params={"limit": limit},
        timeout=timeout,
    ) or {}
    offers = safe_get(
        f"{RIPPLE_DATA_BASE}/accounts/{address}/offers",
        params={"limit": limit},
        timeout=timeout,
    ) or {}
    transactions = safe_get(
        f"{RIPPLE_DATA_BASE}/accounts/{address}/transactions",
        params={"limit": limit, "result": "tesSUCCESS", "descending": True},
        timeout=timeout,
    ) or {}

    def _simplify_txs(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        txs: List[Dict[str, Any]] = []
        for entry in payload.get("transactions", []):
            tx = entry.get("tx") or {}
            txs.append(
                {
                    "hash": tx.get("hash"),
                    "type": tx.get("TransactionType"),
                    "amount": _clean_amount((tx.get("Amount") or {}).get("value") if isinstance(tx.get("Amount"), dict) else tx.get("Amount")),
                    "counterparty": tx.get("Destination") or tx.get("Account"),
                    "date": entry.get("date"),
                }
            )
        return txs

    def _simplify_offers(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        offers_list: List[Dict[str, Any]] = []
        for offer in payload.get("offers", []):
            taker_gets = offer.get("taker_gets_funded") or offer.get("taker_gets")
            taker_pays = offer.get("taker_pays_funded") or offer.get("taker_pays")
            offers_list.append(
                {
                    "sequence": offer.get("seq"),
                    "quality": offer.get("quality"),
                    "taker_gets": taker_gets,
                    "taker_pays": taker_pays,
                }
            )
        return offers_list

    return {
        "account": account.get("account_data") or account.get("account"),
        "trustlines": trustlines.get("lines", []),
        "transactions": _simplify_txs(transactions),
        "offers": _simplify_offers(offers),
    }
