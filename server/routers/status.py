"""Status, settings, and live spectrum endpoints."""
import json
from typing import Any

from fastapi import APIRouter

from config import RECEIVER_NAME
from db import connect, get_settings, rows_to_dict, set_settings
from models import SettingsUpdate

router = APIRouter()


@router.get("/api/status")
def status() -> dict[str, Any]:
    con = connect()
    settings = get_settings()
    # In scan = channel enabled AND its bank active. The bank toggle flips active_categories
    # (soft, remembers per-channel enabled); the per-channel toggle flips enabled.
    active = settings.get("active_categories") or []
    if active:
        ph = ",".join("?" for _ in active)
        active_scan = con.execute(
            f"select count(*) from channels where enabled=1 and system in ({ph})", active).fetchone()[0]
    else:
        active_scan = 0
    data = {
        "receiver": RECEIVER_NAME,
        "settings": settings,
        "channels": con.execute("select count(*) from channels").fetchone()[0],
        "active_scan_channels": active_scan,
        "recordings": con.execute("select count(*) from recordings").fetchone()[0],
        "last_recording": dict(con.execute(
            "select * from recordings order by started_at desc limit 1").fetchone() or {}),
        "agent": dict(con.execute(
            "select * from agent_status order by updated_at desc limit 1").fetchone() or {}),
        "categories": rows_to_dict(con.execute(
            "select system category, count(*) channels from channels where enabled=1 "
            "group by system order by system")),
    }
    con.close()
    return data


@router.get("/api/level")
def level() -> dict[str, Any]:
    """Lightweight endpoint for the real-time S-meter: strongest current signal (dB over floor),
    polled fast by the UI so the needle moves live without rebuilding the whole scan list."""
    con = connect()
    a = con.execute("select levels, current_channel_id, current_name, current_frequency_mhz, recording, "
                    "live_db, channels_per_second, active from agent_status order by updated_at desc limit 1").fetchone()
    con.close()
    if not a:
        return {"level_db": None, "active": False}
    levels = {}
    if a["levels"]:
        try:
            levels = json.loads(a["levels"])
        except (json.JSONDecodeError, TypeError):
            levels = {}
    rec = bool(a["recording"])
    # While recording: the LIVE audio level (moves with the voice). While scanning: the channel
    # the receiver is ON — NOT the global max, so an unrelated steady carrier can't pin it high.
    cur = levels.get(str(a["current_channel_id"])) if a["current_channel_id"] is not None else None
    val = a["live_db"] if (rec and a["live_db"] is not None) else cur
    return {"level_db": val, "recording": rec, "name": a["current_name"],
            "freq": a["current_frequency_mhz"], "cps": a["channels_per_second"], "active": bool(a["active"])}


@router.get("/api/scan")
def scan() -> dict[str, Any]:
    """The live scan view: the channels currently in the scan set, each with its
    latest signal level and state. This is the scanner's natural display."""
    settings = get_settings()
    margin = float(settings.get("detection_margin_db", 8))
    con = connect()
    agent = con.execute("select * from agent_status order by updated_at desc limit 1").fetchone()
    levels = {}
    if agent and agent["levels"]:
        try:
            levels = json.loads(agent["levels"])
        except (json.JSONDecodeError, TypeError):
            levels = {}
    cur_id = agent["current_channel_id"] if agent else None
    recording = bool(agent["recording"]) if agent else False
    # valid VOICE recordings per channel (+ last time each fired) -> spot dead/very-active channels
    rec_counts, last_rec = {}, {}
    for row in con.execute(
            "select channel_id, count(*) n, max(started_at) last from recordings "
            "where channel_id is not null and (kind is null or kind='voice') group by channel_id"):
        rec_counts[row["channel_id"]] = row["n"]
        last_rec[row["channel_id"]] = row["last"]
    # no-voice rejects per channel (by reason) -> spot channels that trigger but never yield voice
    fail_counts, fail_by = {}, {}
    for row in con.execute("select channel_id, reason, count from channel_rejects"):
        cid = row["channel_id"]
        fail_counts[cid] = fail_counts.get(cid, 0) + row["count"]
        fail_by.setdefault(cid, {})[row["reason"]] = row["count"]
    active = settings.get("active_categories") or []
    channels = []
    if active:
        ph = ",".join("?" for _ in active)
        rows = rows_to_dict(con.execute(
            f"select id, frequency_mhz, name, system, group_name, mode from channels "
            f"where enabled=1 and system in ({ph}) order by frequency_hz", active))
    else:
        rows = []
    for r in rows:
        lvl = levels.get(str(r["id"]))
        state = "idle"
        if recording and cur_id == r["id"]:
            state = "rec"
        elif lvl is not None and lvl >= margin:
            state = "active"
        r["level_db"] = lvl
        r["state"] = state
        r["rec_count"] = rec_counts.get(r["id"], 0)
        r["fail_count"] = fail_counts.get(r["id"], 0)
        r["fail_by"] = fail_by.get(r["id"], {})
        r["last_rec"] = last_rec.get(r["id"])
        channels.append(r)
    con.close()
    return {
        "scanning": len(channels),
        "cps": float(agent["channels_per_second"]) if agent else 0.0,
        "active": bool(agent["active"]) if agent else False,
        "recording": recording,
        "current_channel_id": cur_id,
        "current_frequency_mhz": agent["current_frequency_mhz"] if agent else None,
        "margin_db": margin,
        "levels_at": agent["levels_at"] if agent else None,
        "channels": channels,
    }


@router.get("/api/settings")
def api_settings() -> dict[str, Any]:
    return get_settings()


@router.post("/api/settings")
def api_update_settings(update: SettingsUpdate) -> dict[str, Any]:
    return set_settings(update.model_dump(exclude_unset=True))
