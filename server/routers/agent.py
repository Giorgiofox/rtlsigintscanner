"""Agent (Raspberry) endpoints: config pull, heartbeat (incl. live spectrum)."""
import json
from typing import Any

from fastapi import APIRouter

from db import connect, get_settings, now_iso, rows_to_dict
from models import AgentHeartbeat

router = APIRouter()


@router.get("/api/agent/config")
def agent_config() -> dict[str, Any]:
    settings = get_settings()
    categories = settings.get("active_categories") or []
    con = connect()
    # auto re-include carriers the agent excluded longer ago than the cooldown (0 = never).
    # Protects a voice channel that was briefly mistaken for a dead carrier from staying off forever.
    cooldown = int(settings.get("carrier_cooldown_min") or 0)
    if cooldown > 0:
        import datetime as _dt
        cutoff = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=cooldown)) \
            .replace(microsecond=0).isoformat().replace("+00:00", "Z")
        con.execute(
            "update channels set enabled=1, excluded_reason=null, excluded_at=null "
            "where excluded_reason='continuous_carrier' and excluded_at is not null and excluded_at < ?",
            (cutoff,))
    ph = ",".join("?" for _ in categories) or "''"
    rows = rows_to_dict(con.execute(
        f"""select id, frequency_hz, frequency_mhz, name, system, group_name, category,
               mode, priority, tone_hz, attenuation, transcription_language
        from channels where enabled=1 and system in ({ph})
        order by priority desc, system, group_name, frequency_hz""",
        categories)) if categories else []
    con.close()
    return {"settings": settings, "channels": rows, "bands": []}


@router.post("/api/agent/exclude")
def agent_exclude(payload: dict) -> dict[str, Any]:
    """Agent reports a channel stuck on a continuous carrier -> drop it from the scan."""
    channel_id = payload.get("channel_id")
    reason = payload.get("reason", "continuous_carrier")
    if not channel_id:
        return {"ok": False}
    con = connect()
    con.execute("update channels set enabled=0, excluded_reason=?, excluded_at=? where id=?",
                (reason, now_iso(), int(channel_id)))
    con.close()
    return {"ok": True}


@router.post("/api/agent/reject")
def agent_reject(payload: dict) -> dict[str, Any]:
    """Agent reports a candidate that did NOT become a voice recording (flat carrier,
    below squelch, too short, wrong tone). Tally per channel+reason so the UI can flag
    'this channel triggers a lot but never yields voice' — the user then decides to exclude."""
    cid = payload.get("channel_id")
    reason = (payload.get("reason") or "other").split("(")[0].strip()[:24] or "other"
    if not cid:
        return {"ok": False}
    con = connect()
    con.execute(
        """insert into channel_rejects(channel_id, reason, count, updated_at) values(?,?,1,?)
           on conflict(channel_id, reason) do update set count=count+1, updated_at=excluded.updated_at""",
        (int(cid), reason, now_iso()))
    con.close()
    return {"ok": True}


@router.post("/api/agent/heartbeat")
def agent_heartbeat(payload: AgentHeartbeat):
    lv = payload.levels
    # Per-channel signal levels accumulate across a sweep; merge with stored ones so the
    # UI channel list stays populated between band sweeps. Preserve on level-less beats.
    con = connect()
    stored = {}
    row = con.execute("select levels, levels_at from agent_status where scanner_id=?",
                      (payload.scanner_id,)).fetchone()
    if row and row["levels"]:
        try:
            stored = json.loads(row["levels"])
        except (json.JSONDecodeError, TypeError):
            stored = {}
    if lv:
        stored.update({str(k): v for k, v in lv.items()})
    con.execute(
        """insert into agent_status(
          scanner_id, updated_at, scanned_total, channels_per_second, active, recording,
          current_channel_id, current_frequency_mhz, current_name, recordings_uploaded, last_error,
          levels, levels_at, live_db)
        values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        on conflict(scanner_id) do update set
          updated_at=excluded.updated_at, scanned_total=excluded.scanned_total,
          channels_per_second=excluded.channels_per_second, active=excluded.active,
          recording=excluded.recording, current_channel_id=excluded.current_channel_id,
          current_frequency_mhz=excluded.current_frequency_mhz,
          current_name=excluded.current_name, recordings_uploaded=excluded.recordings_uploaded,
          last_error=excluded.last_error, levels=excluded.levels, levels_at=excluded.levels_at,
          live_db=excluded.live_db""",
        (payload.scanner_id, now_iso(), payload.scanned_total, payload.channels_per_second,
         1 if payload.active else 0, 1 if payload.recording else 0,
         payload.current_channel_id, payload.current_frequency_mhz,
         payload.current_name, payload.recordings_uploaded, payload.last_error,
         json.dumps(stored) if stored else None, now_iso() if lv else (row["levels_at"] if row else None),
         payload.live_db))
    con.close()
    return {"ok": True}


@router.get("/api/agent/status")
def api_agent_status():
    con = connect()
    rows = rows_to_dict(con.execute("select * from agent_status order by updated_at desc"))
    con.close()
    return rows
