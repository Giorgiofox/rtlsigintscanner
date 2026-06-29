"""Pydantic request models."""
from pydantic import BaseModel


class SettingsUpdate(BaseModel):
    enabled: bool | None = None
    active_categories: list[str] | None = None
    sample_rate: int | None = None
    nfm_rate: int | None = None
    am_rate: int | None = None
    voice_zcr_max: float | None = None
    gain: str | None = None
    per_band_gain: dict[str, str] | None = None
    ppm: int | None = None
    settle_ms: int | None = None
    dwell_ms: int | None = None
    open_threshold: float | None = None
    close_threshold: float | None = None
    min_record_seconds: float | None = None
    max_record_seconds: float | None = None
    keep_rejected: bool | None = None
    keep_rejected_max: int | None = None
    show_smeter: bool | None = None
    silence_release_seconds: float | None = None
    close_rel: float | None = None
    tail_seconds: float | None = None
    trim_silence: bool | None = None
    trim_preroll_seconds: float | None = None
    eval_seconds: float | None = None
    eval_min_dyn: float | None = None
    min_dynamic_ratio: float | None = None
    am_min_dynamic_ratio: float | None = None
    min_voiced_frac: float | None = None
    detection_margin_db: float | None = None
    rtl_squelch: int | None = None
    scanner_device: str | None = None
    engine: str | None = None
    carrier_auto_exclude: bool | None = None
    carrier_block_seconds: int | None = None
    carrier_cooldown_min: int | None = None
    flat_abort_seconds: float | None = None
    retention_days: int | None = None


class AgentHeartbeat(BaseModel):
    scanner_id: str = "raspi"
    scanned_total: int = 0
    channels_per_second: float = 0.0
    active: bool = False
    recording: bool = False
    current_channel_id: int | None = None
    current_frequency_mhz: str | None = None
    current_name: str | None = None
    recordings_uploaded: int = 0
    last_error: str | None = None
    levels: dict[str, float] | None = None   # {channel_id: signal_delta_dB}
    live_db: float | None = None             # live audio level while recording (for the S-meter)


class ChannelUpdate(BaseModel):
    enabled: bool | None = None
    frequency_mhz: str | None = None
    name: str | None = None
    system: str | None = None
    group_name: str | None = None
    mode: str | None = None
    priority: int | None = None
    tone_hz: float | None = None
    attenuation: int | None = None
    scan_exclude: bool | None = None
    transcription_language: str | None = None


class ChannelCreate(BaseModel):
    frequency_mhz: str
    name: str = ""
    system: str = "DISCOVERED"
    group_name: str = ""
    mode: str = "NFM"
    enabled: bool = True
    priority: int = 0


class BulkChannels(BaseModel):
    category: str | None = None
    q: str | None = None
    set_enabled: bool | None = None
    set_scan_exclude: bool | None = None


class CategoryRename(BaseModel):
    old_system: str
    new_system: str
    old_group_name: str | None = None
    new_group_name: str | None = None


class UploadPayload(BaseModel):
    channel_id: int
    frequency_hz: int
    frequency_mhz: str | None = None
    name: str | None = None
    system: str | None = None
    group: str | None = None
    category: str | None = None
    mode: str | None = None
    started_at: str
    duration_seconds: float
    sample_rate: int
    rms: float | None = None
    peak: float | None = None
    audio_wav_base64: str
    kind: str | None = None          # 'voice' (default) or 'novoice' (rejected, kept for review)
    reject_reason: str | None = None # why the voice gate rejected it (flat / below_open / ...)
