"""
Vercel serverless endpoint — POST /api/scrape

Body (JSON): {"query": "...", "location": "...", "max": 50}
Env:         APIFY_API_TOKEN
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from typing import Any

from flask import Flask, make_response, request

MAX_LEADS_CAP = 50
DEFAULT_ACTOR = "compass/crawler-google-places"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

app = Flask(__name__)


# ── normalisation ────────────────────────────────────────────────────────────

def _collect_emails(item: dict) -> list[str]:
    emails: list[str] = []
    for key in ("emails", "email"):
        v = item.get(key)
        if isinstance(v, list):
            emails.extend(e for e in v if isinstance(e, str))
        elif isinstance(v, str) and v:
            emails.append(v)
    contacts = item.get("contacts") or {}
    if isinstance(contacts, dict):
        for k in ("emails", "email"):
            v = contacts.get(k)
            if isinstance(v, list):
                emails.extend(e for e in v if isinstance(e, str))
            elif isinstance(v, str) and v:
                emails.append(v)
    additional = item.get("additionalInfo") or {}
    if isinstance(additional, dict):
        emails.extend(EMAIL_RE.findall(json.dumps(additional, ensure_ascii=False)))
    seen: set[str] = set()
    out: list[str] = []
    for e in emails:
        e2 = e.strip().lower()
        if e2 and e2 not in seen:
            seen.add(e2)
            out.append(e2)
    return out


def _collect_phones(item: dict) -> list[str]:
    phones: list[str] = []
    for key in ("phones", "phone", "phoneUnformatted"):
        v = item.get(key)
        if isinstance(v, list):
            phones.extend(p for p in v if isinstance(p, str))
        elif isinstance(v, str) and v:
            phones.append(v)
    contacts = item.get("contacts") or {}
    if isinstance(contacts, dict):
        v = contacts.get("phones")
        if isinstance(v, list):
            phones.extend(p for p in v if isinstance(p, str))
    seen: set[str] = set()
    out: list[str] = []
    for p in phones:
        p2 = p.strip()
        if p2 and p2 not in seen:
            seen.add(p2)
            out.append(p2)
    return out


def _real_website(item: dict) -> str | None:
    w = item.get("website")
    if isinstance(w, str) and w.strip() and "google.com/maps" not in w:
        return w.strip()
    return None


def normalize(item: dict) -> dict:
    return {
        "name": item.get("title") or item.get("name"),
        "category": item.get("categoryName") or item.get("category"),
        "website": _real_website(item),
        "emails": _collect_emails(item),
        "phones": _collect_phones(item),
        "address": item.get("address"),
        "city": item.get("city"),
        "neighborhood": item.get("neighborhood"),
        "rating": item.get("totalScore") or item.get("rating"),
        "reviews_count": item.get("reviewsCount") or item.get("reviews"),
        "description": (item.get("description") or "").strip() or None,
        "claimed": item.get("claimed"),
        "permanently_closed": item.get("permanentlyClosed"),
        "google_url": item.get("url"),
        "place_id": item.get("placeId") or item.get("place_id"),
    }


# ── scoring ──────────────────────────────────────────────────────────────────

def score_lead(lead: dict, query: str = "") -> dict:
    e = 2
    if lead.get("emails"):
        e += 4
    if lead.get("phones"):
        e += 2
    if lead.get("website"):
        e += 2
    erisilebilirlik = min(10, e)

    rating = lead.get("rating") or 0
    reviews = lead.get("reviews_count") or 0
    sinyal: float = round(min(10.0, float(rating) * 2), 1) if rating else 4.0
    if reviews >= 100:
        sinyal = min(10.0, sinyal + 1)
    elif reviews >= 50:
        sinyal = min(10.0, sinyal + 0.5)

    su = 7.0
    category = (lead.get("category") or "").lower()
    query_words = [w for w in query.lower().split() if len(w) > 2]
    if any(w in category for w in query_words):
        su = min(10.0, su + 1)
    if lead.get("claimed"):
        su = min(10.0, su + 1)
    if lead.get("website"):
        su = min(10.0, su + 1)

    toplam = round(su * 0.5 + erisilebilirlik * 0.3 + sinyal * 0.2, 1)

    bits = []
    if lead.get("emails"):
        bits.append("email var")
    if lead.get("phones"):
        bits.append("tel var")
    if lead.get("website"):
        bits.append("website açık")
    r = lead.get("rating")
    if isinstance(r, (int, float)) and r >= 4.0:
        bits.append(f"{r:.1f}★")
    if reviews and reviews >= 50:
        bits.append(f"{int(reviews)} yorum")
    if not bits:
        bits.append("Maps'te aktif")
    note = f"{lead.get('name') or 'Firma'}: {', '.join(bits[:3])} — outreach için uygun."

    return {
        **lead,
        "sektor_uyumu": round(su, 1),
        "erisilebilirlik": float(erisilebilirlik),
        "sinyal": sinyal,
        "toplam_skor": toplam,
        "not": note,
    }


# ── apify scrape ─────────────────────────────────────────────────────────────

def run_scrape(query: str, location: str, max_results: int, language: str = "tr") -> list[dict]:
    from apify_client import ApifyClient  # noqa: PLC0415

    api_key = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not api_key:
        raise ValueError("APIFY_API_TOKEN ortam değişkeni ayarlanmamış.")

    max_results = max(1, min(max_results, MAX_LEADS_CAP))
    client = ApifyClient(api_key)

    run = client.actor(DEFAULT_ACTOR).call(
        run_input={
            "searchStringsArray": [query],
            "locationQuery": location,
            "maxCrawledPlacesPerSearch": max_results,
            "language": language,
            "skipClosedPlaces": True,
            "scrapeContacts": True,
        },
        run_timeout=timedelta(minutes=8),
        wait_duration=timedelta(minutes=8),
    )

    dataset_id = None
    if run is not None:
        dataset_id = (run.get("defaultDatasetId") if isinstance(run, dict)
                      else getattr(run, "default_dataset_id", None))
    if not dataset_id:
        raise RuntimeError("Apify çalıştırılamadı veya dataset id dönmedi.")

    items: list[dict] = []
    for it in client.dataset(dataset_id).iterate_items():
        items.append(it)
        if len(items) >= max_results:
            break
    return items[:MAX_LEADS_CAP]


# ── Flask route ───────────────────────────────────────────────────────────────

def _cors(resp: Any) -> Any:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/", methods=["POST", "OPTIONS"])
@app.route("/api/scrape", methods=["POST", "OPTIONS"])
def scrape_endpoint():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))

    try:
        data: dict[str, Any] = request.get_json(force=True, silent=True) or {}
    except Exception:
        return _cors(make_response(
            json.dumps({"error": "Geçersiz JSON."}, ensure_ascii=False), 400,
            {"Content-Type": "application/json"}
        ))

    query = (data.get("query") or "").strip()
    location = (data.get("location") or "").strip()
    try:
        max_results = max(1, min(int(data.get("max", MAX_LEADS_CAP)), MAX_LEADS_CAP))
    except (TypeError, ValueError):
        max_results = MAX_LEADS_CAP
    language = data.get("language", "tr")

    if not query or not location:
        return _cors(make_response(
            json.dumps({"error": "query ve location zorunludur."}, ensure_ascii=False), 400,
            {"Content-Type": "application/json"}
        ))

    try:
        raw = run_scrape(query, location, max_results, language)
    except ValueError as exc:
        return _cors(make_response(
            json.dumps({"error": str(exc)}, ensure_ascii=False), 500,
            {"Content-Type": "application/json"}
        ))
    except Exception as exc:
        return _cors(make_response(
            json.dumps({"error": f"Tarama başarısız: {exc}"}, ensure_ascii=False), 500,
            {"Content-Type": "application/json"}
        ))

    leads = [score_lead(normalize(x), query) for x in raw]
    leads.sort(key=lambda x: x.get("toplam_skor", 0), reverse=True)
    for i, lead in enumerate(leads, 1):
        lead["rank"] = i

    result = {
        "query": query,
        "location": location,
        "language": language,
        "max_limit": MAX_LEADS_CAP,
        "requested": max_results,
        "count": len(leads),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "actor": DEFAULT_ACTOR,
        "leads": leads,
    }
    return _cors(make_response(
        json.dumps(result, ensure_ascii=False),
        200,
        {"Content-Type": "application/json; charset=utf-8"}
    ))
