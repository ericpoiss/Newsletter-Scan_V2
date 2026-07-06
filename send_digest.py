#!/usr/bin/env python3
"""
Baut aus report.md einen fertigen, copy-paste-tauglichen Prompt fuer den
normalen Claude-Chat (kein API-Key noetig) und verschickt ihn per E-Mail.

Nutzt fuer den E-Mail-Versand (SMTP) dieselben Gmail-Zugangsdaten, die
bereits fuer das Auslesen der Newsletter-Postfaecher (IMAP) angelegt wurden.

Benoetigte Umgebungsvariablen:
  GMAIL_ADDRESS       -- Absender (dasselbe Konto wie beim Newsletter-Scan)
  GMAIL_APP_PASSWORD  -- App-Passwort desselben Kontos
  RECIPIENT_EMAIL     -- wohin der fertige Prompt geschickt werden soll
                         (optional; Standard: an GMAIL_ADDRESS selbst)
"""

import os
import re
import smtplib
import sys
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

REPORT_FILE = Path(__file__).parent / "report.md"

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL") or GMAIL_ADDRESS

ANZAHL_ARTIKEL = 5
WORTANZAHL = "600-800"
TONALITAET = "sachlich, klar, für Unternehmer:innen und Selbstständige verständlich"

MAX_VOLLTEXT_CHARS = 6000  # pro PDF-Auszug, damit die Mail nicht ausufert


def parse_report(text: str) -> list[dict]:
    items = []
    current_source = "Unbekannte Quelle"
    for line in text.splitlines():
        heading_match = re.match(r"^##\s+(.*)", line)
        link_item_match = re.match(
            r"^-\s+\[(.+?)\]\((.+?)\)(?:\s+—\s+Volltext:\s+`(.+?)`)?", line
        )
        plain_item_match = re.match(r"^-\s+(.+)", line)
        if heading_match:
            current_source = heading_match.group(1).strip()
        elif link_item_match:
            extract_path = link_item_match.group(3)
            volltext = ""
            if extract_path:
                full_path = Path(__file__).parent / extract_path
                if full_path.exists():
                    volltext = full_path.read_text(encoding="utf-8")[:MAX_VOLLTEXT_CHARS]
            items.append(
                {
                    "quelle": current_source,
                    "titel": link_item_match.group(1).strip(),
                    "url": link_item_match.group(2).strip(),
                    "volltext": volltext,
                }
            )
        elif plain_item_match:
            items.append(
                {
                    "quelle": current_source,
                    "titel": plain_item_match.group(1).strip(),
                    "url": "(kein Link, E-Mail-Newsletter)",
                    "volltext": "",
                }
            )
    return items


def build_prompt(items: list[dict]) -> str:
    liste = "\n".join(f"- [{i['quelle']}] {i['titel']} ({i['url']})" for i in items)

    volltext_bloecke = [
        f"### Volltext-Auszug zu \"{i['titel']}\" ({i['quelle']})\n{i['volltext']}\n"
        for i in items
        if i.get("volltext")
    ]
    volltext_hinweis = (
        "\n\nZusätzlich liegen dir zu manchen Ausgaben vollständige "
        "Text-Auszüge vor -- nutze diese aktiv, um konkrete, inhaltlich "
        "passende Artikel-Themen daraus abzuleiten (nicht nur den "
        "Ausgaben-Titel):\n\n" + "\n\n".join(volltext_bloecke)
        if volltext_bloecke
        else ""
    )

    return f"""Hier ist eine Liste aktueller Themen aus österreichischen Steuer-/
WT-Newslettern (Titel + Quelle + Link):

{liste}{volltext_hinweis}

Aufgabe:
1. Wähle daraus die {ANZAHL_ARTIKEL} Themen aus, die für einen Steuer-/
   Wirtschaftsberatungs-Blog am spannendsten bzw. für die Leser:innen
   (Unternehmer:innen, Selbstständige) am relevantesten sind. Falls dir zu
   einer Ausgabe ein Volltext-Auszug vorliegt, wähle daraus gezielt
   einzelne, konkrete Artikel-Themen statt nur "die ganze Ausgabe".
2. Recherchiere zu jedem gewählten Thema zusätzlich aktuell im Web
   (mehrere Quellen, nicht nur die Ursprungs-URL bzw. den Volltext-Auszug),
   um es fachlich korrekt einzuordnen und zu ergänzen.
3. Schreibe zu jedem der {ANZAHL_ARTIKEL} Themen einen eigenständigen
   Blogartikel ({WORTANZAHL} Wörter, Ton: {TONALITAET}), mit knackiger
   Überschrift, kurzer Einleitung, 3-4 Zwischenüberschriften, Fazit und
   einer Liste der verwendeten Quellen am Ende.

Bitte gib mir alle {ANZAHL_ARTIKEL} Artikel direkt untereinander aus,
jeweils klar mit "---" getrennt.
"""


def send_email(subject: str, body: str) -> None:
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD or not RECIPIENT_EMAIL:
        print("[FEHLER] GMAIL_ADDRESS / GMAIL_APP_PASSWORD / RECIPIENT_EMAIL "
              "nicht vollständig gesetzt -- kann keine Mail senden.", file=sys.stderr)
        sys.exit(1)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)

    print(f"Mail gesendet an {RECIPIENT_EMAIL}.")


def main() -> None:
    if not REPORT_FILE.exists():
        print("Keine report.md gefunden -- zuerst scraper.py laufen lassen.")
        sys.exit(0)

    items = parse_report(REPORT_FILE.read_text(encoding="utf-8"))
    if not items:
        print("Keine neuen Themen -- keine Mail nötig.")
        return

    prompt = build_prompt(items)
    subject = f"Newsletter-Scan: {len(items)} neue Themen -- fertiger Claude-Prompt"
    body = (
        "Unten findest du den fertigen Prompt. Einfach alles ab der naechsten "
        "Zeile kopieren und in einen normalen Claude-Chat einfuegen:\n\n"
        "==================================================\n\n"
        f"{prompt}\n"
        "==================================================\n"
    )

    send_email(subject, body)


if __name__ == "__main__":
    main()
