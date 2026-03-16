#!/usr/bin/env python3
"""
Vérificateur Souverain — Scraper de fact-checks v2.0
=====================================================
Modes :
  HISTORIQUE  → Scrape les archives (jusqu'à 5 ans) — à lancer UNE SEULE FOIS.
  QUOTIDIEN   → Incrémental : ne récupère que les articles plus récents que le dernier connu.

Variable d'environnement : SCRAPE_MODE (défaut = QUOTIDIEN)

Usage :
  SCRAPE_MODE=HISTORIQUE python scraper.py
  python scraper.py  # mode QUOTIDIEN par défaut
"""

import json
import os
import random
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import feedparser
import html2text
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

MODE = os.environ.get("SCRAPE_MODE", "QUOTIDIEN").upper()
DATA_DIR = "data"
OUTPUT_FILE = os.path.join(DATA_DIR, "factchecks.json")
USER_AGENT = (
    "VerificateurSouverain/2.0 "
    "(fact-check-aggregator; +https://github.com/verificateur-souverain)"
)
HEADERS = {"User-Agent": USER_AGENT}

# Nombre max de pages RSS à parcourir en mode HISTORIQUE
MAX_PAGES_HISTORIQUE = 50
# Nombre max d'articles par source en mode QUOTIDIEN
MAX_ARTICLES_QUOTIDIEN = 30

# ═══════════════════════════════════════════════════════════════════════════════
# SOURCES DE FACT-CHECKING
# ═══════════════════════════════════════════════════════════════════════════════
# Chaque source définit :
#   name          : Nom affiché
#   rss_url       : URL du flux RSS
#   lang          : Langue principale
#   rss_paged     : Template d'URL paginée (pour le mode HISTORIQUE)
#   selectors     : Sélecteurs CSS pour l'extraction enrichie sur la page article
#     verdict     : Sélecteur du verdict / rating
#     conclusion  : Sélecteur du paragraphe de conclusion / synthèse
#     claim       : Sélecteur de l'affirmation vérifiée (si différent du titre)

SOURCES = [
    {
        "name": "AFP Fact Check",
        "rss_url": "https://factcheck.afp.com/feed/rss",
        "lang": "fr",
        "rss_paged": "https://factcheck.afp.com/feed/rss?page={page}",
        "selectors": {
            "verdict": ".article-entry .card-verdict, .fact-check-card .verdict",
            "conclusion": ".article-entry > p:last-of-type, .article-body > p:last-of-type",
            "claim": ".article-entry .card-claim, h1",
        },
    },
    {
        "name": "Les Décodeurs — Le Monde",
        "rss_url": "https://www.lemonde.fr/les-decodeurs/rss_full.xml",
        "lang": "fr",
        "rss_paged": None,
        "selectors": {
            "verdict": ".article__heading--kicker, .article__status",
            "conclusion": ".article__paragraph:last-of-type, .article__content > p:last-of-type",
            "claim": ".article__title, h1",
        },
    },
    {
        "name": "Libération CheckNews",
        "rss_url": "https://www.liberation.fr/feed/rss/checknews/",
        "lang": "fr",
        "rss_paged": None,
        "selectors": {
            "verdict": ".tag-label, .article-header-crowdsourcing-tag",
            "conclusion": ".article-body-text > p:last-of-type",
            "claim": "h1.article-header-crowdsourcing-title, h1",
        },
    },
    {
        "name": "PolitiFact",
        "rss_url": "https://www.politifact.com/rss/factchecks/",
        "lang": "en",
        "rss_paged": "https://www.politifact.com/rss/factchecks/?page={page}",
        "selectors": {
            "verdict": ".m-statement__meter img[alt], .c-image__original img[alt]",
            "conclusion": ".m-textblock:last-of-type p, .short-on-time li:last-of-type",
            "claim": ".m-statement__quote, h2.c-title",
        },
    },
    {
        "name": "Full Fact",
        "rss_url": "https://fullfact.org/feed/",
        "lang": "en",
        "rss_paged": "https://fullfact.org/feed/?page={page}",
        "selectors": {
            "verdict": ".card-verdict, .verdict-label, [class*='verdict']",
            "conclusion": ".article-text > p:last-of-type, .content-body > p:last-of-type",
            "claim": "h1, .claim-text",
        },
    },
    {
        "name": "Reuters Fact Check",
        "rss_url": "https://www.reuters.com/arc/outboundfeeds/v3/all/fact-check/?outputType=xml",
        "lang": "en",
        "rss_paged": None,
        "selectors": {
            "verdict": "[class*='verdict'], [class*='fact-check'] span",
            "conclusion": "article p:last-of-type, [class*='article-body'] p:last-of-type",
            "claim": "h1",
        },
    },
    {
        "name": "Africa Check",
        "rss_url": "https://africacheck.org/feed/",
        "lang": "en",
        "rss_paged": "https://africacheck.org/feed/?paged={page}",
        "selectors": {
            "verdict": ".verdict-stamp, .report-verdict, [class*='verdict']",
            "conclusion": ".article-content > p:last-of-type, .entry-content > p:last-of-type",
            "claim": "h1, .report-claim",
        },
    },
    {
        "name": "Correctiv",
        "rss_url": "https://correctiv.org/feed/",
        "lang": "de",
        "rss_paged": None,
        "selectors": {
            "verdict": "[class*='bewertung'], [class*='verdict'], [class*='rating']",
            "conclusion": "article p:last-of-type",
            "claim": "h1",
        },
    },
    {
        "name": "Maldita.es",
        "rss_url": "https://maldita.es/feed/",
        "lang": "es",
        "rss_paged": None,
        "selectors": {
            "verdict": "[class*='veredicto'], [class*='verdict'], .claim-review-rating",
            "conclusion": ".article-body > p:last-of-type, .entry-content > p:last-of-type",
            "claim": "h1",
        },
    },
    {
        "name": "AP Fact Check",
        "rss_url": "https://apnews.com/feed/APFactCheck",
        "lang": "en",
        "rss_paged": None,
        "selectors": {
            "verdict": ".Claim span, [class*='claim'] [class*='verdict']",
            "conclusion": "article .RichTextStoryBody p:last-of-type",
            "claim": "h1",
        },
    },
]

# ═══════════════════════════════════════════════════════════════════════════════
# MAPPING DE VERDICTS → CATÉGORIES NORMALISÉES
# ═══════════════════════════════════════════════════════════════════════════════

VERDICT_MAP = {
    # Français
    "faux": "FAUX",
    "fake": "FAUX",
    "infondé": "FAUX",
    "incorrect": "FAUX",
    "non": "FAUX",
    "vrai": "VRAI",
    "confirmé": "VRAI",
    "exact": "VRAI",
    "oui": "VRAI",
    "nuancé": "NUANCÉ",
    "partiellement vrai": "NUANCÉ",
    "c'est plus compliqué": "NUANCÉ",
    "trompeur": "TROMPEUR",
    "hors contexte": "TROMPEUR",
    "manipulé": "TROMPEUR",
    "détourné": "TROMPEUR",
    "satire": "SATIRE",
    # English
    "false": "FAUX",
    "wrong": "FAUX",
    "true": "VRAI",
    "correct": "VRAI",
    "accurate": "VRAI",
    "mostly true": "NUANCÉ",
    "half true": "NUANCÉ",
    "half-true": "NUANCÉ",
    "mixture": "NUANCÉ",
    "partly false": "TROMPEUR",
    "mostly false": "TROMPEUR",
    "misleading": "TROMPEUR",
    "out of context": "TROMPEUR",
    "pants on fire": "FAUX",
    # Deutsch
    "falsch": "FAUX",
    "richtig": "VRAI",
    "irreführend": "TROMPEUR",
    "teilweise falsch": "TROMPEUR",
    # Español
    "falso": "FAUX",
    "verdadero": "VRAI",
    "engañoso": "TROMPEUR",
}

# ═══════════════════════════════════════════════════════════════════════════════
# UTILITAIRES
# ═══════════════════════════════════════════════════════════════════════════════

_h2t = html2text.HTML2Text()
_h2t.ignore_links = True
_h2t.ignore_images = True
_h2t.body_width = 0


def clean_html(raw: str) -> str:
    """Convertit du HTML en texte brut propre."""
    if not raw:
        return ""
    text = _h2t.handle(raw).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def normalize_verdict(text: str) -> str:
    """Normalise un texte de verdict vers une catégorie standard."""
    if not text:
        return "INCONNU"
    lower = text.lower().strip()
    for key, category in VERDICT_MAP.items():
        if key in lower:
            return category
    return "INCONNU"


def polite_sleep():
    """Pause aléatoire entre 1 et 3 secondes — respect des serveurs."""
    time.sleep(random.uniform(1.0, 3.0))


def safe_get(url: str, timeout: int = 30) -> Optional[requests.Response]:
    """Requête HTTP GET avec gestion d'erreur."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp
    except requests.RequestException as e:
        print(f"    ⚠ Erreur HTTP pour {url[:80]}… : {e}")
        return None


def parse_date_str(date_str: str) -> Optional[str]:
    """Parse une date flexible → format YYYY-MM-DD."""
    if not date_str:
        return None
    try:
        dt = dateparser.parse(date_str, fuzzy=True)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return None


def parse_feed_date(entry) -> str:
    """Extrait la date d'un entry feedparser → YYYY-MM-DD."""
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
            result = parse_date_str(val)
            if result:
                return result
    return datetime.now().strftime("%Y-%m-%d")


def extract_rss_content(entry) -> str:
    """Extrait le contenu brut d'un entry RSS."""
    if hasattr(entry, "content") and entry.content:
        return entry.content[0].get("value", "")
    if hasattr(entry, "summary"):
        return entry.summary or ""
    if hasattr(entry, "description"):
        return entry.description or ""
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# EXTRACTION ENRICHIE (scraping de la page article)
# ═══════════════════════════════════════════════════════════════════════════════


def scrape_article_page(url: str, selectors: dict) -> dict:
    """
    Récupère la page complète d'un article et extrait :
      - verdict_label  (texte du verdict)
      - conclusion     (paragraphe de synthèse)
      - claim          (l'affirmation vérifiée)

    Retourne un dict avec ces 3 clés (valeurs vides si échec).
    """
    result = {"verdict_label": "", "conclusion": "", "claim": ""}

    resp = safe_get(url)
    if not resp:
        return result

    try:
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception:
        return result

    # --- Verdict ---
    verdict_sel = selectors.get("verdict", "")
    if verdict_sel:
        for sel in verdict_sel.split(","):
            sel = sel.strip()
            if not sel:
                continue
            el = soup.select_one(sel)
            if el:
                # Pour les images (PolitiFact), le verdict est dans l'attribut alt
                text = el.get("alt", "") if el.name == "img" else el.get_text(strip=True)
                if text and len(text) < 100:
                    result["verdict_label"] = text
                    break

    # --- Conclusion ---
    conclusion_sel = selectors.get("conclusion", "")
    if conclusion_sel:
        for sel in conclusion_sel.split(","):
            sel = sel.strip()
            if not sel:
                continue
            el = soup.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if text and len(text) > 30:
                    result["conclusion"] = text[:600]
                    break

    # Fallback conclusion : derniers paragraphes de l'article
    if not result["conclusion"]:
        paragraphs = soup.select("article p, .article-body p, .entry-content p, main p")
        # Prendre le dernier paragraphe substantiel
        for p in reversed(paragraphs):
            text = p.get_text(strip=True)
            if len(text) > 50:
                result["conclusion"] = text[:600]
                break

    # --- Claim ---
    claim_sel = selectors.get("claim", "")
    if claim_sel:
        for sel in claim_sel.split(","):
            sel = sel.strip()
            if not sel:
                continue
            el = soup.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                if text and len(text) > 10:
                    result["claim"] = text[:300]
                    break

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# RÉCUPÉRATION DES ARTICLES D'UNE SOURCE
# ═══════════════════════════════════════════════════════════════════════════════


def fetch_rss_entries(source: dict, cutoff_date: Optional[str]) -> list:
    """
    Récupère les entrées RSS d'une source.
    En mode HISTORIQUE, pagine si possible.
    En mode QUOTIDIEN, s'arrête dès qu'un article est plus ancien que cutoff_date.
    """
    all_entries = []
    rss_url = source["rss_url"]
    paged_template = source.get("rss_paged")

    if MODE == "HISTORIQUE" and paged_template:
        # Parcourir plusieurs pages RSS
        for page_num in range(1, MAX_PAGES_HISTORIQUE + 1):
            url = paged_template.format(page=page_num)
            print(f"    📄 Page {page_num}…", end=" ")

            resp = safe_get(url)
            if not resp:
                print("✗")
                break

            feed = feedparser.parse(resp.text)
            if not feed.entries:
                print("(vide, fin)")
                break

            all_entries.extend(feed.entries)
            print(f"✓ {len(feed.entries)} articles")

            # Vérifier la date la plus ancienne de la page
            oldest = parse_feed_date(feed.entries[-1])
            five_years_ago = (datetime.now() - timedelta(days=5 * 365)).strftime("%Y-%m-%d")
            if oldest < five_years_ago:
                print(f"    ⏹ Limite de 5 ans atteinte ({oldest})")
                break

            polite_sleep()
    else:
        # Mode simple : un seul flux
        resp = safe_get(rss_url)
        if not resp:
            return []

        feed = feedparser.parse(resp.text)
        entries = feed.entries[:MAX_ARTICLES_QUOTIDIEN] if MODE == "QUOTIDIEN" else feed.entries
        all_entries.extend(entries)

    # Filtrage par date en mode QUOTIDIEN
    if cutoff_date and MODE == "QUOTIDIEN":
        filtered = []
        for entry in all_entries:
            entry_date = parse_feed_date(entry)
            if entry_date > cutoff_date:
                filtered.append(entry)
            else:
                break  # Les RSS sont triés par date décroissante
        return filtered

    return all_entries


def process_source(source: dict, cutoff_date: Optional[str]) -> list[dict]:
    """
    Traite une source complète :
    1. Récupère le flux RSS
    2. Pour chaque article, scrape la page pour enrichir les données
    3. Retourne une liste de fact-checks normalisés
    """
    name = source["name"]
    print(f"\n  ═══ {name} ═══")

    entries = fetch_rss_entries(source, cutoff_date)
    if not entries:
        print(f"    (aucun article)")
        return []

    print(f"    {len(entries)} articles à traiter")
    items = []

    for i, entry in enumerate(entries):
        title = clean_html(getattr(entry, "title", ""))
        link = getattr(entry, "link", "") or ""
        if not title or not link:
            continue

        date_published = parse_feed_date(entry)

        # Contenu RSS (fallback)
        rss_content = clean_html(extract_rss_content(entry))
        rss_summary = " ".join(rss_content.split()[:80]) if rss_content else ""

        # Extraction enrichie depuis la page article
        enriched = {"verdict_label": "", "conclusion": "", "claim": ""}
        if link:
            try:
                enriched = scrape_article_page(link, source.get("selectors", {}))
                polite_sleep()
            except Exception as e:
                print(f"    ⚠ Erreur enrichissement {link[:60]}… : {e}")

        # Construire le fact-check final
        claim = enriched["claim"] or title
        verdict_text = enriched["verdict_label"]
        verdict_category = normalize_verdict(verdict_text) if verdict_text else "INCONNU"
        conclusion = enriched["conclusion"] or rss_summary

        items.append({
            "title": title,
            "url": link,
            "date": date_published,
            "source_name": name,
            "lang": source["lang"],
            "claim": claim,
            "verdict_label": verdict_text or "Non déterminé",
            "verdict_category": verdict_category,
            "conclusion": conclusion,
        })

        # Log de progression
        if (i + 1) % 10 == 0:
            print(f"    ✓ {i + 1}/{len(entries)} traités")

    print(f"    ✅ {len(items)} articles extraits pour {name}")
    return items


# ═══════════════════════════════════════════════════════════════════════════════
# FUSION ET DÉDUPLICATION
# ═══════════════════════════════════════════════════════════════════════════════


def load_existing_data() -> dict:
    """Charge le fichier factchecks.json existant."""
    if not os.path.exists(OUTPUT_FILE):
        return {"last_updated": "", "total_count": 0, "sources": [], "factchecks": []}

    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # S'assurer que la structure est valide
            if "factchecks" not in data:
                data["factchecks"] = []
            return data
    except (json.JSONDecodeError, IOError):
        return {"last_updated": "", "total_count": 0, "sources": [], "factchecks": []}


def get_cutoff_date(existing_data: dict) -> Optional[str]:
    """
    Retourne la date du dernier article connu (YYYY-MM-DD).
    Utilisé en mode QUOTIDIEN pour ne scraper que les nouveaux articles.
    """
    factchecks = existing_data.get("factchecks", [])
    if not factchecks:
        return None

    dates = [fc.get("date", "1970-01-01") for fc in factchecks]
    dates.sort(reverse=True)
    return dates[0] if dates else None


def merge_and_deduplicate(existing: list[dict], new_items: list[dict]) -> list[dict]:
    """
    Fusionne les anciens et les nouveaux articles.
    Déduplique par URL (les nouveaux écrasent les anciens si même URL).
    """
    by_url = {}

    # D'abord les anciens
    for item in existing:
        url = item.get("url", "")
        if url:
            by_url[url] = item
        else:
            # Garder les articles sans URL (peu probable mais sécurité)
            by_url[f"__no_url_{id(item)}"] = item

    # Puis les nouveaux (écrasent les anciens pour mise à jour)
    for item in new_items:
        url = item.get("url", "")
        if url:
            by_url[url] = item

    # Trier par date décroissante
    result = list(by_url.values())
    result.sort(key=lambda x: x.get("date", "1970-01-01"), reverse=True)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# POINT D'ENTRÉE
# ═══════════════════════════════════════════════════════════════════════════════


def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║  Vérificateur Souverain — Scraper v2.0                  ║")
    print(f"║  Mode : {MODE:<48}║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    # Charger les données existantes
    existing_data = load_existing_data()
    existing_factchecks = existing_data.get("factchecks", [])
    print(f"📂 Base existante : {len(existing_factchecks)} articles\n")

    # Déterminer la date de coupure (mode QUOTIDIEN uniquement)
    cutoff_date = None
    if MODE == "QUOTIDIEN":
        cutoff_date = get_cutoff_date(existing_data)
        if cutoff_date:
            print(f"📅 Mode incrémental : articles après le {cutoff_date}\n")
        else:
            print("📅 Aucune base existante → scraping complet\n")

    # Scraping de chaque source
    all_new_items = []
    for source in SOURCES:
        try:
            items = process_source(source, cutoff_date)
            all_new_items.extend(items)
        except Exception as e:
            print(f"  ✗ Erreur fatale pour {source['name']} : {e}")
            continue

    print(f"\n{'─' * 50}")
    print(f"📊 {len(all_new_items)} nouveaux articles récupérés")

    # Fusion avec la base existante
    if MODE == "HISTORIQUE":
        # En mode historique, on repart de zéro
        merged = merge_and_deduplicate([], all_new_items)
    else:
        merged = merge_and_deduplicate(existing_factchecks, all_new_items)

    print(f"📊 {len(merged)} articles après fusion et déduplication")

    # Export
    os.makedirs(DATA_DIR, exist_ok=True)
    output = {
        "last_updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_count": len(merged),
        "sources": [s["name"] for s in SOURCES],
        "factchecks": merged,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Base sauvegardée → {OUTPUT_FILE}")
    print(f"   {output['total_count']} fact-checks")
    print(f"   Dernière mise à jour : {output['last_updated']}")


if __name__ == "__main__":
    main()
