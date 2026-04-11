"""Flight price monitoring via Amadeus Self-Service API (free tier)."""
from __future__ import annotations

import os
import time
from typing import Any

import httpx

_TOKEN_CACHE: dict[str, Any] = {"token": None, "expires_at": 0}
_BASE = "https://api.amadeus.com"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _credentials() -> tuple[str, str] | None:
    key = os.environ.get("AMADEUS_API_KEY", "").strip()
    secret = os.environ.get("AMADEUS_API_SECRET", "").strip()
    if not key or not secret:
        return None
    return key, secret


def get_amadeus_token() -> str | None:
    """OAuth2 client-credentials token, cached until expiry."""
    if _TOKEN_CACHE["token"] and time.time() < _TOKEN_CACHE["expires_at"] - 60:
        return _TOKEN_CACHE["token"]

    creds = _credentials()
    if not creds:
        return None

    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.post(
            f"{_BASE}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": creds[0],
                "client_secret": creds[1],
            },
        )
        r.raise_for_status()
        data = r.json()
        _TOKEN_CACHE["token"] = data["access_token"]
        _TOKEN_CACHE["expires_at"] = time.time() + data.get("expires_in", 1799)
        return _TOKEN_CACHE["token"]


def search_flights(
    origin: str,
    destination: str,
    date: str,
    return_date: str | None = None,
    max_price: float | None = None,
    airline_pref: str | None = None,
    max_results: int = 5,
) -> dict:
    """Search Amadeus flight-offers.

    Returns dict with keys: lowest_price, currency, airline, departure, offers_count.
    """
    token = get_amadeus_token()
    if not token:
        raise RuntimeError("AMADEUS_API_KEY / AMADEUS_API_SECRET not set")

    params: dict[str, Any] = {
        "originLocationCode": origin.upper(),
        "destinationLocationCode": destination.upper(),
        "departureDate": date,
        "adults": 1,
        "currencyCode": "INR",
        "max": max_results,
    }
    if return_date:
        params["returnDate"] = return_date
    if max_price:
        params["maxPrice"] = int(max_price)

    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=_TIMEOUT) as c:
        r = c.get(f"{_BASE}/v2/shopping/flight-offers", params=params, headers=headers)
        r.raise_for_status()
        data = r.json()

    offers = data.get("data", [])
    if not offers:
        return {"lowest_price": None, "currency": "INR", "airline": None,
                "departure": None, "offers_count": 0}

    if airline_pref:
        pref_upper = airline_pref.upper()
        filtered = [o for o in offers if any(
            seg.get("carrierCode", "").upper() == pref_upper
            for itin in o.get("itineraries", [])
            for seg in itin.get("segments", [])
        )]
        if filtered:
            offers = filtered

    best = min(offers, key=lambda o: float(o.get("price", {}).get("total", "999999")))
    price = float(best["price"]["total"])
    currency = best["price"].get("currency", "INR")

    first_seg = (best.get("itineraries", [{}])[0]
                 .get("segments", [{}])[0])
    airline = first_seg.get("carrierCode", "")
    departure = first_seg.get("departure", {}).get("at", "")

    return {
        "lowest_price": price,
        "currency": currency,
        "airline": airline,
        "departure": departure,
        "offers_count": len(data.get("data", [])),
    }
