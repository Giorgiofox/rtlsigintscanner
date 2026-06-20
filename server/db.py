"""SQLite access, schema init, lightweight migrations, channel import."""
import csv
import datetime as dt
import json
import sqlite3
from typing import Any

from config import (AVIATION_SYSTEMS, CHANNELS_CSV, CONFIG_DIR, DATA_DIR, DB_PATH,
                    DEFAULT_SETTINGS, RECORDINGS_DIR)

SCHEMA = """
create table if not exists channels (
  id integer primary key,
  enabled integer not null,
  frequency_hz integer not null,
  frequency_mhz text not null,
  name text,
  system text,
  group_name text,
  category text,
  mode text,
  priority integer default 0,
  tone_hz real,
  attenuation integer default 0,
  scan_exclude integer default 0,
  transcription_language text default 'it',
  raw_json text not null default '{}'
);
create index if not exists idx_channels_system on channels(system);
create index if not exists idx_channels_freq on channels(frequency_hz);

create table if not exists settings (key text primary key, value text not null);

create table if not exists agent_status (
  scanner_id text primary key,
  updated_at text not null,
  scanned_total integer not null default 0,
  channels_per_second real not null default 0,
  active integer not null default 0,
  recording integer not null default 0,
  current_channel_id integer,
  current_frequency_mhz text,
  current_name text,
  recordings_uploaded integer not null default 0,
  last_error text,
  levels text,
  levels_at text,
  spectrum_at text,
  spectrum_lo_hz integer,
  spectrum_hi_hz integer,
  spectrum_dbm text
);

create table if not exists recordings (
  id text primary key,
  channel_id integer,
  frequency_hz integer not null,
  frequency_mhz text,
  name text,
  system text,
  group_name text,
  category text,
  mode text,
  started_at text not null,
  received_at text not null,
  duration_seconds real not null,
  sample_rate integer not null,
  rms real,
  peak real,
  file_path text not null,
  sha256 text not null,
  transcription_status text default 'queued',
  transcription_language text,
  transcript text,
  transcript_json text
);
create index if not exists idx_recordings_started on recordings(started_at desc);
create index if not exists idx_recordings_category on recordings(category, started_at desc);
create index if not exists idx_recordings_tstatus on recordings(transcription_status);

create table if not exists band_spectrum (
  lo_hz integer primary key,
  hi_hz integer not null,
  dbm text not null,
  floor real,
  updated_at text not null
);

create table if not exists bandplan (
  id integer primary key autoincrement,
  group_name text not null,
  name text not null,
  lo_hz integer not null,
  hi_hz integer not null,
  step_hz integer not null,
  mode text not null,
  enabled integer not null default 0,
  note text,
  unique(lo_hz, hi_hz)
);

create table if not exists channel_rejects (
  channel_id integer not null,
  reason text not null,
  count integer not null default 0,
  updated_at text not null,
  primary key (channel_id, reason)
);

create table if not exists discovered_signals (
  id integer primary key autoincrement,
  frequency_hz integer not null unique,
  frequency_mhz text not null,
  hits integer not null default 1,
  occupancy real default 0,
  dbm_avg real,
  first_seen text not null,
  last_seen text not null,
  promoted integer not null default 0
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("pragma journal_mode=WAL")
    con.execute("pragma synchronous=NORMAL")
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Add columns introduced after first release (idempotent)."""
    wanted = {
        "channels": {"tone_hz": "real", "attenuation": "integer default 0",
                     "scan_exclude": "integer default 0",
                     "excluded_reason": "text", "excluded_at": "text",
                     "band_group": "text"},
        "recordings": {"transcript_confidence": "real",
                       "kind": "text default 'voice'", "reject_reason": "text",
                       "tx_mode": "text"},
        "agent_status": {
            "spectrum_at": "text", "spectrum_lo_hz": "integer",
            "spectrum_hi_hz": "integer", "spectrum_dbm": "text",
            "recording": "integer default 0",
            "levels": "text", "levels_at": "text", "live_db": "real",
        },
    }
    for table, cols in wanted.items():
        existing = {r["name"] for r in con.execute(f"pragma table_info({table})")}
        for col, decl in cols.items():
            if col not in existing:
                con.execute(f"alter table {table} add column {col} {decl}")


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_mode(mode: str, system: str = "") -> str:
    mode = (mode or "AUTO").upper().strip()
    if mode == "AUTO":
        return "AM" if system in AVIATION_SYSTEMS else "NFM"
    if mode == "FM" and system not in AVIATION_SYSTEMS:
        return "NFM"
    return mode


def init_db() -> None:
    con = connect()
    con.executescript(SCHEMA)
    _migrate(con)
    for key, value in DEFAULT_SETTINGS.items():
        con.execute("insert or ignore into settings(key,value) values(?,?)", (key, json.dumps(value)))
    con.close()
    seed_bandplan()


def seed_bandplan() -> None:
    """Load the Italian utility band plan from config/bandplan_it.csv (idempotent)."""
    path = CONFIG_DIR / "bandplan_it.csv"
    if not path.exists():
        return
    con = connect()
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                lo = int(round(float(row["lo_mhz"]) * 1_000_000))
                hi = int(round(float(row["hi_mhz"]) * 1_000_000))
                step = int(round(float(row["step_khz"]) * 1000))
            except (ValueError, KeyError):
                continue
            con.execute(
                """insert into bandplan(group_name, name, lo_hz, hi_hz, step_hz, mode, enabled, note)
                   values(?,?,?,?,?,?,?,?)
                   on conflict(lo_hz, hi_hz) do nothing""",
                (row.get("group", "").strip(), row.get("name", "").strip(), lo, hi, step,
                 (row.get("mode") or "NFM").strip().upper(), int(row.get("default_on") or 0),
                 row.get("note", "").strip()))
    con.close()


def import_channels() -> None:
    if not CHANNELS_CSV.exists():
        return
    con = connect()
    if con.execute("select count(*) from channels").fetchone()[0]:
        con.close()
        return
    with CHANNELS_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            system = (row.get("system") or "").strip()
            language = "en" if system in AVIATION_SYSTEMS else "it"
            con.execute(
                """insert or replace into channels(
                  id, enabled, frequency_hz, frequency_mhz, name, system, group_name,
                  category, mode, priority, attenuation, transcription_language, raw_json
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    int(row["id"]), int(row.get("enabled") or 0), int(row["frequency_hz"]),
                    row["frequency_mhz"], row.get("name") or "", system, row.get("group") or "",
                    row.get("category") or "", normalize_mode(row.get("mode") or "AUTO", system),
                    int(row.get("priority") or 0), int(row.get("attenuation") or 0), language,
                    json.dumps(row, ensure_ascii=False),
                ),
            )
    con.close()


def get_settings() -> dict[str, Any]:
    con = connect()
    rows = con.execute("select key,value from settings").fetchall()
    con.close()
    settings = {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in DEFAULT_SETTINGS.items()}
    for row in rows:
        try:
            settings[row["key"]] = json.loads(row["value"])
        except json.JSONDecodeError:
            settings[row["key"]] = row["value"]
    return settings


def set_settings(update: dict[str, Any]) -> dict[str, Any]:
    con = connect()
    for key, value in update.items():
        if value is not None and key in DEFAULT_SETTINGS:
            con.execute("insert or replace into settings(key,value) values(?,?)", (key, json.dumps(value)))
    con.close()
    return get_settings()


def rows_to_dict(rows) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]
