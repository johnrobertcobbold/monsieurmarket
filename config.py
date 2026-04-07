# config.py
"""
MonsieurMarket — Central Configuration
"""

CONFIG = {
    # ── POLLING ──────────────────────────────────────────────────────────────
    "poll_interval_minutes":   60,
    "dedup_window_hours":       4,
    "alert_threshold":          6,

    # ── QUIET HOURS (Paris time, 24h) ────────────────────────────────────────
    "quiet_hours_start": 0,
    "quiet_hours_end":   7,

    # ── WHALE ────────────────────────────────────────────────────────────────
    "whale_threshold_usd":      10000,
    "whale_instant_alert_usd": 100000,

    # ── POLYMARKET ───────────────────────────────────────────────────────────
    "polymarket_markets_file": "polymarket/polymarket_markets.json",

    # ── PRICE WATCHER ────────────────────────────────────────────────────────
    "price_watcher": {
        "enabled":               True,
        "trigger_pct_from_open": 1.5,
        "cooldown_min":          15,
        "rolling_windows": [
            (1,  1.0,  "1min"),
            (5,  1.0,  "5min"),
            (10, 1.5,  "10min"),
        ],
    },

    # ── WHALE TRIGGERS ───────────────────────────────────────────────────────
    "whale_triggers": {
        "single_trade_usd":   50000,
        "single_trader_usd":  75000,
        "net_flow_usd":      150000,
        "net_flow_min_pct":    0.70,
    },

    # ── WEEKLY DIGEST ────────────────────────────────────────────────────────
    "weekly_digest": {
        "enabled": True,
        "day":     "sunday",
        "time":    "09:00",
    },

    # ── EXTERNAL API ENDPOINTS ───────────────────────────────────────────────────
    "polymarket_data_api": "https://data-api.polymarket.com",
    "polymarket_ws":       "wss://ws-subscriptions-clob.polymarket.com/ws/market",

    # ── DISPLAY ──────────────────────────────────────────────────────────────
    "display_timezone": "Europe/Paris",

    # ── THEMES ───────────────────────────────────────────────────────────────
    "themes": [
        {
            "name":          "US Ground Op / Kharg",
            "active":        True,
            "wake_override": False,
            "keywords": [
                "Kharg", "Kharg Island", "Hormuz", "Strait of Hormuz",
                "boots on the ground", "ground forces Iran",
                "US troops Iran", "special forces Iran",
                "Iran military operation", "Iran invasion",
                "carrier group Iran", "amphibious Iran",
            ],
            "tradables": [
                {
                    "name": "Brent Crude", "ticker": "BRN",
                    "type": "turbo_long", "my_position": "long",
                    "signal_direction": "bullish", "conviction": "high",
                },
                {
                    "name": "Thales", "ticker": "HO.PA",
                    "type": "equity", "my_position": "long",
                    "signal_direction": "bullish", "conviction": "high",
                },
                {
                    "name": "TotalEnergies", "ticker": "TTE.PA",
                    "type": "equity", "my_position": "watching",
                    "signal_direction": "bullish", "conviction": "medium",
                },
                {
                    "name": "FTSE 100", "ticker": "UKX",
                    "type": "index", "my_position": "watching",
                    "signal_direction": "bullish", "conviction": "medium",
                    "note": "Oil-heavy index, benefits from Brent spike",
                },
                {
                    "name": "CAC 40", "ticker": "PX1",
                    "type": "index", "my_position": "short_candidate",
                    "signal_direction": "bearish", "conviction": "medium",
                    "note": "Luxury-heavy, hurt by oil shock and risk-off",
                },
            ],
        },
        {
            "name":          "Houthi Escalation",
            "active":        True,
            "wake_override": False,
            "keywords": [
                "Houthi", "Houthis", "Ansarallah",
                "Yemen", "Yemen strike", "Yemen missile",
                "Red Sea attack", "Red Sea shipping",
                "tanker attack", "shipping disruption",
                "Bab el-Mandeb", "Gulf of Aden",
                "Houthi missile Israel", "Houthi drone",
            ],
            "escalation_triggers": [
                "Yanbu", "Saudi oil", "Aramco", "Ras Tanura",
            ],
            "tradables": [
                {
                    "name": "Brent Crude", "ticker": "BRN",
                    "type": "turbo_long", "my_position": "long",
                    "signal_direction": "bullish", "conviction": "high",
                },
                {
                    "name": "Thales", "ticker": "HO.PA",
                    "type": "equity", "my_position": "long",
                    "signal_direction": "bullish", "conviction": "medium",
                },
                {
                    "name": "TotalEnergies", "ticker": "TTE.PA",
                    "type": "equity", "my_position": "watching",
                    "signal_direction": "bullish", "conviction": "medium",
                },
            ],
            "alert_levels": {
                "level_1": {
                    "description": "Houthis firing at Israel",
                    "score_range":  [4, 6],
                    "note": "Ongoing background noise, mild Brent positive",
                },
                "level_2": {
                    "description": "Houthis targeting Red Sea shipping",
                    "score_range":  [6, 8],
                    "note": "Brent +$5-10 expected. Check turbo barrier.",
                },
                "level_3": {
                    "description": "Houthis targeting Yanbu / Saudi infrastructure",
                    "score_range":  [8, 10],
                    "note": "Bloomberg Economics: $140 Brent scenario. ACT NOW.",
                    "wake_override": True,
                },
            },
        },
        {
            "name":          "European Rearmament",
            "active":        False,
            "wake_override": False,
            "keywords": [
                "NATO spending", "defense budget Europe",
                "rearmament", "European army", "defense procurement",
            ],
            "tradables": [
                {"name": "Rheinmetall", "ticker": "RHM.DE",
                 "signal_direction": "bullish", "conviction": "high"},
                {"name": "Leonardo",    "ticker": "LDO.MI",
                 "signal_direction": "bullish", "conviction": "high"},
                {"name": "BAE Systems", "ticker": "BA.L",
                 "signal_direction": "bullish", "conviction": "high"},
            ],
        },
        {
            "name":          "French Political Risk",
            "active":        False,
            "wake_override": False,
            "keywords": [
                "dissolution Assemblée", "élections France",
                "motion de censure", "gouvernement chute",
                "crise politique France",
            ],
            "tradables": [
                {"name": "CAC 40", "ticker": "PX1",
                 "signal_direction": "bearish", "conviction": "high"},
            ],
        },
    ],
}