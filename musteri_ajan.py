#!/usr/bin/env python3
"""
Müşteri Bulma Ajanı — Apify Google Maps tabanlı B2B lead toplama CLI'ı.
Claude Code skill'i tarafından orkestre edilir.

KATI KURAL: Her tarama maksimum 50 firma çeker. Bu limit hardcoded.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
MAX_LEADS_CAP = 50  # MUTLAK ÜST SINIR — DEĞİŞTİRİLEMEZ
DEFAULT_ACTOR = "compass/crawler-google-places"


# ----------------------------- env loading -----------------------------

def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def get_api_key() -> str:
    load_env()
    key = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not key:
        print(
            "[HATA] APIFY_API_TOKEN bulunamadı. .env dosyasına ekle "
            "veya `export APIFY_API_TOKEN=...` ile ayarla.",
            file=sys.stderr,
        )
        sys.exit(1)
    return key


# ----------------------------- rich helpers -----------------------------

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

    console = Console()
    HAS_RICH = True
except ImportError:
    HAS_RICH = False
    console = None  # type: ignore


def banner() -> None:
    if not HAS_RICH:
        print("=== MÜŞTERİ BULMA AJANI ===")
        return
    txt = Text()
    txt.append("MÜŞTERİ BULMA AJANI", style="bold cyan")
    txt.append("  ·  ", style="dim")
    txt.append("Apify + Claude Code", style="yellow")
    txt.append("\n", style="")
    txt.append(f"Hard limit: {MAX_LEADS_CAP} firma / tarama", style="dim")
    console.print(Panel(txt, border_style="cyan", padding=(0, 2)))


def info(msg: str) -> None:
    if HAS_RICH:
        console.print(f"[cyan]→[/cyan] {msg}")
    else:
        print(f"-> {msg}")


def ok(msg: str) -> None:
    if HAS_RICH:
        console.print(f"[green]✓[/green] {msg}")
    else:
        print(f"[ok] {msg}")


def warn(msg: str) -> None:
    if HAS_RICH:
        console.print(f"[yellow]![/yellow] {msg}")
    else:
        print(f"[warn] {msg}")


def err(msg: str) -> None:
    if HAS_RICH:
        console.print(f"[red]✗[/red] {msg}")
    else:
        print(f"[err] {msg}", file=sys.stderr)


# ----------------------------- normalization -----------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _collect_emails(item: dict[str, Any]) -> list[str]:
    emails: list[str] = []
    for key in ("emails", "email"):
        v = item.get(key)
        if isinstance(v, list):
            emails.extend([e for e in v if isinstance(e, str)])
        elif isinstance(v, str) and v:
            emails.append(v)
    contacts = item.get("contacts") or {}
    if isinstance(contacts, dict):
        for k in ("emails", "email"):
            v = contacts.get(k)
            if isinstance(v, list):
                emails.extend([e for e in v if isinstance(e, str)])
            elif isinstance(v, str) and v:
                emails.append(v)
    additional = item.get("additionalInfo") or {}
    if isinstance(additional, dict):
        blob = json.dumps(additional, ensure_ascii=False)
        emails.extend(EMAIL_RE.findall(blob))

    # de-dupe, lowercase
    seen: set[str] = set()
    out: list[str] = []
    for e in emails:
        e2 = e.strip().lower()
        if e2 and e2 not in seen:
            seen.add(e2)
            out.append(e2)
    return out


def _collect_phones(item: dict[str, Any]) -> list[str]:
    phones: list[str] = []
    for key in ("phones", "phone", "phoneUnformatted"):
        v = item.get(key)
        if isinstance(v, list):
            phones.extend([p for p in v if isinstance(p, str)])
        elif isinstance(v, str) and v:
            phones.append(v)
    contacts = item.get("contacts") or {}
    if isinstance(contacts, dict):
        v = contacts.get("phones")
        if isinstance(v, list):
            phones.extend([p for p in v if isinstance(p, str)])
    seen: set[str] = set()
    out: list[str] = []
    for p in phones:
        p2 = p.strip()
        if p2 and p2 not in seen:
            seen.add(p2)
            out.append(p2)
    return out


def _real_website(item: dict[str, Any]) -> str | None:
    """`website` ise gerçek site; `url` Google Maps linkidir → website olarak kullanma."""
    w = item.get("website")
    if isinstance(w, str) and w.strip() and "google.com/maps" not in w:
        return w.strip()
    return None


def normalize(item: dict[str, Any]) -> dict[str, Any]:
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


# ----------------------------- scraping -----------------------------

def run_apify_scrape(
    query: str,
    location: str,
    language: str,
    max_results: int,
) -> list[dict[str, Any]]:
    try:
        from apify_client import ApifyClient
    except ImportError:
        err("apify-client kurulu değil. Kurulum: `python -m pip install -r requirements.txt`")
        sys.exit(2)

    # Hard-cap enforcement
    if max_results > MAX_LEADS_CAP:
        warn(f"--max ({max_results}) {MAX_LEADS_CAP}'a düşürüldü (hard cap).")
        max_results = MAX_LEADS_CAP
    max_results = max(1, max_results)

    api_key = get_api_key()
    client = ApifyClient(api_key)

    run_input: dict[str, Any] = {
        "searchStringsArray": [query],
        "locationQuery": location,
        "maxCrawledPlacesPerSearch": max_results,
        "language": language,
        "skipClosedPlaces": True,
        "scrapeContacts": True,
    }

    info(f"Apify actor başlatılıyor: [bold]{DEFAULT_ACTOR}[/bold]" if HAS_RICH else f"Actor: {DEFAULT_ACTOR}")
    info(f"Sorgu: [bold]{query}[/bold]  |  Konum: [bold]{location}[/bold]  |  Limit: [bold]{max_results}[/bold]")

    if HAS_RICH:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            transient=True,
        ) as progress:
            progress.add_task(description="Apify çalışıyor (Google Maps taranıyor)...", total=None)
            run = client.actor(DEFAULT_ACTOR).call(
                run_input=run_input,
                run_timeout=timedelta(minutes=10),
                wait_duration=timedelta(minutes=10),
            )
    else:
        run = client.actor(DEFAULT_ACTOR).call(
            run_input=run_input,
            run_timeout=timedelta(minutes=10),
            wait_duration=timedelta(minutes=10),
        )

    dataset_id = None
    if run is not None:
        if isinstance(run, dict):
            dataset_id = run.get("defaultDatasetId") or run.get("default_dataset_id")
        else:
            dataset_id = getattr(run, "default_dataset_id", None)

    if not dataset_id:
        err("Apify run dönmedi veya dataset id yok.")
        sys.exit(3)

    items: list[dict[str, Any]] = []
    for it in client.dataset(dataset_id).iterate_items():
        items.append(it)
        if len(items) >= max_results:
            break

    # Final safety clamp
    return items[:MAX_LEADS_CAP]


# ----------------------------- commands -----------------------------

def cmd_scrape(args: argparse.Namespace) -> None:
    banner()

    requested = min(args.max, MAX_LEADS_CAP)
    if args.max > MAX_LEADS_CAP:
        warn(f"İstenen {args.max} → {MAX_LEADS_CAP} olarak sınırlandı.")

    raw = run_apify_scrape(args.query, args.location, args.language, requested)
    leads = [normalize(x) for x in raw]
    leads = leads[:MAX_LEADS_CAP]  # paranoid clamp

    out_path = Path(args.output)
    payload = {
        "query": args.query,
        "location": args.location,
        "language": args.language,
        "max_limit": MAX_LEADS_CAP,
        "requested": requested,
        "count": len(leads),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "actor": DEFAULT_ACTOR,
        "leads": leads,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

    ok(f"{len(leads)} firma çekildi → [bold]{out_path}[/bold]")

    if not leads:
        warn("Sonuç boş. Sorguyu veya konumu daralt/değiştir.")
        return

    if HAS_RICH:
        tbl = Table(
            title=f"İlk {min(10, len(leads))} firma (ham veri)",
            show_lines=False,
            header_style="bold cyan",
        )
        tbl.add_column("#", style="dim", width=3, justify="right")
        tbl.add_column("Firma", style="bold")
        tbl.add_column("E-posta", style="cyan", overflow="fold")
        tbl.add_column("Telefon")
        tbl.add_column("Puan", justify="center")
        tbl.add_column("Yorum", justify="right")
        tbl.add_column("Website", style="blue", overflow="fold", max_width=32)
        for i, lead in enumerate(leads[:10], 1):
            emails = ", ".join(lead.get("emails", [])[:2]) or "—"
            phones = ", ".join(lead.get("phones", [])[:1]) or "—"
            rating = lead.get("rating")
            reviews = lead.get("reviews_count")
            tbl.add_row(
                str(i),
                (lead.get("name") or "—")[:40],
                emails,
                phones,
                f"{rating:.1f}" if isinstance(rating, (int, float)) else "—",
                str(reviews) if reviews else "—",
                lead.get("website") or "—",
            )
        console.print(tbl)
        console.print(
            Panel(
                f"Sıradaki adım: Claude bu veriyi analiz edip "
                f"[bold]leads_ranked.json[/bold] üretecek.",
                border_style="green",
                title="✓ Tarama tamam",
            )
        )


def cmd_display(args: argparse.Namespace) -> None:
    path = Path(args.file)
    if not path.exists():
        err(f"Dosya yok: {path}")
        sys.exit(1)
    data = json.loads(path.read_text())
    leads = data.get("leads") if isinstance(data, dict) else data
    if not isinstance(leads, list):
        err("Beklenen format: {leads: [...]} veya [...]")
        sys.exit(1)

    leads = leads[:MAX_LEADS_CAP]

    # Sort by toplam_skor desc if scored
    if leads and isinstance(leads[0], dict) and "toplam_skor" in leads[0]:
        leads.sort(key=lambda x: x.get("toplam_skor", 0), reverse=True)

    if not HAS_RICH:
        print(json.dumps(leads, ensure_ascii=False, indent=2))
        return

    banner()
    tbl = Table(
        title=f"Sıralı Potansiyel Müşteri Listesi  ·  {len(leads)} aday",
        show_lines=True,
        header_style="bold magenta",
    )
    tbl.add_column("#", style="dim", width=3, justify="right")
    tbl.add_column("Firma", style="bold", overflow="fold", max_width=28)
    tbl.add_column("Skor", justify="center", style="bold yellow")
    tbl.add_column("E-posta", style="cyan", overflow="fold")
    tbl.add_column("Telefon")
    tbl.add_column("Sektör", overflow="fold", max_width=18)
    tbl.add_column("Neden uygun?", style="green", overflow="fold")

    for i, lead in enumerate(leads, 1):
        score = lead.get("toplam_skor")
        score_str = f"{score:.1f}" if isinstance(score, (int, float)) else "—"
        emails = ", ".join(lead.get("emails", [])[:2]) or "—"
        phones = ", ".join(lead.get("phones", [])[:1]) or "—"
        tbl.add_row(
            str(i),
            (lead.get("name") or "—"),
            score_str,
            emails,
            phones,
            (lead.get("category") or "—"),
            (lead.get("not") or lead.get("note") or "—"),
        )
    console.print(tbl)


def cmd_export_csv(args: argparse.Namespace) -> None:
    path = Path(args.file)
    if not path.exists():
        err(f"Dosya yok: {path}")
        sys.exit(1)
    data = json.loads(path.read_text())
    leads = data.get("leads") if isinstance(data, dict) else data
    if not isinstance(leads, list):
        err("Beklenen format: {leads: [...]} veya [...]")
        sys.exit(1)
    leads = leads[:MAX_LEADS_CAP]

    out = Path(args.output)
    fieldnames = [
        "rank", "name", "toplam_skor", "sektor_uyumu", "erisilebilirlik", "sinyal",
        "emails", "phones", "website", "city", "category", "rating", "reviews_count",
        "address", "google_url", "not",
    ]
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for i, l in enumerate(leads, 1):
            writer.writerow({
                "rank": i,
                "name": l.get("name"),
                "toplam_skor": l.get("toplam_skor"),
                "sektor_uyumu": l.get("sektor_uyumu"),
                "erisilebilirlik": l.get("erisilebilirlik"),
                "sinyal": l.get("sinyal"),
                "emails": "; ".join(l.get("emails") or []),
                "phones": "; ".join(l.get("phones") or []),
                "website": l.get("website"),
                "city": l.get("city"),
                "category": l.get("category"),
                "rating": l.get("rating"),
                "reviews_count": l.get("reviews_count"),
                "address": l.get("address"),
                "google_url": l.get("google_url"),
                "not": l.get("not") or l.get("note"),
            })
    ok(f"CSV yazıldı: [bold]{out}[/bold] ({len(leads)} satır)")


def cmd_serve(args: argparse.Namespace) -> None:
    import http.server
    import socketserver
    import threading
    import webbrowser

    banner()
    port = args.port
    directory = str(ROOT)

    landing_exists = (ROOT / "index.html").exists()
    dash_exists = (ROOT / "dashboard.html").exists()
    if not (landing_exists or dash_exists):
        err("index.html ve dashboard.html bulunamadı.")
        sys.exit(1)
    if dash_exists and not (ROOT / "leads_ranked.json").exists():
        warn("leads_ranked.json yok — dashboard boş gözükecek. Önce tarama + analiz yap.")

    target_page = args.page
    if target_page == "auto":
        target_page = "index.html" if landing_exists else "dashboard.html"

    handler_cls = http.server.SimpleHTTPRequestHandler

    class Handler(handler_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)
        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            return

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        url = f"http://127.0.0.1:{port}/{target_page}"
        label = "Landing" if target_page == "index.html" else "Dashboard"
        ok(f"{label} çalışıyor: [bold link={url}]{url}[/bold link]" if HAS_RICH else f"{label}: {url}")
        if landing_exists and dash_exists:
            other = "dashboard.html" if target_page == "index.html" else "index.html"
            info(f"Diğer sayfa: http://127.0.0.1:{port}/{other}")
        info("Durdurmak için Ctrl+C")
        if not args.no_open:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print()
            ok("Sunucu durduruldu.")


def cmd_check(args: argparse.Namespace) -> None:
    banner()
    info("Sağlık kontrolü...")
    load_env()
    key = os.environ.get("APIFY_API_TOKEN", "")
    if key:
        ok(f"APIFY_API_TOKEN bulundu (***{key[-6:]})")
    else:
        err("APIFY_API_TOKEN yok.")
    try:
        import apify_client  # noqa
        ok(f"apify-client kurulu ({apify_client.__version__ if hasattr(apify_client, '__version__') else 'OK'})")
    except ImportError:
        err("apify-client kurulu DEĞİL. → pip install -r requirements.txt")
    if HAS_RICH:
        ok("rich kurulu")
    else:
        warn("rich kurulu değil (UI sade olur).")
    ok(f"Hard limit: {MAX_LEADS_CAP} firma / tarama")


# ----------------------------- main -----------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        prog="musteri_ajan",
        description="Müşteri Bulma Ajanı — Apify üzerinden B2B lead toplama CLI'ı.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scrape", help="Apify ile firma topla (max 50).")
    sc.add_argument("--query", required=True, help="Arama (örn. 'inşaat firması').")
    sc.add_argument("--location", required=True, help="Konum (örn. 'İstanbul, Türkiye').")
    sc.add_argument("--language", default="tr", help="Dil kodu (varsayılan: tr).")
    sc.add_argument(
        "--max", type=int, default=MAX_LEADS_CAP,
        help=f"Maksimum sonuç (HARD CAP {MAX_LEADS_CAP}).",
    )
    sc.add_argument("--output", default="leads_raw.json", help="Çıktı JSON yolu.")
    sc.set_defaults(func=cmd_scrape)

    ds = sub.add_parser("display", help="Sıralı listeyi terminalde göster.")
    ds.add_argument("file", help="leads_ranked.json yolu.")
    ds.set_defaults(func=cmd_display)

    ex = sub.add_parser("export-csv", help="Sıralı listeyi CSV'e çevir.")
    ex.add_argument("file", help="leads_ranked.json yolu.")
    ex.add_argument("--output", default="leads_ranked.csv")
    ex.set_defaults(func=cmd_export_csv)

    ck = sub.add_parser("check", help="Kurulumu doğrula.")
    ck.set_defaults(func=cmd_check)

    sv = sub.add_parser("serve", help="Landing + dashboard'u lokalde sun.")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-open", action="store_true", help="Tarayıcıyı otomatik açma.")
    sv.add_argument(
        "--page",
        choices=("auto", "index.html", "dashboard.html"),
        default="auto",
        help="Hangi sayfayı aç (auto: index varsa landing, yoksa dashboard).",
    )
    sv.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
