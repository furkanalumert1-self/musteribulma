"""
Vercel serverless endpoint — Async Apify pattern

POST /api/scrape           → Apify run başlat, run_id döndür (< 2s)
GET  /api/status/<run_id>  → Apify run durumunu sorgula    (< 3s)
GET  /api/results/<run_id> → Sonuçları çek ve puanla       (< 10s)

Body (JSON): {"query": "...", "location": "...", "max": 50}
Env:         APIFY_API_TOKEN
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any

from flask import Flask, make_response, request

MAX_LEADS_CAP = 50
DEFAULT_ACTOR = "compass/crawler-google-places"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_DIGITS_RE = re.compile(r"\d{5,}")

app = Flask(__name__)


# ── normalisation ────────────────────────────────────────────────────────────

def _valid_email(raw: str) -> bool:
    """Telefon+email yapışık gelen sahte adresleri (ör. +905551234info@x.com) eler."""
    local = raw.split("@")[0]
    return not local.startswith("+") and not _DIGITS_RE.search(local)


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
        if e2 and e2 not in seen and _valid_email(e2):
            seen.add(e2)
            out.append(e2)
    return out


def _normalize_phone(p: str) -> str:
    """Karşılaştırma için sadece rakamları bırakır (boşluk/tire/parantez siler)."""
    return re.sub(r"[^\d+]", "", p)


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
    seen_norm: set[str] = set()
    out: list[str] = []
    for p in phones:
        p2 = p.strip()
        if not p2:
            continue
        norm = _normalize_phone(p2)
        if norm and norm not in seen_norm:
            seen_norm.add(norm)
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


# ── apify helpers ────────────────────────────────────────────────────────────

_APIFY_TERMINAL = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
_APIFY_RUNNING  = {"READY", "RUNNING"}


def _apify_client():
    from apify_client import ApifyClient  # noqa: PLC0415
    api_key = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not api_key:
        raise ValueError("APIFY_API_TOKEN ortam değişkeni ayarlanmamış.")
    return ApifyClient(api_key)


def _to_dict(obj) -> dict | None:
    """apify-client >=1.7 Pydantic modelini dict'e çevirir; alias'ları korur."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, mode="json")
    return vars(obj)


# ── Flask routes ─────────────────────────────────────────────────────────────

import os as _os

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _cors(resp: Any) -> Any:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _json(data: Any, code: int = 200) -> Any:
    return _cors(make_response(
        json.dumps(data, ensure_ascii=False),
        code,
        {"Content-Type": "application/json; charset=utf-8"},
    ))


def _send_html(filename: str):
    from flask import send_file as _sf  # noqa: PLC0415
    path = _os.path.join(_ROOT, filename)
    if _os.path.exists(path):
        return _sf(path, mimetype="text/html")
    return make_response("Not found", 404)


# ── static pages ─────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def home():
    return _send_html("index.html")


@app.route("/dashboard.html", methods=["GET"])
def dashboard():
    return _send_html("dashboard.html")


@app.route("/health", methods=["GET"])
def health():
    return _json({"status": "healthy", "app": "musteribulma"})


# ── POST /api/scrape  →  Apify run başlat, hemen run_id döndür (~1s) ─────────

@app.route("/api/scrape", methods=["POST", "OPTIONS"])
def start_scrape():
    if request.method == "OPTIONS":
        return _cors(make_response("", 200))

    data: dict[str, Any] = request.get_json(force=True, silent=True) or {}
    query    = (data.get("query") or "").strip()
    location = (data.get("location") or "").strip()
    try:
        max_results = max(1, min(int(data.get("max", MAX_LEADS_CAP)), MAX_LEADS_CAP))
    except (TypeError, ValueError):
        max_results = MAX_LEADS_CAP
    language = data.get("language", "tr")

    if not query or not location:
        return _json({"error": "query ve location zorunludur."}, 400)

    try:
        client = _apify_client()
        run = client.actor(DEFAULT_ACTOR).start(run_input={
            "searchStringsArray": [query],
            "locationQuery": location,
            "maxCrawledPlacesPerSearch": max_results,
            "language": language,
            "skipClosedPlaces": True,
            "scrapeContacts": True,
        })
    except ValueError as exc:
        return _json({"error": str(exc)}, 500)
    except Exception as exc:
        return _json({"error": f"Apify başlatılamadı: {exc}"}, 500)

    run_id = run.get("id") if isinstance(run, dict) else getattr(run, "id", None)
    if not run_id:
        return _json({"error": "Apify run id alınamadı."}, 500)

    return _json({
        "run_id": run_id,
        "status": "RUNNING",
        "query": query,
        "location": location,
        "max": max_results,
        "language": language,
    })


# ── GET /api/status/<run_id>  →  Apify run durumunu sorgula (~1s) ────────────

@app.route("/api/status/<run_id>", methods=["GET"])
def scrape_status(run_id: str):
    try:
        client = _apify_client()
        run_info = _to_dict(client.run(run_id).get())
        if not run_info:
            return _json({"error": "Run bilgisi alınamadı."}, 500)
        status = run_info.get("status", "UNKNOWN")
        stats  = run_info.get("stats") or {}
        if isinstance(stats, object) and hasattr(stats, "get") is False:
            stats = {}
        return _cors(make_response(
            json.dumps({
                "run_id": run_id,
                "status": status,
                "done": status in _APIFY_TERMINAL,
                "items_scraped": stats.get("itemsScraped", 0) if isinstance(stats, dict) else 0,
            }, ensure_ascii=False),
            200,
            {"Content-Type": "application/json"},
        ))
    except ValueError as exc:
        return _json({"error": str(exc)}, 500)
    except Exception as exc:
        return _json({"error": f"Durum sorgulanamadı: {exc}"}, 500)


# ── GET /api/results/<run_id>  →  Sonuçları çek ve puanla (~5s) ──────────────

@app.route("/api/results/<run_id>", methods=["GET"])
def scrape_results(run_id: str):
    query    = request.args.get("query", "")
    location = request.args.get("location", "")
    language = request.args.get("language", "tr")

    try:
        client   = _apify_client()
        run_info = _to_dict(client.run(run_id).get())
    except ValueError as exc:
        return _json({"error": str(exc)}, 500)
    except Exception as exc:
        return _json({"error": f"Run bilgisi alınamadı: {exc}"}, 500)

    if not run_info:
        return _json({"error": "Run bilgisi alınamadı."}, 500)

    status = run_info.get("status", "")
    if status != "SUCCEEDED":
        return _json({"error": f"Run henüz tamamlanmadı (status: {status})."}, 400)

    dataset_id = run_info.get("defaultDatasetId")
    if not dataset_id:
        return _json({"error": "Dataset id bulunamadı."}, 500)

    try:
        items: list[dict] = []
        for it in client.dataset(dataset_id).iterate_items():
            items.append(it)
            if len(items) >= MAX_LEADS_CAP:
                break
    except Exception as exc:
        return _json({"error": f"Veri çekilemedi: {exc}"}, 500)

    leads = [score_lead(normalize(x), query) for x in items]
    leads.sort(key=lambda x: x.get("toplam_skor", 0), reverse=True)
    for i, lead in enumerate(leads, 1):
        lead["rank"] = i

    return _json({
        "run_id": run_id,
        "query": query,
        "location": location,
        "language": language,
        "max_limit": MAX_LEADS_CAP,
        "count": len(leads),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "actor": DEFAULT_ACTOR,
        "leads": leads,
    })
