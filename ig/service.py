"""
ig/service.py — IG Markets REST session and market snapshots.
"""

import os
import logging

log = logging.getLogger('MonsieurMarket')

try:
    from trading_ig import IGService
    IG_AVAILABLE = True
except ImportError:
    IG_AVAILABLE = False

_ig_service = None


def get_ig_service():
    global _ig_service
    if not IG_AVAILABLE:
        return None
    username = os.getenv("IG_USERNAME")
    password = os.getenv("IG_PASSWORD")
    api_key  = os.getenv("IG_API_KEY")
    if not all([username, password, api_key]):
        return None
    if _ig_service is not None:
        return _ig_service
    try:
        svc = IGService(username, password, api_key, acc_type="DEMO")
        svc.create_session()
        acc_number = os.getenv("IG_ACC_NUMBER")
        if acc_number:
            svc.switch_account(acc_number, False)
        _ig_service = svc
        log.info("IG Markets session established (demo)")
    except Exception as e:
        log.warning(f"IG Markets login failed: {e}")
        _ig_service = None
    return _ig_service

def get_ig_service_live():
    """Read-only live service for market discovery."""
    global _ig_service_live
    username = os.getenv("IG_USERNAME_LIVE")
    password = os.getenv("IG_PASSWORD_LIVE")
    api_key  = os.getenv("IG_API_KEY_LIVE")
    if not all([username, password, api_key]):
        return None
    if _ig_service_live is not None:
        return _ig_service_live
    try:
        svc = IGService(username, password, api_key, acc_type="LIVE")
        svc.create_session()
        acc_number = os.getenv("IG_ACC_NUMBER_LIVE")
        if acc_number:
            svc.switch_account(acc_number, False)
        _ig_service_live = svc
        log.info("IG Markets LIVE session established (read-only)")
    except Exception as e:
        log.warning(f"IG Markets LIVE login failed: {e}")
        _ig_service_live = None
    return _ig_service_live

def reset_session():
    """Force session reconnect on next call."""
    global _ig_service
    _ig_service = None


def check_ig_brent() -> dict | None:
    svc  = get_ig_service()
    epic = os.getenv("IG_BRENT_EPIC", "").strip()
    if svc is None or not epic:
        return None
    try:
        info         = svc.fetch_market_by_epic(epic)
        snap         = info.get("snapshot", {}) if isinstance(info, dict) else {}
        inst         = info.get("instrument", {}) if isinstance(info, dict) else {}
        bid          = float(snap.get("bid") or 0)
        ask          = float(snap.get("offer") or snap.get("ask") or 0)
        mid          = (bid + ask) / 2 if bid and ask else 0
        net_chg      = float(snap.get("netChange") or 0)
        net_chg_pct  = float(snap.get("percentageChange") or 0)
        high         = float(snap.get("high") or 0)
        low          = float(snap.get("low") or 0)
        market_state = snap.get("marketStatus", "UNKNOWN")

        knock_out = None
        for field in ["knockoutLevel", "limitLevel", "stopLevel"]:
            val = inst.get(field) or snap.get(field)
            if val:
                try:
                    knock_out = float(val)
                    break
                except (TypeError, ValueError):
                    pass

        barrier_distance_pct = None
        barrier_warning      = False
        if knock_out and mid:
            barrier_distance_pct = abs((mid - knock_out) / mid) * 100
            barrier_warning      = barrier_distance_pct < 5.0

        result = {
            "epic": epic, "bid": bid, "ask": ask, "mid": mid,
            "net_chg": net_chg, "net_chg_pct": net_chg_pct,
            "high": high, "low": low, "market_state": market_state,
            "knock_out_level": knock_out,
            "barrier_distance_pct": barrier_distance_pct,
            "barrier_warning": barrier_warning,
        }
        log.info(
            f"IG Brent: mid={mid:.2f} chg={net_chg_pct:+.2f}% state={market_state}"
            + (f" ⚠️ barrier {barrier_distance_pct:.1f}% away" if barrier_warning else "")
        )
        return result
    except Exception as e:
        lvl = log.debug if "500" in str(e) else log.warning
        lvl(f"IG REST snapshot failed: {e}")
        reset_session()
        return None


def format_ig_block(ig: dict) -> str:
    if not ig:
        return ""
    arrow      = "📈" if ig["net_chg_pct"] >= 0 else "📉"
    state_icon = "🟢" if ig["market_state"] == "TRADEABLE" else "🔴"
    lines = [
        "",
        f"🛢 <b>Brent (IG live)</b> {state_icon}",
        f"  Bid {ig['bid']:.2f} / Ask {ig['ask']:.2f}  {arrow} {ig['net_chg_pct']:+.2f}% today",
        f"  Range: {ig['low']:.2f} – {ig['high']:.2f}",
    ]
    if ig.get("knock_out_level"):
        lines.append(
            f"  Knock-out barrier: <b>{ig['knock_out_level']:.2f}</b>  "
            f"({ig['barrier_distance_pct']:.1f}% away)"
        )
        if ig["barrier_warning"]:
            lines.append("  ⚠️ <b>BARRIER PROXIMITY ALERT — within 5%</b>")
    return "\n".join(lines)