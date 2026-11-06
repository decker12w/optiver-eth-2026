"""Shared news helpers: how good or bad a post is (score) and which stocks it is about (about).

Not a strategy (the leading "_" makes the loader skip it); news_trader and stock_mm import it.
Everything is word lookups, a few microseconds per post. Extend the lists from the event's posts:
every post is logged with its score and matched words.
"""
from __future__ import annotations

import re

# Company names as they may appear in posts -> ticker. Tickers themselves ("NVDA") always match.
ALIASES = {
    "nvidia": "NVDA", "advanced micro devices": "AMD", "broadcom": "AVGO", "coreweave": "CRWV",
    "micron": "MU", "nebius": "NBIS", "sandisk": "SNDK", "vertiv": "VRT",
}
# A post that names no company but uses one of these is about the whole sector (all our stocks).
SECTOR_WORDS = {
    "chip", "chips", "chipmaker", "chipmakers", "semiconductor", "semiconductors", "ai", "gpu",
    "gpus", "datacenter", "datacenters", "hyperscaler", "hyperscalers", "sector", "tech", "nasdaq",
    "market", "markets", "stocks", "economy", "fed", "inflation", "tariff", "tariffs",
}
SECTOR_PHRASES = {"data center", "data centre", "interest rates"}

# Sentiment weights: 2 = strong news, 1 = mild. Phrases are matched before single words.
PHRASES = {
    # bad
    "profit warning": -2, "misses expectations": -2, "misses estimates": -2, "cuts guidance": -2,
    "lowers guidance": -2, "cuts forecast": -2, "steps down": -1, "class action": -2,
    "export ban": -2, "export controls": -1,
    # good
    "beats expectations": 2, "beats estimates": 2, "raises guidance": 2, "raises forecast": 2,
    "record profit": 2, "record revenue": 2, "share buyback": 1, "takeover bid": 2,
    "closes probe": 1, "drops probe": 1, "ends probe": 1, "cleared of": 1,
}
WORDS = {
    # bad, strong
    "fraud": -2, "bankruptcy": -2, "bankrupt": -2, "insolvency": -2, "scandal": -2, "plunges": -2,
    "plummets": -2, "crash": -2, "crashes": -2, "collapse": -2, "collapses": -2, "recall": -2,
    "recalls": -2, "default": -2, "defaults": -2, "plunge": -2, "plummet": -2, "tumble": -2,
    "tumbles": -2,
    # bad, mild
    "miss": -1, "misses": -1, "missed": -1, "probe": -1, "investigation": -1, "investigates": -1,
    "lawsuit": -1, "sues": -1, "sued": -1, "fined": -1, "penalty": -1, "fall": -1, "falls": -1,
    "drop": -1, "drops": -1, "slump": -1, "sink": -1, "sinks": -1,
    "slumps": -1, "declines": -1, "loss": -1, "losses": -1, "weak": -1, "weaker": -1, "cuts": -1,
    "layoffs": -1, "resigns": -1, "delay": -1, "delays": -1, "delayed": -1, "breach": -1,
    "hacked": -1, "outage": -1, "halts": -1, "halted": -1, "banned": -1, "warning": -1,
    "warns": -1, "downgrade": -1, "downgrades": -1, "downgraded": -1, "disappointing": -1,
    "disappoints": -1, "underperform": -1, "shortage": -1, "allegations": -1, "bearish": -1,
    "restrictions": -1, "sanctions": -1, "tariff": -1, "tariffs": -1,
    # good, strong
    "beats": 2, "beat": 2, "soar": 2, "soars": 2, "surge": 2, "surges": 2, "skyrockets": 2,
    "breakthrough": 2,
    "record": 1,
    # good, mild
    "wins": 1, "won": 1, "rise": 1, "rises": 1, "gain": 1, "gains": 1, "jump": 1, "jumps": 1,
    "rally": 1, "rallies": 1, "growth": 1, "grows": 1, "strong": 1, "stronger": 1, "boost": 1,
    "boosts": 1, "approval": 1, "approved": 1,
    "approves": 1, "partnership": 1, "expands": 1, "outperform": 1, "outperforms": 1,
    "upgrade": 1, "upgrades": 1, "upgraded": 1, "raises": 1, "exceeds": 1, "tops": 1,
    "awarded": 1, "bullish": 1, "rebound": 1, "dividend": 1, "buyback": 1, "boom": 1,
}
NEGATIONS = {"not", "no", "never", "denies", "denied", "deny", "rejects", "dismisses", "without"}
_WORD = re.compile(r"[a-z0-9]+")


def score(text: str) -> tuple[int, list[str]]:
    """Sentiment of a post: (score, matched terms). > 0 good news, < 0 bad news, 0 neither.

    A negation just before a word ("not", "denies", ...) cancels that word, so "X denies fraud"
    is not bad news. Words inside a matched phrase are not counted again.
    """
    words = _WORD.findall(text.lower())
    total, hits, i = 0, [], 0
    while i < len(words):
        for n in (3, 2):  # longest phrase first
            phrase = " ".join(words[i:i + n])
            if i + n <= len(words) and phrase in PHRASES:
                weight = PHRASES[phrase]
                break
        else:
            n, phrase, weight = 1, words[i], WORDS.get(words[i], 0)
        if weight:
            if any(w in NEGATIONS for w in words[max(0, i - 2):i]):
                hits.append(f"(not {phrase})")
            else:
                total += weight
                hits.append(f"{phrase}{weight:+d}")
        i += n
    return total, hits


def companies(instruments: dict) -> dict[str, list[str]]:
    """Stocks per company, keyed by lowercase ticker: the instrument id before any "_"
    ("NVDA" -> "nvda", "PHILIPS_A" and "PHILIPS_B" -> "philips")."""
    found: dict[str, list[str]] = {}
    for instrument_id, instrument in instruments.items():
        if getattr(instrument.instrument_type, "name", instrument.instrument_type) == "STOCK":
            found.setdefault(instrument_id.split("_")[0].lower(), []).append(instrument_id)
    return found


def about(text: str, known: dict[str, list[str]]) -> tuple[set[str], bool]:
    """Which companies (keys of `known`) a post is about, and whether it is sector-wide news.

    Returns (named, sector). `named` is empty when no company is mentioned; `sector` is True only
    then, and only if the post uses a sector word. With a single company, every post is about it.
    """
    lowered = text.lower()
    words = set(_WORD.findall(lowered))
    named = {key for key in known if key in words}
    named |= {t.lower() for alias, t in ALIASES.items() if alias in lowered and t.lower() in known}
    if not named and len(known) == 1:
        return set(known), False
    sector = not named and (bool(words & SECTOR_WORDS) or any(p in lowered for p in SECTOR_PHRASES))
    return named, sector
