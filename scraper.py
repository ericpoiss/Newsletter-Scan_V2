#!/usr/bin/env python3
"""
Modularer Newsletter-/Themen-Scanner mit drei Quelltypen:

  - "web"       : Web-Uebersichtsseite mit Artikel-Links (z.B. WKO, LBG)
  - "pdf_index" : Seite, auf der neue PDF-Ausgaben zum Download stehen
                  (z.B. KSW Mitgliedermagazin) -- es wird die NEUE AUSGABE
                  erkannt, nicht der Volltext durchsucht.
  - "email"     : Newsletter, die nur per E-Mail-Anmeldung ankommen
                  (KSW Allgemein, BMF, TPA, KPMG). Wird per IMAP aus einem
                  dedizierten Postfach gelesen.

Ergebnis wie gehabt: report.md (nur neue Eintraege) + state.json (Merker).
"""

import email
import imaplib
import json
import os
import re
import sys
from email.header import decode_header
from pathlib import Path
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup

STATE_FILE = Path(__file__).parent / "state.json"
REPORT_FILE = Path(__file__).parent / "report.md"
PDF_EXTRACTS_DIR = Path(__file__).parent / "pdf_extracts"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 NewsletterScanner/1.0"
    )
}

# ---------------------------------------------------------------------------
# QUELLEN-KONFIGURATION -- hier Quellen hinzufuegen/aendern
# ---------------------------------------------------------------------------
SOURCES = [
    {"name": "WKO Steuernews", "type": "web",
     "url": "https://www.wko.at/steuern/news-zu-steuerthemen"},
    {"name": "LBG", "type": "web",
     "url": "https://www.lbg.at/servicecenter/lbg_steuertipps_praxis/index_ger.html"},
    {"name": "KSW Mitgliedermagazin", "type": "pdf_index",
     "url": "https://ksw.or.at/update/"},
    {"name": "KSW Allgemeiner Newsletter", "type": "email",
     "sender_contains": "akademie-sw"},
    {"name": "BMF", "type": "email", "sender_contains": "bmf.gv.at"},
    {"name": "TPA", "type": "email", "sender_contains": "tpa-group"},
    {"name": "KPMG Tax News", "type": "email", "sender_contains": "kpmg"},
]

BLOCKLIST_WORDS = {
    "kontakt", "impressum", "datenschutz", "cookie", "anmelden", "login",
    "newsletter abonnieren", "startseite", "home", "suche", "menu", "menü",
    "facebook", "twitter", "linkedin", "xing", "instagram", "youtube",
    "agb", "barrierefreiheit", "jobs", "karriere", "sitemap",
}
MIN_TITLE_LEN = 15
MAX_TITLE_LEN = 160

IMAP_HOST = "imap.gmail.com"
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")


# ---------------------------------------------------------------------------
# TYP: web
# ---------------------------------------------------------------------------
def fetch(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        print(f"  [FEHLER] Konnte {url} nicht abrufen: {e}", file=sys.stderr)
        return None


def _maybe_add(candidates: dict, title: str, href: str, base_url: str) -> None:
    if not title or not (MIN_TITLE_LEN <= len(title) <= MAX_TITLE_LEN):
        return
    if any(w in title.lower() for w in BLOCKLIST_WORDS):
        return
    if href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return
    candidates.setdefault(urljoin(base_url, href), title)


def scan_web(url: str) -> list[dict] | None:
    html = fetch(url)
    if html is None:
        return None  # Fehler beim Abruf -- NICHT mit "keine Artikel" verwechseln
    soup = BeautifulSoup(html, "html.parser")
    candidates: dict[str, str] = {}

    for heading in soup.find_all(["h2", "h3", "h4"]):
        title = heading.get_text(strip=True)
        link_tag = heading.find("a") or heading.find_next("a")
        if link_tag and link_tag.get("href"):
            _maybe_add(candidates, title, link_tag["href"], url)

    for a in soup.find_all("a", href=True):
        _maybe_add(candidates, a.get_text(strip=True), a["href"], url)

    return [{"titel": t, "url": u} for u, t in candidates.items()]


# ---------------------------------------------------------------------------
# TYP: pdf_index (neue Ausgaben erkennen + Volltext extrahieren)
# ---------------------------------------------------------------------------
MAX_PDF_MB = 25  # Sicherheitsgrenze, damit kein riesiges File alles blockiert


def slugify_filename(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9äöüÄÖÜß._-]+", "-", text).strip("-")
    return text[:80] or "dokument"


def download_pdf(url: str) -> bytes | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=60, stream=True)
        resp.raise_for_status()
        content_length = int(resp.headers.get("Content-Length", 0))
        if content_length and content_length > MAX_PDF_MB * 1024 * 1024:
            print(f"  [HINWEIS] PDF zu groß ({content_length / 1e6:.1f} MB), überspringe Volltext.")
            return None
        return resp.content
    except requests.RequestException as e:
        print(f"  [FEHLER] PDF-Download fehlgeschlagen ({url}): {e}", file=sys.stderr)
        return None


def extract_pdf_text(pdf_bytes: bytes, max_chars: int = 15000) -> str:
    """Extrahiert reinen Text aus dem PDF (bis zu max_chars Zeichen, um die
    spaetere Weitergabe an die Claude-API in vernuenftigem Rahmen zu halten)."""
    text_parts = []
    total_len = 0
    tmp_path = Path("/tmp/_scan_tmp.pdf")
    tmp_path.write_bytes(pdf_bytes)
    try:
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
                total_len += len(page_text)
                if total_len >= max_chars:
                    break
    except Exception as e:
        print(f"  [FEHLER] PDF-Textextraktion fehlgeschlagen: {e}", file=sys.stderr)
        return ""
    finally:
        tmp_path.unlink(missing_ok=True)
    return "\n".join(text_parts)[:max_chars]


def scan_pdf_index(url: str) -> list[dict] | None:
    html = fetch(url)
    if html is None:
        return None  # Fehler beim Abruf -- NICHT mit "keine PDFs" verwechseln
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf"):
            title = a.get_text(strip=True) or href.rsplit("/", 1)[-1]
            items.append({"titel": f"Neue Ausgabe: {title}", "url": urljoin(url, href)})
    return items


def process_new_pdf(item: dict) -> str | None:
    """Laedt eine neu erkannte PDF-Ausgabe herunter, extrahiert den Text und
    speichert ihn unter pdf_extracts/. Gibt den Dateipfad zurueck (oder None
    bei Fehler)."""
    print(f"  Lade PDF herunter: {item['url']}")
    pdf_bytes = download_pdf(item["url"])
    if pdf_bytes is None:
        return None

    text = extract_pdf_text(pdf_bytes)
    if not text.strip():
        print("  [HINWEIS] Kein Text extrahiert (evtl. gescannte Bild-PDF ohne OCR).")
        return None

    PDF_EXTRACTS_DIR.mkdir(exist_ok=True)
    filename = PDF_EXTRACTS_DIR / f"{slugify_filename(item['titel'])}.txt"
    filename.write_text(text, encoding="utf-8")
    print(f"  -> Volltext gespeichert: {filename.name} ({len(text)} Zeichen)")
    return str(filename.relative_to(Path(__file__).parent))


# ---------------------------------------------------------------------------
# TYP: email (per IMAP aus dediziertem Gmail-Postfach)
# ---------------------------------------------------------------------------
def _decode(value: str) -> str:
    parts = decode_header(value)
    return "".join(
        p.decode(enc or "utf-8", errors="ignore") if isinstance(p, bytes) else p
        for p, enc in parts
    )


def scan_email_sources(email_sources: list[dict], already_seen_ids: set) -> dict:
    """Liest alle E-Mails im Postfach EINMAL aus und ordnet sie den passenden
    Quellen anhand des Absenders zu. Gibt {quelle_name: [items]} zurueck."""
    results = {s["name"]: [] for s in email_sources}

    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        print("  [HINWEIS] GMAIL_ADDRESS/GMAIL_APP_PASSWORD nicht gesetzt "
              "-- E-Mail-Quellen werden übersprungen.")
        return results

    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select("INBOX")
    except Exception as e:
        print(f"  [FEHLER] IMAP-Login fehlgeschlagen: {e}", file=sys.stderr)
        return results

    status, msg_ids = imap.search(None, "ALL")
    if status != "OK":
        imap.logout()
        return results

    for msg_id in msg_ids[0].split():
        status, data = imap.fetch(msg_id, "(RFC822)")
        if status != "OK" or not data or not data[0]:
            continue
        msg = email.message_from_bytes(data[0][1])

        message_id = msg.get("Message-ID", "").strip()
        if not message_id or message_id in already_seen_ids:
            continue

        sender = _decode(msg.get("From", "")).lower()
        subject = _decode(msg.get("Subject", "")).strip()

        for source in email_sources:
            if source["sender_contains"].lower() in sender:
                results[source["name"]].append({
                    "titel": subject or "(kein Betreff)",
                    "url": message_id,  # dient nur als eindeutiger Schluessel
                })
                already_seen_ids.add(message_id)
                break

    imap.logout()
    return results


# ---------------------------------------------------------------------------
# STATE / REPORT
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    state = load_state()
    new_state: dict = {}
    report_lines = ["# Neue Themen aus den Quellen\n"]
    total_new = 0

    email_sources = [s for s in SOURCES if s["type"] == "email"]

    # E-Mail-Postfach nur EINMAL komplett durchgehen (fuer alle email-Quellen)
    already_seen_ids = set()
    for s in email_sources:
        already_seen_ids.update(state.get(s["name"], []))
    email_results = scan_email_sources(email_sources, already_seen_ids) if email_sources else {}

    for source in SOURCES:
        name, typ = source["name"], source["type"]
        print(f"Scanne: {name} ({typ})")

        if typ == "web":
            candidates = scan_web(source["url"])
        elif typ == "pdf_index":
            candidates = scan_pdf_index(source["url"])
        elif typ == "email":
            candidates = email_results.get(name, [])
        else:
            candidates = []

        if candidates is None:
            # Abruf ist fehlgeschlagen -- alten Stand unangetastet lassen,
            # sonst wuerden beim naechsten erfolgreichen Lauf alle alten
            # Artikel faelschlich als "neu" gemeldet.
            print("  [HINWEIS] Übersprungen wegen Abruf-Fehler, State bleibt unverändert.")
            new_state[name] = state.get(name, [])
            continue

        seen_keys = set(state.get(name, []))
        new_items = [c for c in candidates if c["url"] not in seen_keys]
        new_state[name] = [c["url"] for c in candidates] if typ != "email" else list(
            set(state.get(name, [])) | {c["url"] for c in candidates}
        )

        if new_items:
            report_lines.append(f"## {name}\n")
            for item in new_items:
                if typ == "email":
                    report_lines.append(f"- {item['titel']}")
                elif typ == "pdf_index":
                    extract_path = process_new_pdf(item)
                    if extract_path:
                        report_lines.append(
                            f"- [{item['titel']}]({item['url']}) "
                            f"— Volltext: `{extract_path}`"
                        )
                    else:
                        report_lines.append(
                            f"- [{item['titel']}]({item['url']}) "
                            f"— (Volltext-Extraktion nicht möglich)"
                        )
                else:
                    report_lines.append(f"- [{item['titel']}]({item['url']})")
                total_new += 1
            report_lines.append("")
        else:
            print("  keine neuen Einträge")

    if total_new == 0:
        report_lines.append("_Keine neuen Themen seit dem letzten Scan._\n")

    REPORT_FILE.write_text("\n".join(report_lines), encoding="utf-8")
    save_state(new_state)
    print(f"\nFertig. {total_new} neue Themen gefunden -> {REPORT_FILE}")


if __name__ == "__main__":
    main()
