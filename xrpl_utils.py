"""XRPL helper functions for the XRP dashboard.

This module implements a few lightweight helpers for interacting
with the XRP Ledger via publicly available HTTP endpoints.  It
does not require a WebSocket connection and can be executed in
restricted environments without installing the official ``xrpl``
library.  Should you wish to swap in a more comprehensive
implementation later, the functions here provide a clear
interface for the rest of the application.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

import requests


def fetch_account_overview(account: str) -> Dict[str, Any]:
    """Fetch a basic overview of an XRPL account from Ripple's Data API.

    The Data API v2 provides a simple JSON endpoint for retrieving
    account metadata such as balance, transaction count and
    first/last activity.  See https://xrpscan.com/api for
    additional endpoints.  If the request fails, an empty
    dictionary is returned.

    Args:
        account: A classic address or X‑address.

    Returns:
        A dictionary with account information or an empty dict on failure.
    """

    # Normalise X‑addresses by stripping tags; the Data API accepts
    # classic addresses only.  A simple regex extract is used here.
    classic = parse_account_input(account).get("address", account)
    url = f"https://data.ripple.com/v2/accounts/{classic}"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()  # type: ignore[assignment]
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_account_input(value: str) -> Dict[str, str]:
    """Parse a user input into its classic address and tag components.

    Accepts either a classic address starting with 'r' or an X‑address
    (which encodes both the classic address and tag).  The tag is
    returned as a string; if absent it will be omitted from the
    result.  The function does not perform full base58 validation
    but attempts a best effort extraction using regular expressions.

    Args:
        value: User supplied account identifier.

    Returns:
        A dictionary containing at least the ``address`` key and
        optionally a ``tag`` if present.
    """

    value = (value or "").strip()
    result: Dict[str, str] = {}
    # Very basic heuristics: classic addresses start with 'r' and are
    # between 25 and 35 characters.  X‑addresses may start with 'X'.
    classic_match = re.match(r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$", value)
    if classic_match:
        result["address"] = value
        return result
    # Attempt to split X‑address into classic and tag.  The most
    # robust way would be to decode the Base58Check encoding but that
    # requires a dependency.  As a fallback we search for a tag
    # separated by a colon or hyphen.
    parts = re.split(r"[:|\-]", value)
    if parts:
        result["address"] = parts[0]
        if len(parts) > 1:
            result["tag"] = parts[1]
    return result