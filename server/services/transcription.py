"""Background transcription worker (faster-whisper, CPU).

Polls recordings with transcription_status='queued', transcribes them, and stores
the text. Language comes from the recording (en for aviation, it otherwise). The
model is lazy-loaded on first job so the app starts instantly and works even if
faster-whisper / the model are unavailable (jobs just stay queued).
"""
import json
import os
import threading
import time
from pathlib import Path

from db import connect

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "medium")
MODEL_DIR = os.environ.get("WHISPER_MODEL_DIR", "/data/models")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE", "int8")
ENABLED = os.environ.get("WHISPER_ENABLED", "1") not in ("0", "false", "")
POLL_SECONDS = float(os.environ.get("WHISPER_POLL_SECONDS", "5"))

_model = None
_model_lock = threading.Lock()
_stop = threading.Event()


def _load_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel  # imported lazily
            Path(MODEL_DIR).mkdir(parents=True, exist_ok=True)
            _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type=COMPUTE_TYPE,
                                  download_root=MODEL_DIR)
            print(f"[whisper] model {MODEL_SIZE} loaded ({COMPUTE_TYPE})", flush=True)
    return _model


def _claim_job():
    con = connect()
    row = con.execute(
        "select id, file_path, transcription_language, tx_mode from recordings "
        "where transcription_status='queued' order by started_at asc limit 1").fetchone()
    if row:
        con.execute("update recordings set transcription_status='running' where id=?", (row["id"],))
    con.close()
    return row


def _transcribe(row):
    model = _load_model()
    language = (row["transcription_language"] or "it").lower()
    if language not in ("it", "en"):
        language = None
    import math
    # permissive (re-transcribe) drops the VAD filter + previous-text conditioning so the WHOLE
    # clip is transcribed (recovers cut/partial transcripts) at the cost of more noise-as-words.
    permissive = (row["tx_mode"] == "full")
    segs = list(model.transcribe(
        row["file_path"], language=language, beam_size=5,
        vad_filter=(not permissive), condition_on_previous_text=(not permissive))[0])
    parts = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs]
    text = " ".join(p["text"] for p in parts).strip()
    # Confidence: duration-weighted exp(avg token log-prob) discounted by no-speech prob.
    # Noisy/garbled audio -> low avg_logprob / high no_speech_prob -> low score (0..1).
    if segs:
        tot = sum((s.end - s.start) for s in segs) or 1.0
        alp = sum((s.avg_logprob if s.avg_logprob is not None else -3.0) * (s.end - s.start) for s in segs) / tot
        nsp = sum((s.no_speech_prob or 0.0) * (s.end - s.start) for s in segs) / tot
        conf = max(0.0, min(1.0, math.exp(alp) * (1.0 - nsp)))
    else:
        conf = 0.0
    con = connect()
    con.execute(
        "update recordings set transcription_status='done', transcript=?, transcript_json=?, "
        "transcript_confidence=? where id=?",
        (text, json.dumps(parts, ensure_ascii=False), round(conf, 3), row["id"]))
    con.close()
    print(f"[whisper] {row['id']} -> {len(text)} chars conf={conf:.2f}", flush=True)


def _worker():
    print(f"[whisper] worker started (model={MODEL_SIZE}, enabled={ENABLED})", flush=True)
    # recover jobs orphaned 'running' by a restart mid-transcription
    con = connect()
    n = con.execute("update recordings set transcription_status='queued' where transcription_status='running'").rowcount
    con.close()
    if n:
        print(f"[whisper] requeued {n} orphaned running jobs", flush=True)
    while not _stop.is_set():
        if not ENABLED:
            time.sleep(POLL_SECONDS)
            continue
        row = _claim_job()
        if not row:
            time.sleep(POLL_SECONDS)
            continue
        try:
            _transcribe(row)
        except Exception as exc:
            print(f"[whisper] error on {row['id']}: {exc}", flush=True)
            con = connect()
            con.execute("update recordings set transcription_status='error', transcript=? where id=?",
                        (f"[error] {exc}"[:500], row["id"]))
            con.close()
            time.sleep(2)


def start_worker():
    t = threading.Thread(target=_worker, name="whisper-worker", daemon=True)
    t.start()
    return t


def stop_worker():
    _stop.set()
