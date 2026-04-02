"""
Wolf Trading Bot — Market Classifier
NLP keyword classifier that adjusts confidence based on market category.

Different prediction market categories have very different win rates:
- Regulatory/economic markets (Fed rate, CPI, elections) resolve on
  verifiable official data — high reliability, boost confidence.
- Celebrity/social/speculative markets have near-random outcomes — suppress.

This does NOT filter markets out — it adjusts the confidence multiplier
so position sizing and threshold checks naturally favor reliable categories.

Usage:
    from market_classifier import classify_market, get_category_label

    multiplier = classify_market("Will the Fed raise rates in March?")
    # multiplier > 1.0 = boost, < 1.0 = suppress, 1.0 = neutral
    final_confidence = min(0.99, base_confidence * multiplier)
"""
import re
import logging

logger = logging.getLogger("wolf.classifier")

# ── Category definitions ──────────────────────────────────────────────────────
# Each category: (label, keywords, multiplier)
# Keywords are matched as whole words or substrings (case-insensitive).
# Multiplier applied to base confidence before Kelly sizing.

_CATEGORIES: list[tuple[str, list[str], float]] = [
    # ── HIGH RELIABILITY (boost) ──────────────────────────────────────────────
    # These resolve on hard, verifiable official data with known timelines.
    (
        "economic_indicator",
        [
            "federal reserve", "fed rate", "fomc", "interest rate",
            "cpi", "inflation", "gdp", "unemployment", "nonfarm", "payroll",
            "pce", "ppi", "retail sales", "consumer price",
        ],
        1.18,
    ),
    (
        "sports_outcome",
        [
            "nba champion", "nba finals", "super bowl", "world series",
            "stanley cup", "nfl champion", "world cup champion",
            "march madness", "ncaa champion", "formula 1", "f1 champion",
            "wimbledon", "us open winner", "masters winner",
        ],
        1.12,
    ),
    (
        "election_official",
        [
            "presidential election", "senate election", "house election",
            "governor election", "ballot", "electoral", "primary election",
            "general election", "runoff election", "referendum",
        ],
        1.10,
    ),
    (
        "crypto_price_binary",
        [
            "btc above", "btc below", "bitcoin above", "bitcoin below",
            "eth above", "eth below", "ethereum above", "ethereum below",
            "bitcoin price", "eth price", "btc price", "crypto price",
        ],
        1.08,
    ),
    (
        "corporate_event",
        [
            "earnings", "revenue", "eps", "quarterly results",
            "ipo", "acquisition", "merger", "bankruptcy", "layoffs announced",
            "fda approval", "fda decision", "drug approval",
        ],
        1.08,
    ),

    # ── NEUTRAL (no adjustment) ───────────────────────────────────────────────
    # Well-defined outcomes but with moderate uncertainty.
    # (handled by returning 1.0 for unmatched markets)

    # ── LOW RELIABILITY (suppress) ────────────────────────────────────────────
    # Speculative, subjective, or social media driven — hard to predict.
    (
        "celebrity_social",
        [
            "kardashian", "taylor swift", "kanye", "beyoncé", "beyonce",
            "drake", "ariana", "rihanna", "celebrity", "influencer",
            "tiktok trend", "viral", "twitter thread",
        ],
        0.72,
    ),
    (
        "elon_twitter",
        [
            "elon musk", "elon tweet", "twitter policy", "x.com",
            "musk will", "musk says", "elon says",
        ],
        0.75,
    ),
    (
        "ai_model_speculation",
        [
            "gpt-5", "gpt5", "claude 4", "gemini ultra", "next openai",
            "new model", "llm release", "ai model", "chatgpt will",
            "openai will release",
        ],
        0.75,
    ),
    (
        "subjective_opinion",
        [
            "most popular", "who will win oscars", "best picture",
            "grammy", "emmy", "golden globe", "award winner",
            "who will be", "will x say", "will they announce",
        ],
        0.78,
    ),
    (
        "conspiracy_paranormal",
        [
            "alien", "ufo", "uap", "extraterrestrial", "bigfoot",
            "conspiracy", "deep state", "illuminati", "flat earth",
        ],
        0.60,
    ),
    (
        "prediction_about_predictions",
        [
            "polymarket", "kalshi odds", "betting market", "will wolf",
            "prediction market", "will the market",
        ],
        0.70,
    ),
]

# Pre-compile patterns for performance
_COMPILED: list[tuple[str, list[re.Pattern], float]] = [
    (
        label,
        [re.compile(re.escape(kw), re.IGNORECASE) for kw in keywords],
        multiplier,
    )
    for label, keywords, multiplier in _CATEGORIES
]


def classify_market(question: str, category: str = "") -> float:
    """
    Return confidence multiplier for a market based on its question text.

    Args:
        question: The market's question string (e.g. "Will BTC exceed $100k?")
        category: Optional category tag from the venue API

    Returns:
        float: Multiplier to apply to base confidence.
               > 1.0 = reliable category (boost)
               = 1.0 = neutral / unknown
               < 1.0 = unreliable category (suppress)

    The multiplier is always clamped by the caller with min(0.99, conf * mult).
    This function never directly changes confidence — it only provides the factor.
    """
    text = f"{question} {category}".lower()

    best_suppress = 1.0    # Strongest suppressor found
    best_boost    = 1.0    # Strongest booster found

    for label, patterns, multiplier in _COMPILED:
        for pattern in patterns:
            if pattern.search(text):
                if multiplier < 1.0:
                    # Take the MOST aggressive suppress (lowest value)
                    best_suppress = min(best_suppress, multiplier)
                else:
                    # Take the STRONGEST boost (highest value)
                    best_boost = max(best_boost, multiplier)
                break  # One match per category is enough

    # If any suppressor matched, it overrides any boost
    # (safety: we never boost a suppressed category)
    if best_suppress < 1.0:
        return best_suppress

    return best_boost


def get_category_label(question: str, category: str = "") -> str:
    """Return the matched category label for logging/debugging."""
    text = f"{question} {category}".lower()
    for label, patterns, _ in _COMPILED:
        for pattern in patterns:
            if pattern.search(text):
                return label
    return "neutral"


def is_suppressed(question: str, category: str = "") -> bool:
    """Quick check: returns True if this market should be treated with reduced confidence."""
    return classify_market(question, category) < 1.0


def is_boosted(question: str, category: str = "") -> bool:
    """Quick check: returns True if this market is in a high-reliability category."""
    return classify_market(question, category) > 1.0
