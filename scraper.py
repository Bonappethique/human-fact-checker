#!/usr/bin/env python3
"""
Vérificateur Souverain — Collecteur via Google Fact Check API
=============================================================
Back-office uniquement. Aucun appel depuis l'extension utilisateur.

Modes :
  HISTORIQUE  → récupère 2 ans de fact-checks, mois par mois
  QUOTIDIEN   → incrémental depuis le dernier article connu

Variables d'environnement :
  GOOGLE_API_KEY  → clé API Google Fact Check
  SCRAPE_MODE     → HISTORIQUE ou QUOTIDIEN (défaut)
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests
from dateutil.parser import parse as parse_date
from dateutil.relativedelta import relativedelta

# ── Configuration ────────────────────────────────────────────────────────────

API_KEY = os.environ.get("GOOGLE_API_KEY", "")
MODE = os.environ.get("SCRAPE_MODE", "QUOTIDIEN").upper()
ENDPOINT = "https://factchecktools.googleapis.com/v1alpha1/claims:search"
DATA_FILE = Path("data/factchecks.json")
LANGUAGES = ["fr", "en"]
PAGE_SIZE = 100  # max autorisé par Google
MAX_PAGES_PER_QUERY = 20  # garde-fou anti-boucle infinie
DELAY_BETWEEN_REQUESTS = 0.5  # secondes — respect du quota

# Mapping verdict Google → catégorie française
VERDICT_MAP = {
    "false":           "FAUX",
    "mostly false":    "FAUX",
    "pants on fire":   "FAUX",
    "fake":            "FAUX",
    "incorrect":       "FAUX",
    "true":            "VRAI",
    "mostly true":     "VRAI",
    "correct":         "VRAI",
    "mixture":         "NUANCÉ",
    "half true":       "NUANCÉ",
    "partly false":    "NUANCÉ",
    "misleading":      "TROMPEUR",
    "out of context":  "TROMPEUR",
    "exaggerated":     "TROMPEUR",
    "satire":          "SATIRE",
    "unverified":      "INDÉTERMINÉ",
    "unproven":        "INDÉTERMINÉ",
    "no evidence":     "INDÉTERMINÉ",
}


def normalize_verdict(raw: str) -> str:
    """Mappe un verdict brut vers une catégorie française."""
    if not raw:
        return "INDÉTERMINÉ"
    key = raw.strip().lower()
    if key in VERDICT_MAP:
        return VERDICT_MAP[key]
    for pattern, category in VERDICT_MAP.items():
        if pattern in key:
            return category
    return "INDÉTERMINÉ"


def load_existing() -> dict:
    """Charge la base existante ou retourne un squelette vide."""
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            if "factchecks" in data and isinstance(data["factchecks"], list):
                return data
        except (json.JSONDecodeError, KeyError):
            pass
    return {"last_updated": None, "total_count": 0, "sources": [], "factchecks": []}


def get_cutoff_date(existing: dict) -> datetime:
    """Détermine la date à partir de laquelle chercher."""
    if MODE == "HISTORIQUE":
        return datetime.utcnow() - relativedelta(years=2)

    if existing["factchecks"]:
        dates = []
        for fc in existing["factchecks"]:
            try:
                dates.append(parse_date(fc.get("date_published", "")))
            except (ValueError, TypeError):
                continue
        if dates:
            return max(dates)

    return datetime.utcnow() - timedelta(days=7)


def search_api(query: str = "", language: str = "fr",
               max_age_days: int | None = None) -> list[dict]:
    """Interroge l'API Google Fact Check avec pagination complète."""
    results = []
    page_token = None

    for page in range(MAX_PAGES_PER_QUERY):
        params = {
            "key": API_KEY,
            "languageCode": language,
            "pageSize": PAGE_SIZE,
        }
        if query:
            params["query"] = query
        if max_age_days and max_age_days > 0:
            params["maxAgeDays"] = max_age_days
        if page_token:
            params["pageToken"] = page_token

        url = f"{ENDPOINT}?{urlencode(params)}"

        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 429:
                print("  ⏳ Rate limit atteint, pause 10s…")
                time.sleep(10)
                continue
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  ⚠ Erreur API : {e}")
            break

        data = resp.json()
        claims = data.get("claims", [])
        if not claims:
            break

        results.extend(claims)
        page_token = data.get("nextPageToken")
        if not page_token:
            break

        time.sleep(DELAY_BETWEEN_REQUESTS)

    return results


def parse_claim(claim: dict) -> list[dict]:
    """Transforme un objet 'claim' Google en entrées normalisées."""
    entries = []
    claim_text = claim.get("text", "").strip()

    for review in claim.get("claimReview", []):
        source_url = review.get("url", "")
        if not source_url:
            continue

        raw_verdict = review.get("textualRating", "")
        pub_date = review.get("reviewDate", "")
        try:
            date_str = parse_date(pub_date).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            date_str = ""

        lang = review.get("languageCode", "fr")

        entries.append({
            "claim": claim_text,
            "title": review.get("title", claim_text),
            "verdict_label": raw_verdict,
            "verdict_category": normalize_verdict(raw_verdict),
            "explanation": claim_text,
            "source_name": review.get("publisher", {}).get("name", "Inconnu"),
            "source_url": source_url,
            "date_published": date_str,
            "lang": lang[:2] if lang else "fr",
        })

    return entries


def merge(existing: list[dict], new_entries: list[dict]) -> list[dict]:
    """Fusionne sans doublons (clé = source_url)."""
    by_url = {fc["source_url"]: fc for fc in existing}
    added = 0
    for entry in new_entries:
        if entry["source_url"] not in by_url:
            by_url[entry["source_url"]] = entry
            added += 1
    print(f"  ✅ {added} nouveaux articles ajoutés ({len(new_entries)} traités)")
    return sorted(by_url.values(), key=lambda x: x.get("date_published", ""), reverse=True)


def collect_historical() -> list[dict]:
    """Mode HISTORIQUE : balaye mois par mois sur 2 ans, par langue."""
    all_entries = []
    now = datetime.utcnow()
    start = now - relativedelta(years=2)

    for lang in LANGUAGES:
        current = start
        while current < now:
            next_month = current + relativedelta(months=1)
            days_diff = (min(next_month, now) - current).days
            if days_diff <= 0:
                break

            label = current.strftime("%Y-%m")
            age_from_now = (now - current).days
            print(f"📅 [{lang.upper()}] Période {label} ({days_diff}j)…")

            claims = search_api(language=lang, max_age_days=age_from_now)
            for c in claims:
                all_entries.extend(parse_claim(c))

            current = next_month
            time.sleep(DELAY_BETWEEN_REQUESTS)

        # Dédupliquer au fur et à mesure
        seen = {}
        deduped = []
        for e in all_entries:
            if e["source_url"] not in seen:
                seen[e["source_url"]] = True
                deduped.append(e)
        all_entries = deduped

    print(f"\n📊 Total HISTORIQUE : {len(all_entries)} articles uniques")
    return all_entries


def collect_incremental(cutoff: datetime) -> list[dict]:
    """Mode QUOTIDIEN : ne récupère que les articles récents."""
    all_entries = []
    days_since = max(1, (datetime.utcnow() - cutoff).days + 1)

    for lang in LANGUAGES:
        print(f"🔄 [{lang.upper()}] Derniers {days_since} jours…")
        claims = search_api(language=lang, max_age_days=days_since)
        for c in claims:
            all_entries.extend(parse_claim(c))
        time.sleep(DELAY_BETWEEN_REQUESTS)

    print(f"📊 Total incrémental : {len(all_entries)} articles bruts")
    return all_entries


def main():
    if not API_KEY:
        print("❌ GOOGLE_API_KEY manquante. Abandon.")
        sys.exit(1)

    print(f"🚀 Vérificateur Souverain — Mode {MODE}")
    print(f"   Langues : {', '.join(LANGUAGES)}")

    existing = load_existing()
    cutoff = get_cutoff_date(existing)
    print(f"   Date de coupure : {cutoff.strftime('%Y-%m-%d')}")
    print(f"   Articles existants : {len(existing['factchecks'])}\n")

    if MODE == "HISTORIQUE":
        new_entries = collect_historical()
    else:
        new_entries = collect_incremental(cutoff)

    all_factchecks = merge(existing["factchecks"], new_entries)

    sources = sorted(set(fc["source_name"] for fc in all_factchecks if fc.get("source_name")))
    output = {
        "last_updated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_count": len(all_factchecks),
        "sources": sources,
        "factchecks": all_factchecks,
    }

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n✅ Fichier écrit : {DATA_FILE}")
    print(f"   {output['total_count']} articles | {len(sources)} sources")


if __name__ == "__main__":
    main()
