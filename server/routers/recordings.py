"""Recording archive: list, audio stream, delete, agent upload, transcription trigger."""
import base64
import hashlib
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from config import AVIATION_SYSTEMS, RECORDINGS_DIR
from db import connect, get_settings, now_iso, rows_to_dict
from models import UploadPayload

router = APIRouter()


@router.get("/api/recordings")
def recordings(category: str | None = None, mode: str | None = None,
               channel_id: int | None = None, status: str | None = None,
               since: str | None = None, until: str | None = None,
               q: str | None = None, kind: str | None = None,
               limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 2000))
    where, params = [], []
    if kind:
        where.append("kind=?"); params.append(kind)
    else:
        where.append("(kind is null or kind='voice')")   # main feed = real recordings only
    if category:
        where.append("system=?"); params.append(category)
    if mode:
        where.append("upper(mode)=?"); params.append(mode.upper())
    if channel_id:
        where.append("channel_id=?"); params.append(channel_id)
    if status:
        where.append("transcription_status=?"); params.append(status)
    if since:
        where.append("started_at>=?"); params.append(since)
    if until:
        where.append("started_at<=?"); params.append(until)
    if q:
        where.append("(name like ? or transcript like ? or category like ? or frequency_mhz like ?)")
        params += [f"%{q}%"] * 4
    sql = "select * from recordings"
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by started_at desc limit ?"
    params.append(limit)
    con = connect()
    rows = rows_to_dict(con.execute(sql, params))
    con.close()
    for row in rows:
        row["audio_url"] = f"/api/recordings/{row['id']}/audio"
    return rows


@router.get("/api/recordings/usage")
def usage() -> dict[str, Any]:
    """Total disk used by recording WAVs (for the status bar) + voice/no-voice counts."""
    total, files = 0, 0
    for p in RECORDINGS_DIR.rglob("*.wav"):
        try:
            total += p.stat().st_size
            files += 1
        except OSError:
            pass
    con = connect()
    nv = con.execute("select count(*) from recordings where kind='novoice'").fetchone()[0]
    voice = con.execute("select count(*) from recordings where kind is null or kind='voice'").fetchone()[0]
    con.close()
    return {"bytes": total, "files": files, "voice": voice, "novoice": nv}


@router.post("/api/recordings/purge")
def purge(payload: dict) -> dict[str, Any]:
    """Bulk-delete recordings matching the SAME filters as the feed (the UI sends its current
    filter, so 'filter then delete' works). Deletes DB rows + the WAV files."""
    where, params = [], []
    if payload.get("kind"):
        where.append("kind=?"); params.append(payload["kind"])
    elif not payload.get("include_novoice"):
        where.append("(kind is null or kind='voice')")
    if payload.get("category"):
        where.append("system=?"); params.append(payload["category"])
    if payload.get("mode"):
        where.append("upper(mode)=?"); params.append(str(payload["mode"]).upper())
    if payload.get("since"):
        where.append("started_at>=?"); params.append(payload["since"])
    if payload.get("until"):
        where.append("started_at<=?"); params.append(payload["until"])
    if payload.get("q"):
        where.append("(name like ? or transcript like ? or category like ? or frequency_mhz like ?)")
        params += [f"%{payload['q']}%"] * 4
    sql = "select id, file_path from recordings"
    if where:
        sql += " where " + " and ".join(where)
    con = connect()
    rows = con.execute(sql, params).fetchall()
    for r in rows:
        con.execute("delete from recordings where id=?", (r["id"],))
        try:
            Path(r["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    con.close()
    return {"ok": True, "deleted": len(rows)}


@router.get("/api/recordings/{recording_id}/audio")
def recording_audio(recording_id: str):
    con = connect()
    row = con.execute("select file_path from recordings where id=?", (recording_id,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "recording not found")
    path = Path(row["file_path"])
    if not path.exists():
        raise HTTPException(404, "audio file missing")
    return FileResponse(path, media_type="audio/wav", filename=path.name)


@router.delete("/api/recordings/{recording_id}")
def delete_recording(recording_id: str):
    con = connect()
    row = con.execute("select file_path from recordings where id=?", (recording_id,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(404, "recording not found")
    path = Path(row["file_path"])
    con.execute("delete from recordings where id=?", (recording_id,))
    con.close()
    if path.exists():
        path.unlink()
    return {"ok": True}


@router.post("/api/recordings/{recording_id}/retranscribe")
def retranscribe(recording_id: str, permissive: bool = False):
    """Re-queue a recording for transcription. permissive=1 turns OFF the VAD filter and
    previous-text conditioning so a cut/partial transcript gets the WHOLE audio transcribed."""
    con = connect()
    cur = con.execute(
        "update recordings set transcription_status='queued', transcript=null, transcript_json=null, "
        "transcript_confidence=null, tx_mode=? where id=?",
        ("full" if permissive else None, recording_id))
    con.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "recording not found")
    return {"ok": True}


@router.post("/api/recordings/upload")
def upload(payload: UploadPayload):
    audio = base64.b64decode(payload.audio_wav_base64)
    if len(audio) < 512:
        raise HTTPException(400, "audio too small")
    recording_id = str(uuid.uuid4())
    started_safe = payload.started_at.replace(":", "").replace("-", "").replace("Z", "")
    system = payload.system or "UNKNOWN"
    folder = RECORDINGS_DIR / system.replace("/", "_").replace(" ", "_")
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{started_safe}_{payload.frequency_mhz or payload.frequency_hz}_{recording_id[:8]}.wav"
    path = folder / filename
    path.write_bytes(audio)
    digest = hashlib.sha256(audio).hexdigest()
    language = "en" if system in AVIATION_SYSTEMS else "it"
    kind = "novoice" if (payload.kind == "novoice") else "voice"
    tstatus = "skip" if kind == "novoice" else "queued"   # don't waste whisper on rejected clips
    con = connect()
    con.execute(
        """insert into recordings(
          id, channel_id, frequency_hz, frequency_mhz, name, system, group_name, category, mode,
          started_at, received_at, duration_seconds, sample_rate, rms, peak, file_path, sha256,
          transcription_status, transcription_language, kind, reject_reason
        ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (recording_id, payload.channel_id, payload.frequency_hz, payload.frequency_mhz, payload.name,
         payload.system, payload.group, payload.category, payload.mode, payload.started_at, now_iso(),
         payload.duration_seconds, payload.sample_rate, payload.rms, payload.peak, str(path), digest,
         tstatus, language, kind, payload.reject_reason))
    # rolling-prune: keep only the last N no-voice clips per channel (delete file + row)
    if kind == "novoice" and payload.channel_id:
        keep = int(get_settings().get("keep_rejected_max", 5) or 5)
        for row in con.execute(
                "select id, file_path from recordings where channel_id=? and kind='novoice' "
                "order by received_at desc limit -1 offset ?", (payload.channel_id, keep)).fetchall():
            con.execute("delete from recordings where id=?", (row["id"],))
            try:
                Path(row["file_path"]).unlink(missing_ok=True)
            except OSError:
                pass
    con.close()
    return {"ok": True, "id": recording_id, "sha256": digest}
