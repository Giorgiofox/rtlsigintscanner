"""Static configuration and defaults for the RTL SIGINT Scanner backend."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
DB_PATH = DATA_DIR / "scanner.sqlite"
RECORDINGS_DIR = DATA_DIR / "recordings"
CHANNELS_CSV = CONFIG_DIR / "channels.csv"
RECEIVER_NAME = os.environ.get("RECEIVER_NAME", "RTL SIGINT SCANNER")

AVIATION_SYSTEMS = {"AIR CIVILE", "AIR MILITARE"}

# Scanner/agent + UI settings. Persisted in the `settings` table; the agent reads
# them via /api/agent/config. Per-band gain: "" means auto (best for weak air AM).
DEFAULT_SETTINGS = {
    "enabled": False,
    "active_categories": ["AIR CIVILE"],
    "sample_rate": 24000,
    "nfm_rate": 16000,                # NBFM demod/output rate (narrower = less hiss)
    "am_rate": 24000,                 # AM aviation demod/output rate
    "voice_zcr_max": 0.30,            # reject FM audio above this zero-crossing rate (noise)
    "gain": "",                       # global fallback gain ("" = auto)
    "per_band_gain": {"air": "", "vhf_low": "32.8", "default": "28.0"},
    "ppm": 0,
    "settle_ms": 120,
    "dwell_ms": 350,
    "open_threshold": 0.05,
    "close_threshold": 0.035,
    "min_record_seconds": 1.2,
    "max_record_seconds": 60,
    "keep_rejected": False,            # keep 'no-voice' rejected clips so you can review WHY a channel fails
    "keep_rejected_max": 5,            # ...keeping only the last N per channel (rolling)
    "show_smeter": True,               # show the analog S-meter above Live activity (UI only)
    "silence_release_seconds": 3.0,
    "close_rel": 0.3,                  # gap = audio below this fraction of the voice peak (closes over a carrier)
    "tail_seconds": 3.0,
    "trim_silence": True,              # trim dead carrier/noise off head & tail (keeps tail_seconds of tail)
    "trim_preroll_seconds": 0.3,       # audio kept just before the first voice
    "eval_seconds": 1.5,
    "eval_min_dyn": 1.5,
    "min_dynamic_ratio": 2.6,
    "am_min_dynamic_ratio": 1.4,       # weaker voice-vs-carrier gate for AM aviation (weak ATC is low-dynamic)
    "min_voiced_frac": 0.06,
    "detection_margin_db": 8,
    "rtl_squelch": 0,
    "scanner_device": "0",             # RTL-SDR serial; "0" = first device (set per dongle)
    # --- 2.0 ---
    "engine": "search",                # "search" (Pi3) | "channelizer" (Pi4)
    "carrier_auto_exclude": False,     # DANGER: time-based version wrongly excluded busy voice channels; keep OFF
    "carrier_block_seconds": 20,       # carrier must persist this long to be excluded
    "carrier_cooldown_min": 60,        # auto re-include after N min (0 = never)
    "flat_abort_seconds": 3.0,         # abort recording a flat carrier after N s
}
