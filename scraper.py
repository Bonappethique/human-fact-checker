#!/usr/bin/env python3
"""
Vérificateur Souverain — Scraper de fact-checks
Récupère et normalise les données de 10 sources fiables.
"""

import json
import os
import re
from datetime import datetime

import feedparser
import html2text
import requests

# ─── Configuration des sources ───────────────────────────────────────────────

SOURCES = [
    {
        "name": "AFP Fact Check",
        "url": "https://factcheck.afp.com/feed/rss",
        "lang": "fr",
    },
    {
        "name": "Les Décodeurs – Le Monde",
        "url": "https://www.lemonde.fr/les-decodeurs/rss_full.xml",
        "lang": "fr",
    },
    {
        "name": "Libération Désintox",
        "url": "https://www.liberation.fr/feed/rss/checknews/",
        "lang": "fr",
    },
    {
        "name": "Reuters Fact Check",
        "url": "https://www.reuters.com/arc/outboundfeeds/v3/all/fact-check/?outputType=xml",
        "lang": "en",
    },
    {
        "name": "AP Fact Check",
        "url": "https://apnews.com/feed/APFactCheck",
        "lang": "en",
    },
    {
        "name": "PolitiFact",
        "url": "https://www.politifact.com/rss/factchecks/",
        "lang": "en",
    },
    {
        "name": "Full Fact",
        "url": "https://fullfact.org/feed/",
        "lang": "en",
    },
    {
        "name": "Correctiv",
        "url": "https://correctiv.org/feed/",
        "lang": "de",
    },
    {
        "name": "Maldita.es",
        "url": "https://maldita.es/feed/",
        "lang": "es",
    },
    {
        "name": "Africa Check",
        "url": "https://africacheck.org/feed/",
        "lang": "en",
    },
]

# ─── Mappings de verdict ─────────────────────────────────────────────────────

VERDICT_MAP = {
    # Français
    "faux": "FAUX",
    "fake": "FAUX",
    "false": "FAUX",
    "falso": "FAUX",
    "falsch": "FAUX",
    "incorrect": "FAUX",
    "wrong": "FAUX",
    "vrai": "VRAI",
    "true": "VRAI",
    "correct": "VRAI",
    "verdadero": "VRAI",
    "richtig": "VRAI",
    "mostly true": "NUANCÉ",
    "half true": "NUANCÉ",
    "half-true": "NUANCÉ",
    "mixture": "NUANCÉ",
    "nuancé": "NUANCÉ",
    "partly false": "TROMPEUR",
    "mostly false": "TROMPEUR",
    "misleading": "TROMPEUR",
    "trompeur": "TROMPEUR",
    "engañoso": "TROMPEUR",
    "irreführend": "TROMPEUR",
    "out of context": "TROMPEUR",
    "hors contexte": "TROMPEUR",
    "satire": "SATIRE",
    "pants on fire": "FAUX",
}

h2t = html2text.HTML2Text()
h2t.ignore_links = True
h2t.ignore_images = True
h2t.body_width = 0


def clean_html(raw: str) -> str:
    """Convertit le HTML en texte brut propre."""
    if not raw:
        return ""
    text = h2t.handle(raw).strip()
    # Nettoyer les sauts de ligne multiples
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def normalize_verdict(text: str) -> str:
    """Normalise un verdict en catégorie standard."""
    if not text:
        return "NUANCÉ"
    lower = text.lower().strip()
    for key, category in VERDICT_MAP.items():
        if key in lower:
            return category
    return "NUANCÉ"


def extract_verdict_from_title(title: str) -> tuple[str, str]:
    """Tente d'extraire un verdict depuis le titre de l'article."""
    patterns = [
        r"(?:verdict|rating|ruling)\s*:\s*(.+)",
        r"—\s*(.+)$",
        r"\|\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            verdict_text = match.group(1).strip()
            return verdict_text, normalize_verdict(verdict_text)
    return "", "NUANCÉ"


def make_summary(content: str, max_sentences: int = 3) -> str:
    """Extrait les premières phrases comme résumé."""
    if not content:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", content)
    summary = " ".join(sentences[:max_sentences])
    if len(summary) > 500:
        summary = summary[:497] + "…"
    return summary


def parse_date(entry) -> str:
    """Extrait et formate la date de publication."""
    for field in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, field, None) or entry.get(field)
        if parsed:
            try:
                return datetime(*parsed[:6]).strftime("%Y-%m-%d")
            except Exception:
                pass
    for field in ("published", "updated"):
        val = getattr(entry, field, None) or entry.get(field)
        if val:
            return val[:10]
    return datetime.now().strftime("%Y-%m-%d")


def fetch_source(source: dict) -> list[dict]:
    """Récupère et normalise les articles d'une source RSS."""
    items = []
    print(f"  ⟶ {source['name']}…", end=" ")

    try:
        resp = requests.get(
            source["url"],
            timeout=30,
            headers={
                "User-Agent": "VerificateurSouverain/1.0 (fact-check-aggregator; +https://github.com/verificateur-souverain)"
            },
        )
        resp.raise_for_status()
        resp.encoding = "utf-8"
        feed = feedparser.parse(resp.text)
    except Exception as e:
        print(f"✗ Erreur: {e}")
        return []

    for entry in feed.entries[:50]:  # Max 50 articles par source
        title = clean_html(getattr(entry, "title", ""))
        if not title:
            continue

        # Contenu
        content_raw = ""
        if hasattr(entry, "content") and entry.content:
            content_raw = entry.content[0].get("value", "")
        elif hasattr(entry, "summary"):
            content_raw = entry.summary or ""
        elif hasattr(entry, "description"):
            content_raw = entry.description or ""

        content = clean_html(content_raw)

        # Verdict
        verdict_text, verdict_category = extract_verdict_from_title(title)
        if not verdict_text:
            # Chercher dans le contenu
            for keyword, cat in VERDICT_MAP.items():
                if keyword in content.lower()[:200]:
                    verdict_text = keyword.capitalize()
                    verdict_category = cat
                    break

        link = getattr(entry, "link", "") or ""

        items.append(
            {
                "title": title,
                "claim": title,  # Le titre est souvent l'affirmation vérifiée
                "verdict_text": verdict_text or "À vérifier",
                "verdict_category": verdict_category,
                "explanation": make_summary(content),
                "source_name": source["name"],
                "source_url": link,
                "date_published": parse_date(entry),
                "lang": source["lang"],
            }
        )

    print(f"✓ {len(items)} articles")
    return items


def main():
    print("╔══════════════════════════════════════════════╗")
    print("║  Vérificateur Souverain — Collecte de données ║")
    print("╚══════════════════════════════════════════════╝\n")

    all_items = []
    for source in SOURCES:
        all_items.extend(fetch_source(source))

    # Déduplication par URL
    seen_urls = set()
    unique_items = []
    for item in all_items:
        url = item["source_url"]
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_items.append(item)
        elif not url:
            unique_items.append(item)

    # Tri par date décroissante
    unique_items.sort(key=lambda x: x["date_published"], reverse=True)

    # Export
    os.makedirs("data", exist_ok=True)
    output = {
        "last_updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_count": len(unique_items),
        "sources": [s["name"] for s in SOURCES],
        "factchecks": unique_items,
    }

    with open("data/factchecks.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ {len(unique_items)} fact-checks sauvegardés dans data/factchecks.json")
    print(f"   Dernière mise à jour : {output['last_updated']}")


if __name__ == "__main__":
    main()
