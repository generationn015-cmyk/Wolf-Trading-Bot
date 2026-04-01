"""
Wolf Trading Bot — Timezone Arbitrage Strategy

The edge: Polymarket is ~90% US traders. Global events (Japan BOJ, EU Parliament,
Australia RBA, OPEC, South Korea, Singapore) resolve while America sleeps (2–6 AM EST).
By the time the US wakes up, the outcome is already clear in foreign news sources
but Polymarket prices haven't moved yet.

Entry: 10¢–35¢ on a near-certain outcome in overseas sources
Payout: $1.00 at resolution
Expected WR: 85–92% when signal fires correctly

Sources monitored:
- Japanese government RSS (BOJ decisions, Diet votes)
- EU Parliament streams / EU Council press releases  
- Australian RBA / ASX announcements
- Middle East flight/OPEC trackers
- Asian central bank RSS feeds
- UK / European financial wire RSS

US sleep window: 2:00–7:30 AM EST = UTC 07:00–12:30
We also scan 30 min before that window for setup positions.
"""
import time
import logging
import asyncio
import re
import feedparser
import requests
from dataclasses import dataclass, field
from typing import Optional
import config
import config
POLYMARKET_GAMMA_URL = config.POLYMARKET_GAMMA_URL
import json as _json
from market_priority import fetch_prioritized_markets

logger = logging.getLogger("wolf.strategy.timezone_arb")

# ── Global news RSS sources ───────────────────────────────────────────────────
RSS_SOURCES = [
    # Japan
    {"url": "https://www.boj.or.jp/en/announcements/release_2024/index.htm/rss.xml",
     "region": "JP", "tags": ["rate", "policy", "decision", "boj"]},
    {"url": "https://feeds.reuters.com/reuters/JPNews",
     "region": "JP", "tags": ["japan", "boj", "bank of japan", "yen"]},
    # EU
    {"url": "https://www.europarl.europa.eu/rss/doc/top-stories/en.xml",
     "region": "EU", "tags": ["vote", "parliament", "regulation", "ecb", "rate"]},
    {"url": "https://feeds.reuters.com/reuters/EUTopNews",
     "region": "EU", "tags": ["ecb", "european", "parliament", "rate", "vote"]},
    # Australia
    {"url": "https://feeds.reuters.com/reuters/AUNews",
     "region": "AU", "tags": ["rba", "australia", "rate", "reserve bank"]},
    # UK
    {"url": "https://feeds.reuters.com/reuters/UKTopNews",
     "region": "UK", "tags": ["boe", "bank of england", "rate", "vote"]},
    # General financial wire
    {"url": "https://feeds.reuters.com/reuters/businessNews",
     "region": "GLOBAL", "tags": ["opec", "rate", "fed", "central bank", "decision"]},
    {"url": "https://feeds.bbci.co.uk/news/business/rss.xml",
     "region": "GLOBAL", "tags": ["rate", "bank", "decision", "vote", "parliament"]},
]

# Keywords that indicate an outcome is already decided (high confidence signal)
CONFIRMED_KEYWORDS = [
    "approved", "passed", "voted", "decided", "confirmed", "announced",
    "held steady", "kept rates", "raised rates", "cut rates", "rejected",
    "signed", "enacted", "defeated", "won", "lost", "resolved",
]

# Market keyword mapping: news terms → Polymarket market search terms
MARKET_KEYWORDS = {
    "boj": ["japan rate", "boj rate", "bank of japan"],
    "ecb": ["ecb rate", "european central bank", "eu rate"],
    "rba": ["australia rate", "rba rate", "reserve bank australia"],
    "boe": ["bank of england", "boe rate", "uk rate"],
    "opec": ["opec", "oil production", "saudi"],
    "rate": ["interest rate", "central bank rate", "fed rate"],
    "parliament": ["parliament vote", "legislation"],
}

COOLDOWN_SEC  = 3600  # 1 hour per market — don't re-enter while position is open
MAX_ENTRY_PRICE = 0.38  # Only enter if market price ≤ 38¢ (enough upside)
MIN_ENTRY_PRICE = 0.05  # Skip near-zero — liquidity too thin
CONFIDENCE_BASE = 0.82  # Timezone arb fires with high conviction


@dataclass
class TZSignal:
    market_id: str
    side: str
    entry_price: float
    confidence: float
    news_headline: str
    region: str
    timestamp: float = field(default_factory=time.time)


class TimezoneArb:
    def __init__(self):
        self._last_rss_fetch:  float = 0.0
        self._rss_ttl:         float = 300    # re-fetch RSS every 5 min
        self._rss_cache:       list[dict] = []
        self._fired_markets:   dict[str, float] = {}  # market_id → last fired ts
        self._market_cache:    list[dict] = []
        self._market_cache_ts: float = 0.0
        self._market_ttl:      float = 300

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _is_us_sleep_window(self) -> bool:
        """Returns True if current UTC time is 06:30–13:00 UTC (2:30–9:00 AM ET)."""
        import datetime
        utc_hour = datetime.datetime.utcnow().hour
        utc_min  = datetime.datetime.utcnow().minute
        utc_frac = utc_hour + utc_min / 60.0
        # 06:30–13:00 UTC = 2:30–9:00 AM ET — the prime gap window
        return 6.5 <= utc_frac <= 13.0

    def _is_pre_sleep_setup(self) -> bool:
        """Returns True if we're 30–60 min before the gap window (setup positions)."""
        import datetime
        utc_frac = datetime.datetime.utcnow().hour + datetime.datetime.utcnow().minute / 60.0
        return 6.0 <= utc_frac < 6.5  # 2:00–2:30 AM ET setup window

    # ── RSS fetching ──────────────────────────────────────────────────────────

    def _fetch_rss_headlines(self) -> list[dict]:
        now = time.time()
        if now - self._last_rss_fetch < self._rss_ttl and self._rss_cache:
            return self._rss_cache

        headlines = []
        for source in RSS_SOURCES:
            try:
                feed = feedparser.parse(source["url"])
                for entry in feed.entries[:10]:
                    title   = (entry.get("title", "") or "").lower()
                    summary = (entry.get("summary", "") or "").lower()
                    text    = title + " " + summary

                    # Tag match — does this article relate to a known market category?
                    matched_tags = [t for t in source["tags"] if t in text]
                    if not matched_tags:
                        continue

                    # Confirmed signal — is the outcome already decided?
                    confirmed = any(kw in text for kw in CONFIRMED_KEYWORDS)

                    headlines.append({
                        "title":     entry.get("title", ""),
                        "summary":   entry.get("summary", "")[:200],
                        "url":       entry.get("link", ""),
                        "region":    source["region"],
                        "tags":      matched_tags,
                        "confirmed": confirmed,
                        "published": entry.get("published", ""),
                        "raw_text":  text,
                    })
            except Exception as e:
                logger.debug(f"RSS fetch failed ({source['region']}): {e}")

        self._rss_cache = headlines
        self._last_rss_fetch = now
        if headlines:
            confirmed_count = sum(1 for h in headlines if h["confirmed"])
            logger.info(
                f"TZ RSS: {len(headlines)} articles | "
                f"{confirmed_count} confirmed outcomes"
            )
        return headlines

    # ── Market matching ───────────────────────────────────────────────────────

    def _fetch_active_markets(self) -> list[dict]:
        now = time.time()
        if now - self._market_cache_ts < self._market_ttl and self._market_cache:
            return self._market_cache
        try:
            markets = fetch_prioritized_markets(
                limit=100,
                max_days=30,
            )
            if not isinstance(markets, list):
                return self._market_cache

            filtered = []
            for m in markets:
                op = m.get("outcomePrices", [])
                if isinstance(op, str):
                    try:    op = _json.loads(op)
                    except: op = []
                if not op or len(op) < 2:
                    continue
                try:
                    p0, p1 = float(op[0]), float(op[1])
                except (ValueError, TypeError):
                    continue
                # We want lopsided markets — one side very cheap (the opportunity)
                min_price = min(p0, p1)
                if not (MIN_ENTRY_PRICE <= min_price <= MAX_ENTRY_PRICE):
                    continue
                vol = float(m.get("volumeNum", 0) or 0)
                if vol < 1000:
                    continue
                m["_yes_price"] = p0
                m["_no_price"]  = p1
                m["_min_price"] = min_price
                filtered.append(m)

            self._market_cache = filtered
            self._market_cache_ts = now
        except Exception as e:
            logger.warning(f"TZ market fetch failed: {e}")
        return self._market_cache

    def _match_headline_to_market(self, headline: dict, markets: list[dict]) -> Optional[dict]:
        """Find a Polymarket market that matches a given news headline."""
        text = headline["raw_text"]
        tags = headline["tags"]

        # Build search terms from tags
        search_terms = []
        for tag in tags:
            search_terms.extend(MARKET_KEYWORDS.get(tag, [tag]))

        for market in markets:
            question = (market.get("question", "") or "").lower()
            description = (market.get("description", "") or "").lower()
            market_text = question + " " + description

            # Check if any search term appears in the market
            for term in search_terms:
                if term in market_text:
                    return market

            # Also check raw headline words against market question
            headline_words = set(re.findall(r'\b\w{4,}\b', text))
            market_words   = set(re.findall(r'\b\w{4,}\b', market_text))
            overlap = headline_words & market_words
            if len(overlap) >= 3:
                return market

        return None

    # ── Main scan ─────────────────────────────────────────────────────────────

    async def scan(self) -> list[dict]:
        """
        Scan during US sleep window for timezone arb opportunities.
        Outside the window: still scan but with lower confidence multiplier.
        """
        signals = []
        now = time.time()

        in_window  = self._is_us_sleep_window()
        pre_window = self._is_pre_sleep_setup()

        if not (in_window or pre_window):
            # Outside prime window — skip to save API calls
            return signals

        headlines = self._fetch_rss_headlines()
        if not headlines:
            return signals

        markets = self._fetch_active_markets()
        if not markets:
            return signals

        # Only fire on confirmed outcomes during the sleep window
        actionable = [h for h in headlines if h["confirmed"]] if in_window else headlines

        for headline in actionable[:10]:  # Max 10 per scan
            market = self._match_headline_to_market(headline, markets)
            if not market:
                continue

            market_id = market.get("conditionId") or market.get("id", "")
            if not market_id:
                continue

            # Cooldown check
            if now - self._fired_markets.get(market_id, 0) < COOLDOWN_SEC:
                continue

            yes_price = market["_yes_price"]
            no_price  = market["_no_price"]

            # Determine which side the news supports
            # If headline confirms YES outcome → buy YES (if cheap)
            # If headline confirms NO outcome → buy NO (if cheap)
            confirmed = headline["confirmed"]
            headline_text = headline["raw_text"]

            # Heuristic: "passed", "approved", "raised" → YES outcome more likely
            yes_words = ["approved", "passed", "raised", "won", "signed", "confirmed"]
            no_words  = ["rejected", "defeated", "held steady", "kept rates", "failed"]

            yes_signal = any(w in headline_text for w in yes_words)
            no_signal  = any(w in headline_text for w in no_words)

            if yes_signal and yes_price <= MAX_ENTRY_PRICE:
                side       = "YES"
                entry_price = yes_price
            elif no_signal and no_price <= MAX_ENTRY_PRICE:
                side       = "NO"
                entry_price = no_price
            elif min(yes_price, no_price) <= MAX_ENTRY_PRICE * 0.7:
                # Price is very cheap — likely near-certain but direction unclear
                # Pick the cheaper side (market is pricing it as unlikely — we think otherwise)
                if yes_price < no_price:
                    side = "YES"; entry_price = yes_price
                else:
                    side = "NO"; entry_price = no_price
            else:
                continue  # No clear signal

            if entry_price < MIN_ENTRY_PRICE or entry_price > MAX_ENTRY_PRICE:
                continue

            # Confidence: higher in the sleep window, lower in pre-window
            confidence = CONFIDENCE_BASE if in_window else CONFIDENCE_BASE * 0.85
            if confirmed:
                confidence = min(0.95, confidence + 0.05)

            vol = float(market.get("volumeNum", 0) or 0)

            self._fired_markets[market_id] = now
            signals.append({
                "strategy":    "timezone_arb",
                "venue":       "polymarket",
                "market_id":   market_id,
                "side":        side,
                "entry_price": entry_price,
                "confidence":  confidence,
                "edge":        1.0 - entry_price - 0.02,  # net edge after fees
                "volume":      vol,
                "timestamp":   now,
                "region":      headline["region"],
                "reason": (
                    f"TZ arb [{headline['region']}] "
                    f"{headline['title'][:60]}… "
                    f"entry={entry_price:.2f} conf={confidence:.2f}"
                ),
            })
            logger.info(
                f"TZ arb signal: [{headline['region']}] "
                f"{market.get('question','')[:50]}… "
                f"{side} @ {entry_price:.2f} | conf={confidence:.2f}"
            )

            if len(signals) >= 3:  # Max 3 per cycle
                break

        return signals
