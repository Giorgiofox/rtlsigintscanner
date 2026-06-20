#!/usr/bin/env python3
"""WebScanner SDR agent (Raspberry Pi, RTL-SDR).

Detection via wide banded rtl_power sweeps (few processes, high ch/s), recording
via a single rtl_fm spawn per candidate with a clean demod chain. Self-heals the
dongle from the wedged-USB state via USBDEVFS_RESET. Stdlib only (Pi3-friendly).
"""
import base64
import datetime as dt
import fcntl
import glob
import json
import math
import os
import signal
import struct
import subprocess
import threading
import time
import urllib.request
import wave
from pathlib import Path

SERVER_URL = os.environ.get("SERVER_URL", "http://localhost:8096").rstrip("/")
DEVICE = os.environ.get("SCANNER_DEVICE", "0")  # RTL-SDR serial; "0" = first device
WORK_DIR = Path(os.environ.get("WORK_DIR", "/tmp/webscanner-agent"))
CONFIG_POLL_SECONDS = float(os.environ.get("CONFIG_POLL_SECONDS", "10"))

# Detection sweep tuning (overridable via env; ch/s critical).
DETECT_BIN_HZ = int(os.environ.get("DETECT_BIN_HZ", "12500"))
BAND_GAP_HZ = int(os.environ.get("BAND_GAP_HZ", "15000000"))   # channels closer than this share a sweep
BAND_MAX_SPAN_HZ = int(os.environ.get("BAND_MAX_SPAN_HZ", "48000000"))
BAND_GUARD_HZ = int(os.environ.get("BAND_GUARD_HZ", "400000"))
SWEEP_TIMEOUT_S = float(os.environ.get("SWEEP_TIMEOUT_S", "20"))
MAX_CANDIDATES_PER_SWEEP = int(os.environ.get("MAX_CANDIDATES_PER_SWEEP", "3"))
WATCHDOG_TIMEOUT_S = float(os.environ.get("WATCHDOG_TIMEOUT_S", "90"))

WORK_DIR.mkdir(parents=True, exist_ok=True)
AVIATION_SYSTEMS = {"AIR CIVILE", "AIR MILITARE"}
stop = False
_last_progress = 0.0   # touched whenever the scan loop makes progress; watched by the watchdog


def handle_stop(signum, frame):
    global stop
    stop = True


signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT, handle_stop)


def touch_progress():
    global _last_progress
    _last_progress = time.time()


def _watchdog():
    """If the scan loop makes no progress for WATCHDOG_TIMEOUT_S the device is wedged with a
    blocking read (the IQ read_samples / rtl_fm read have no timeout). Exit non-zero so systemd
    restarts us; startup runs usb_reset(force=True) and recovers. Prevents multi-hour dead hangs."""
    while not stop:
        time.sleep(15)
        if _last_progress and time.time() - _last_progress > WATCHDOG_TIMEOUT_S:
            log(f"WATCHDOG: no scan progress for {int(time.time() - _last_progress)}s "
                f"(device wedged?) -> exit(1) for systemd restart")
            os._exit(1)


def now_iso():
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- USB

_last_reset = 0.0
USB_RESET_MIN_INTERVAL = float(os.environ.get("USB_RESET_MIN_INTERVAL", "120"))


def usb_reset(force=False):
    """USBDEVFS_RESET the scanner dongle. Recovers the wedged-USB state where the
    device tunes/locks but never delivers samples. Works as plugdev user.

    Rate-limited: hammering resets destabilizes the Pi USB hub (EMI/brownout can
    drop ALL devices). At most one reset per USB_RESET_MIN_INTERVAL seconds."""
    global _last_reset
    if not force and (time.time() - _last_reset) < USB_RESET_MIN_INTERVAL:
        return False
    _last_reset = time.time()
    for d in glob.glob("/sys/bus/usb/devices/*"):
        sp = os.path.join(d, "serial")
        try:
            if not os.path.exists(sp) or open(sp).read().strip() != DEVICE:
                continue
            busnum = int(open(os.path.join(d, "busnum")).read())
            devnum = int(open(os.path.join(d, "devnum")).read())
            node = f"/dev/bus/usb/{busnum:03d}/{devnum:03d}"
            fd = os.open(node, os.O_WRONLY)
            try:
                fcntl.ioctl(fd, 0x5514, 0)  # USBDEVFS_RESET
                log(f"usb_reset {node}")
            finally:
                os.close(fd)
            time.sleep(1.5)
            return True
        except Exception as exc:
            log(f"usb_reset error: {exc}")
    return False


# ------------------------------------------------------------------------- HTTP

def http_json(path, payload=None, timeout=10):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["content-type"] = "application/json"
    req = urllib.request.Request(SERVER_URL + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode())


# ---------------------------------------------------------------- demod / rtl_fm

def rtl_fm_mode(mode):
    mode = (mode or "FM").upper()
    return {"AM": "am", "WFM": "wbfm", "USB": "usb", "LSB": "lsb"}.get(mode, "fm")


def band_gain(settings, frequency_hz, mode):
    """Per-band gain. Strong local NFM (VHF/UHF) clips at high gain; weak air AM
    benefits from auto. Returns a gain string ('' => auto)."""
    per_band = settings.get("per_band_gain") or {}
    mhz = frequency_hz / 1e6
    if (mode or "").upper() == "AM" or 118 <= mhz <= 137 or 225 <= mhz <= 400:
        key = "air"
    elif mhz < 110:
        key = "vhf_low"   # 73-79 VVFF/PS
    else:
        key = "default"
    if key in per_band:
        return str(per_band[key])
    return str(settings.get("gain", "") or "")


def build_rtl_fm_cmd(frequency_hz, mode, settings):
    demod = rtl_fm_mode(mode)
    gain = band_gain(settings, frequency_hz, mode)
    squelch = int(settings.get("rtl_squelch", 0) or 0)
    ppm = int(settings.get("ppm", 0) or 0)
    # Narrow NBFM demod (matches ~12.5 kHz channels) cuts adjacent splatter/hiss;
    # AM aviation keeps a wider passband. WFM uses the rtl_fm preset.
    if demod == "fm":
        out_rate = int(settings.get("nfm_rate", 16000))
    elif demod == "am":
        out_rate = int(settings.get("am_rate", 24000))
    else:
        out_rate = int(settings.get("sample_rate", 24000))
    cmd = ["rtl_fm", "-d", str(DEVICE), "-f", str(int(frequency_hz)), "-M", demod]
    if demod == "wbfm":
        cmd += ["-r", str(out_rate)]            # wbfm preset sets -s 170k internally
    else:
        cmd += ["-s", str(out_rate)]            # narrow demod at output rate
    cmd += ["-F", "9", "-A", "fast", "-E", "dc"]  # low-leakage FIR + fast atan + DC block
    if demod in ("wbfm", "fm"):
        cmd += ["-E", "deemp"]                  # de-emphasis tames FM high-freq hiss
    if gain:
        cmd += ["-g", gain]
    if squelch > 0:
        cmd += ["-l", str(squelch)]
    if ppm:
        cmd += ["-p", str(ppm)]
    return cmd, out_rate


def graceful_stop(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        usb_reset()   # a SIGKILL mid-stream can wedge the dongle; reset proactively


def read_pcm(proc, out_rate, seconds):
    wanted = int(out_rate * 2 * seconds)
    data = bytearray()
    deadline = time.time() + seconds + 2.0
    while len(data) < wanted and time.time() < deadline and not stop:
        chunk = proc.stdout.read(min(8192, wanted - len(data))) if proc.stdout else b""
        if not chunk:
            break
        data += chunk
    return bytes(data)


def pcm_stats(data):
    """Return (rms, peak, zcr). ZCR (zero-crossing rate) separates voice from
    open-squelch FM noise: voice ~0.05-0.20, white hiss ~0.35-0.60."""
    n = len(data) // 2
    if n < 1:
        return 0.0, 0.0, 0.0
    samples = struct.unpack("<%dh" % n, data[: n * 2])
    peak = max(abs(v) for v in samples) / 32768.0
    rms = math.sqrt(sum(v * v for v in samples) / n) / 32768.0
    crossings = sum(1 for i in range(1, n) if (samples[i - 1] < 0) != (samples[i] < 0))
    zcr = crossings / n
    return rms, peak, zcr


def _biquad(samples, b0, b1, b2, a1, a2):
    """Direct-form-I biquad (RBJ). Coeffs already normalized by a0."""
    x1 = x2 = y1 = y2 = 0.0
    out = []
    ap = out.append
    for x in samples:
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1 = x1, x
        y2, y1 = y1, y
        ap(y)
    return out


def _hpf_coeffs(fc, sr, q=0.707):
    w = 2 * math.pi * fc / sr
    cw, sw = math.cos(w), math.sin(w)
    alpha = sw / (2 * q)
    a0 = 1 + alpha
    return ((1 + cw) / 2 / a0, -(1 + cw) / a0, (1 + cw) / 2 / a0,
            (-2 * cw) / a0, (1 - alpha) / a0)


def _lpf_coeffs(fc, sr, q=0.707):
    w = 2 * math.pi * fc / sr
    cw, sw = math.cos(w), math.sin(w)
    alpha = sw / (2 * q)
    a0 = 1 + alpha
    return ((1 - cw) / 2 / a0, (1 - cw) / a0, (1 - cw) / 2 / a0,
            (-2 * cw) / a0, (1 - alpha) / a0)


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    i = max(0, min(len(sorted_vals) - 1, int(p * (len(sorted_vals) - 1))))
    return sorted_vals[i]


def process_audio(data, sample_rate, settings):
    """Clean up SDR voice: voice band-pass, then a frame-based noise gate that
    mutes the hiss between words/transmissions, then loudness normalization keyed
    to the voice level (not to a noise spike). This is what kills the "rumore"."""
    if not settings.get("audio_filter", True):
        return data
    n = len(data) // 2
    if n < sample_rate // 10:
        return data
    samples = [v / 32768.0 for v in struct.unpack("<%dh" % n, data[: n * 2])]
    hp = float(settings.get("audio_hpf_hz", 250))
    lp = min(float(settings.get("audio_lpf_hz", 3000)), sample_rate / 2 - 200)
    samples = _biquad(samples, *_hpf_coeffs(hp, sample_rate))
    samples = _biquad(samples, *_lpf_coeffs(lp, sample_rate))

    # frame RMS envelope
    fl = max(1, int(sample_rate * 0.02))           # 20 ms frames
    nf = (len(samples) + fl - 1) // fl
    frms = []
    for k in range(nf):
        seg = samples[k * fl:(k + 1) * fl]
        frms.append(math.sqrt(sum(v * v for v in seg) / len(seg)) if seg else 0.0)
    srt = sorted(frms)
    noise = max(_percentile(srt, 0.25), 1e-4)      # noise floor ~ quiet frames
    voice = max(_percentile(srt, 0.92), noise * 1.5)  # voice level ~ loud frames
    ratio = float(settings.get("audio_gate_ratio", 2.2))
    thr = noise * ratio

    # soft per-frame gate gain with attack/release smoothing
    target = [1.0 if frms[k] >= thr else max(0.0, (frms[k] / thr) ** 2) for k in range(nf)]
    smoothed = []
    g = 0.0
    for k in range(nf):
        a = 0.5 if target[k] > g else 0.2          # fast attack, slower release
        g = g + a * (target[k] - g)
        smoothed.append(g)
    # keep the last tail_seconds UNGATED so you hear the trailing radio noise/carrier
    # (scanner-style tail) instead of an abrupt cut.
    tail_s = float(settings.get("tail_seconds", 3.0))
    tail_frames = int(tail_s / 0.02)
    for k in range(max(0, nf - tail_frames), nf):
        smoothed[k] = 1.0

    # normalize to voice level, capped by max gain and by a peak ceiling (no clipping)
    norm = min(float(settings.get("audio_norm_level", 0.32)) / voice,
               float(settings.get("audio_max_gain", 15.0)))
    peak_s = max((abs(v) for v in samples), default=1.0)
    if peak_s > 1e-6:
        norm = min(norm, 0.95 / peak_s)
    out = bytearray()
    for i, v in enumerate(samples):
        gain = smoothed[i // fl] * norm
        out += struct.pack("<h", int(max(-1.0, min(1.0, v * gain)) * 32767))
    return bytes(out)


def voice_dynamics(data, sample_rate):
    """(loud/floor ratio, voiced fraction). Voice has speech bursts well above the
    noise floor; a steady carrier or open-squelch hiss is flat (ratio ~1, voiced ~0).
    This separates real transmissions from noise better than ZCR after de-emphasis."""
    n = len(data) // 2
    if n < sample_rate // 5:
        return 0.0, 0.0
    s = struct.unpack("<%dh" % n, data[: n * 2])
    fl = max(1, int(sample_rate * 0.03))
    fr = []
    for k in range(0, n, fl):
        seg = s[k:k + fl]
        if seg:
            fr.append(math.sqrt(sum(v * v for v in seg) / len(seg)) / 32768.0)
    if len(fr) < 3:
        return 0.0, 0.0
    srt = sorted(fr)
    floor = max(srt[len(srt) // 5], 1e-4)
    loud = srt[int(len(srt) * 0.9)]
    return loud / floor, sum(1 for v in fr if v > floor * 2.2) / len(fr)


def goertzel(samples, sample_rate, target_hz):
    """Single-frequency power (for CTCSS tone detection)."""
    n = len(samples)
    if n < sample_rate // 10:
        return 0.0
    k = int(0.5 + (n * target_hz) / sample_rate)
    w = 2.0 * math.pi * k / n
    coeff = 2.0 * math.cos(w)
    s_prev = s_prev2 = 0.0
    for x in samples:
        s = x + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s
    power = s_prev2 ** 2 + s_prev ** 2 - coeff * s_prev * s_prev2
    return power / n


def tone_present(data, sample_rate, tone_hz, tol=0.25):
    """True if a CTCSS tone near tone_hz dominates the sub-audible band."""
    n = len(data) // 2
    if n < sample_rate // 5 or not tone_hz:
        return True  # cannot judge -> don't block
    samples = [v / 32768.0 for v in struct.unpack("<%dh" % n, data[: n * 2])]
    target = goertzel(samples, sample_rate, tone_hz)
    # reference: a nearby off-tone frequency
    ref = goertzel(samples, sample_rate, tone_hz * 1.6 + 30)
    return target > max(ref * 3.0, 1e-9)


# --------------------------------------------------------------- detection sweep

def parse_power(output):
    bins = []
    for line in output.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue
        try:
            low = int(float(parts[2]))
            step = float(parts[4])
            values = [float(v) for v in parts[6:] if v]
        except ValueError:
            continue
        for i, dbm in enumerate(values):
            bins.append((low + int((i + 0.5) * step), dbm))
    return bins


def median(values):
    if not values:
        return -120.0
    s = sorted(values)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def build_bands(channels):
    ordered = sorted(channels, key=lambda c: int(c["frequency_hz"]))
    bands, cur, lo = [], [], None
    for ch in ordered:
        f = int(ch["frequency_hz"])
        if cur and (f - lo > BAND_GAP_HZ or f - int(cur[0]["frequency_hz"]) > BAND_MAX_SPAN_HZ):
            bands.append(cur)
            cur = []
        cur.append(ch)
        lo = f
    if cur:
        bands.append(cur)
    return bands


def sweep_band(band, settings):
    lo = min(int(c["frequency_hz"]) for c in band) - BAND_GUARD_HZ
    hi = max(int(c["frequency_hz"]) for c in band) + BAND_GUARD_HZ
    if hi - lo < 200000:
        hi = lo + 200000
    span = hi - lo
    # -i must exceed the full sweep time (~0.4s/hop) or rtl_power hangs.
    hops = max(1, math.ceil(span / 2_400_000))
    interval = max(2, math.ceil(hops * 0.3) + 1)  # ~0.2s/hop sweep + margin
    gain = settings.get("gain", "") or "30"
    ppm = int(settings.get("ppm", 0) or 0)
    cmd = ["rtl_power", "-d", str(DEVICE), "-g", str(gain),
           "-f", f"{lo}:{hi}:{DETECT_BIN_HZ}", "-w", "hann", "-i", str(interval), "-1", "-"]
    if ppm:
        cmd += ["-p", str(ppm)]
    started = time.time()
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=SWEEP_TIMEOUT_S)
        bins = parse_power(proc.stdout)
    except subprocess.TimeoutExpired:
        # hung sweep wedges the device: reset before returning
        subprocess.run(["pkill", "-9", "-f", f"rtl_power.*{DEVICE}"], stderr=subprocess.DEVNULL)
        usb_reset()
        return time.time() - started, [], None
    elapsed = time.time() - started
    if not bins:
        return elapsed, [], None
    # Noise floor = low percentile, NOT median: in a narrow/busy band the median sits on
    # top of carriers, hiding real signals (a +11 dB carrier looked like +0.5 dB). The 20th
    # percentile tracks the true noise floor regardless of how full the band is.
    ordered = sorted(d for _, d in bins)
    floor = ordered[max(0, int(len(ordered) * 0.20))]
    margin = float(settings.get("detection_margin_db", 8))
    candidates, levels = [], {}
    for ch in band:
        f = int(ch["frequency_hz"])
        near = min(bins, key=lambda b: abs(b[0] - f))
        if abs(near[0] - f) > DETECT_BIN_HZ:
            continue
        delta = near[1] - floor
        levels[int(ch["id"])] = round(delta, 1)   # signal level for the UI channel list
        if delta >= margin:
            e = dict(ch)
            e["power_dbm"] = near[1]
            e["power_delta_db"] = delta
            candidates.append(e)
    return elapsed, candidates, levels


# ----------------------------------------------------------------------- record

def upload_recording(channel, started_at, pcm, out_rate, rms, peak, kind="voice", reject_reason=None):
    duration = len(pcm) / 2 / out_rate
    path = WORK_DIR / "up.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(out_rate)
        w.writeframes(pcm)
    audio = path.read_bytes()
    path.unlink(missing_ok=True)
    payload = {
        "channel_id": int(channel["id"]),
        "frequency_hz": int(channel["frequency_hz"]),
        "frequency_mhz": channel.get("frequency_mhz"),
        "name": channel.get("name"),
        "system": channel.get("system"),
        "group": channel.get("group_name"),
        "category": channel.get("category"),
        "mode": channel.get("mode"),
        "started_at": started_at,
        "duration_seconds": duration,
        "sample_rate": out_rate,
        "rms": rms,
        "peak": peak,
        "audio_wav_base64": base64.b64encode(audio).decode(),
        "kind": kind,
        "reject_reason": reject_reason,
    }
    return http_json("/api/recordings/upload", payload=payload, timeout=30)


def report_reject(channel, settings, reason, pcm, out_rate, started=None):
    """A candidate did NOT become a voice recording. Always tally it (per-channel stats).
    If keep_rejected is on, also upload the captured audio as a 'no-voice' clip so the user
    can listen and judge WHY the channel fails (carrier? noise? real voice cut by the gate?)."""
    cid = int(channel.get("id") or 0)
    if cid:
        try:
            http_json("/api/agent/reject", {"channel_id": cid, "reason": reason}, timeout=4)
        except Exception:
            pass
    if settings.get("keep_rejected") and pcm and len(pcm) >= 1024:
        try:
            audio = process_audio(bytes(pcm), out_rate, settings)
            frms, fpeak, _ = pcm_stats(audio)
            upload_recording(channel, started or now_iso(), audio, out_rate, frms, fpeak,
                             kind="novoice", reject_reason=reason)
        except Exception as exc:
            log(f"  novoice upload error: {exc}")
    return False, reason


def trim_to_voice(pcm, out_rate, settings):
    """Trim leading/trailing non-voice from a capture so a recording does NOT begin or end
    with dead carrier/noise (the squelch can't close on a channel with a constant carrier, so
    captures run to max_record_seconds). We keep the audio from just before the first voiced
    frame to `tail_seconds` after the last one. Returns trimmed bytes, or None if no voice at all
    (pure carrier/noise -> reject). Frame energies vs the clip's OWN noise floor, so it adapts to
    weak signals and never loses the voice itself."""
    n = len(pcm) // 2
    fr = max(1, int(out_rate * 0.02))            # 20 ms frames
    if n < fr * 12:
        return pcm
    samples = struct.unpack(f"<{n}h", pcm[: n * 2])
    E = []
    for i in range(0, n - fr, fr):
        seg = samples[i:i + fr]
        E.append((sum(s * s for s in seg) / fr) ** 0.5 / 32768.0)
    m = len(E)
    if m < 12:
        return pcm
    # VOICE detection by local VARIABILITY, not absolute energy: a constant carrier (CC control
    # channel) is loud but STEADY -> low rolling std; speech modulates the audio -> high rolling
    # std. Energy thresholding fails on a loud carrier (everything is "loud"); variability doesn't.
    w = max(3, int(0.4 / 0.02))                  # ~0.4 s window
    var = [0.0] * m
    for i in range(m):
        a, b = max(0, i - w), min(m, i + w + 1)
        seg = E[a:b]
        mu = sum(seg) / len(seg)
        var[i] = (sum((x - mu) ** 2 for x in seg) / len(seg)) ** 0.5
    srt = sorted(var)
    vfloor = srt[int(m * 0.5)]                    # steady-carrier variability
    vpk = srt[int(m * 0.95)]                       # speech variability
    close_th = float(settings.get("close_threshold", 0.02))
    thr = vfloor + (vpk - vfloor) * 0.30
    voiced = [i for i in range(m) if var[i] >= thr and E[i] >= close_th]
    if not voiced:
        return None                              # steady carrier / noise only -> reject
    preroll = int(float(settings.get("trim_preroll_seconds", 0.3)) / 0.02)
    tail = int(float(settings.get("tail_seconds", 3.0)) / 0.02)
    a = max(0, voiced[0] - preroll)
    b = min(m, voiced[-1] + 1 + tail)
    return pcm[a * fr * 2: b * fr * 2]


def record_candidate(channel, settings, heartbeat=None):
    """Single rtl_fm spawn. Two phases:
    1. EVALUATE (~1.5s, not flagged as recording): confirm it's real voice via dynamics.
       Noise/carriers are rejected here fast — we do NOT park 30s on them.
    2. CAPTURE (flagged recording=True so the UI shows REC only for real voice): record
       until the transmission ends (silence) or max."""
    cmd, out_rate = build_rtl_fm_cmd(int(channel["frequency_hz"]), channel.get("mode"), settings)
    settle_ms = float(settings.get("settle_ms", 120))
    open_th = float(settings.get("open_threshold", 0.05))
    close_th = float(settings.get("close_threshold", 0.035))
    min_s = float(settings.get("min_record_seconds", 1.2))
    max_s = float(settings.get("max_record_seconds", 30))
    release_s = float(settings.get("silence_release_seconds", 1.5))
    eval_s = float(settings.get("eval_seconds", 1.5))
    eval_min_dyn = float(settings.get("eval_min_dyn", 1.5))   # eval: only reject FLAT carriers/noise
    # final voice gate; AM aviation voice is far less dynamic than FM (weak ATC ~1.5 vs FM voice >2),
    # so use a lower threshold for AM or weak air band gets rejected as flat.
    if (channel.get("mode") or "").upper() == "AM":
        min_dyn = float(settings.get("am_min_dynamic_ratio", 1.4))
    else:
        min_dyn = float(settings.get("min_dynamic_ratio", 2.6))
    min_vf = float(settings.get("min_voiced_frac", 0.06))
    tone_hz = float(channel.get("tone_hz") or 0)

    def beat(rec, lvl=None):
        if heartbeat:
            heartbeat(channel, rec, lvl)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        read_pcm(proc, out_rate, settle_ms / 1000.0)        # drop tuning transient
        # --- phase 1: evaluate (no REC flag) ---
        eval_pcm = read_pcm(proc, out_rate, eval_s)
        rms, peak, _ = pcm_stats(eval_pcm)
        if rms < open_th:
            return report_reject(channel, settings, "below_open", eval_pcm, out_rate)
        # eval gate is LOOSE: only reject a flat dead carrier / steady hiss fast. Real voice
        # (incl. AM ATC, low dynamics over a short window) passes; quality is judged at the end.
        eval_dyn, _ = voice_dynamics(eval_pcm, out_rate)
        if eval_dyn < eval_min_dyn:
            return report_reject(channel, settings, f"flat(dyn={eval_dyn:.1f})", eval_pcm, out_rate)
        if tone_hz and not tone_present(eval_pcm, out_rate, tone_hz):
            return report_reject(channel, settings, "tone_mismatch", eval_pcm, out_rate)

        # --- phase 2: capture (simple RMS squelch) ---
        # Record while audio is above close_th; stop after release_s of continuous silence
        # (those trailing seconds are the radio-noise tail). Quality judged once at the end.
        started = now_iso()
        pcm = bytearray(eval_pcm)
        silence = 0.0
        voiced_total = 0.0
        t0 = time.time()
        chunk_s = 0.25
        close_rel = float(settings.get("close_rel", 0.3))   # silence = below this fraction of the voice peak
        voice_peak = max(rms, open_th)                       # track loudest voice so gaps over a carrier count as silence
        while time.time() - t0 < max_s and not stop:
            chunk = read_pcm(proc, out_rate, chunk_s)
            if not chunk:
                break
            crms, cpeak, _ = pcm_stats(chunk)
            live_db = 20.0 * math.log10(max(crms, 1e-4) / max(open_th, 1e-4))
            beat(voiced_total >= 0.5, max(0.0, live_db))   # live audio level for the S-meter
            pcm.extend(chunk)
            peak = max(peak, cpeak)
            voice_peak = max(voice_peak, crms)
            # close on RELATIVE silence: a steady carrier never drops below an absolute close_th,
            # but it IS well below the voice peak, so gaps between transmissions now end the recording.
            close_level = max(close_th, voice_peak * close_rel)
            if crms < close_level:
                silence += chunk_s
            else:
                silence = 0.0
                voiced_total += chunk_s
                rms = max(rms, crms)
            if silence >= release_s:
                break
    finally:
        graceful_stop(proc)

    duration = len(pcm) / 2 / out_rate
    if duration < min_s:
        return report_reject(channel, settings, "too_short", pcm, out_rate, started)
    # trim dead carrier/noise off the head & tail (squelch can't close on a constant carrier,
    # so the capture ran long). Keep tail_seconds of radio tail after the last voice.
    if settings.get("trim_silence", True):
        trimmed = trim_to_voice(bytes(pcm), out_rate, settings)
        if trimmed is None:
            return report_reject(channel, settings, "flat_noise(no_voice)", pcm, out_rate, started)
        pcm = bytearray(trimmed)
        duration = len(pcm) / 2 / out_rate
        if duration < min_s:
            return report_reject(channel, settings, "too_short", pcm, out_rate, started)
    # final gate: reject a steady carrier / open-squelch hiss (flat). Real voice varies.
    dyn_ratio, _ = voice_dynamics(bytes(pcm), out_rate)
    if dyn_ratio < min_dyn:
        return report_reject(channel, settings, f"flat_noise(dyn={dyn_ratio:.1f})", pcm, out_rate, started)
    audio = process_audio(bytes(pcm), out_rate, settings)
    frms, fpeak, _ = pcm_stats(audio)
    try:
        res = upload_recording(channel, started, audio, out_rate, frms, fpeak)
        log(f"UPLOADED {res.get('id')} {channel.get('frequency_mhz')} {channel.get('name')} dur={duration:.1f}s")
        return True, "ok"
    except Exception as exc:
        log(f"upload error: {exc}")
        return False, "upload_error"


# --------------------------------------------------------------------- heartbeat

def send_heartbeat(scanned_total, cps, active, channel=None, recordings_uploaded=0,
                   last_error=None, levels=None, recording=False, live_db=None):
    touch_progress()   # heartbeats fire every window (scan) and every chunk (recording) = liveness
    payload = {
        "scanner_id": "raspi",
        "scanned_total": int(scanned_total),
        "channels_per_second": float(cps),
        "active": bool(active),
        "recording": bool(recording),
        "current_channel_id": int(channel["id"]) if channel else None,
        "current_frequency_mhz": channel.get("frequency_mhz") if channel else None,
        "current_name": channel.get("name") if channel else None,
        "recordings_uploaded": int(recordings_uploaded),
        "last_error": str(last_error) if last_error else None,
    }
    if levels:
        payload["levels"] = levels
    if live_db is not None:
        payload["live_db"] = float(live_db)
    try:
        http_json("/api/agent/heartbeat", payload=payload, timeout=4)
    except Exception as exc:
        log(f"heartbeat error: {exc}")


# --------------------------------------------------- in-process IQ engine (pyrtlsdr)
# Detection without per-band process spawns: one persistent RtlSdr retunes in-process
# (~tens of ms) to each <=2 MHz window and FFTs ALL channels in it at once (~25 ch/s vs
# ~3 with rtl_power). Recording still uses the proven rtl_fm chain (device handed off).
IQ_WINDOW_HZ = int(os.environ.get("IQ_WINDOW_HZ", "2000000"))
IQ_FFT_N = int(os.environ.get("IQ_FFT_N", str(1 << 15)))   # 32768: enough for detection, fast FFT
_sdr = None


def iq_open():
    global _sdr
    if _sdr is None:
        import rtlsdr
        _sdr = rtlsdr.RtlSdr(serial_number=DEVICE)
        _sdr.sample_rate = 2.4e6
    return _sdr


def iq_close():
    global _sdr
    if _sdr is not None:
        try:
            _sdr.close()
        except Exception:
            pass
        _sdr = None


def build_windows(channels):
    ordered = sorted(channels, key=lambda c: int(c["frequency_hz"]))
    wins, cur = [], []
    for ch in ordered:
        f = int(ch["frequency_hz"])
        if cur and f - int(cur[0]["frequency_hz"]) > IQ_WINDOW_HZ:
            wins.append(cur)
            cur = []
        cur.append(ch)
    if cur:
        wins.append(cur)
    return wins


def iq_detect_all(channels, settings, heartbeat=None, base_total=0, cps=0.0, recs=0):
    import numpy as np
    sdr = iq_open()
    gain = settings.get("gain", "") or "40"
    try:
        sdr.gain = float(gain)
    except (ValueError, TypeError):
        sdr.gain = "auto"
    margin = float(settings.get("detection_margin_db", 6))
    levels, candidates, scanned = {}, [], 0
    for win in build_windows(channels):
        if stop:
            break
        touch_progress()   # per-window liveness so a stuck read_samples trips the watchdog
        lo = int(win[0]["frequency_hz"]); hi = int(win[-1]["frequency_hz"])
        center = (lo + hi) // 2
        try:
            sdr.center_freq = max(center, 1_000_000)
            x = sdr.read_samples(IQ_FFT_N)
        except Exception as exc:
            log(f"iq read error: {exc}")
            usb_reset(); iq_close()
            return scanned, candidates, levels
        w = np.hanning(len(x))
        psd = 20 * np.log10(np.abs(np.fft.fftshift(np.fft.fft(x * w))) + 1e-9)
        freqs = center + np.fft.fftshift(np.fft.fftfreq(len(x), 1 / 2.4e6))
        floor = float(np.percentile(psd, 20))
        for ch in win:
            i = int(np.argmin(np.abs(freqs - int(ch["frequency_hz"]))))
            delta = round(float(psd[i] - floor), 1)
            levels[int(ch["id"])] = delta
            if delta >= margin:
                e = dict(ch); e["power_delta_db"] = delta
                candidates.append(e)
        scanned += len(win)
        if heartbeat:
            heartbeat(base_total + scanned, cps, True, None, recs, levels=dict(levels))
    return scanned, candidates, levels


# -------------------------------------------------------------------------- main

def main():
    log(f"agent start device={DEVICE} server={SERVER_URL}")
    usb_reset(force=True)  # always start from a clean device state
    last_config = 0.0
    last_heartbeat = 0.0
    config = {"settings": {"enabled": False}, "channels": []}
    scanned_total = 0
    recordings_uploaded = 0
    cps = 0.0
    empty_sweeps = 0
    touch_progress()
    threading.Thread(target=_watchdog, name="watchdog", daemon=True).start()

    while not stop:
        touch_progress()   # liveness: idle or scanning both count; only a blocked read goes stale
        if time.time() - last_config > CONFIG_POLL_SECONDS:
            try:
                config = http_json("/api/agent/config")
                last_config = time.time()
                log(f"config enabled={config['settings'].get('enabled')} channels={len(config.get('channels', []))}")
            except Exception as exc:
                log(f"config error: {exc}")
                send_heartbeat(scanned_total, cps, False, last_error=exc)
                time.sleep(5)
                continue

        settings = config.get("settings", {})
        channels = config.get("channels", [])
        if not settings.get("enabled") or not channels:
            if time.time() - last_heartbeat > 5:
                send_heartbeat(scanned_total, cps, False, recordings_uploaded=recordings_uploaded)
                last_heartbeat = time.time()
            time.sleep(1)
            continue

        engine = "iq" if settings.get("engine") in ("iq", "channelizer") else "search"
        sweep_started = time.time()
        scanned = 0
        all_candidates = []
        sweep_levels = {}

        if engine == "iq":
            try:
                scanned, all_candidates, sweep_levels = iq_detect_all(
                    channels, settings, heartbeat=send_heartbeat,
                    base_total=scanned_total, cps=cps, recs=recordings_uploaded)
            except Exception as exc:
                log(f"iq engine error: {exc} -> fallback to search")
                iq_close()
                engine = "search"

        if engine != "iq":
            for band in build_bands(channels):
                if stop:
                    break
                send_heartbeat(scanned_total + scanned, cps, True, None, recordings_uploaded)
                elapsed, candidates, levels = sweep_band(band, settings)
                scanned += len(band)
                if levels is None:
                    empty_sweeps += 1
                else:
                    empty_sweeps = 0
                    all_candidates.extend(candidates)
                    sweep_levels.update(levels)
                    send_heartbeat(scanned_total + scanned, cps, True, None,
                                   recordings_uploaded, levels=sweep_levels)
                log(f"sweep {band[0].get('frequency_mhz')}-{band[-1].get('frequency_mhz')} "
                    f"{len(band)}ch dt={elapsed:.1f}s candidates={0 if levels is None else len(candidates)}")
                if empty_sweeps >= 3:
                    did = usb_reset()
                    log(f"watchdog: {empty_sweeps} empty sweeps -> usb_reset={'done' if did else 'rate-limited'}, backing off")
                    send_heartbeat(scanned_total, cps, True, last_error="device delivering no samples")
                    time.sleep(30)
                    empty_sweeps = 0
                    break

        sweep_elapsed = max(time.time() - sweep_started, 0.001)
        scanned_total += scanned
        cps = scanned / sweep_elapsed
        all_candidates.sort(key=lambda c: (int(c.get("priority") or 0), float(c.get("power_delta_db") or 0)),
                            reverse=True)
        all_candidates = all_candidates[:MAX_CANDIDATES_PER_SWEEP]
        log(f"SWEEP DONE engine={engine} scanned={scanned} dt={sweep_elapsed:.1f}s cps={cps:.2f} candidates={len(all_candidates)}")
        send_heartbeat(scanned_total, cps, True, None, recordings_uploaded)
        last_heartbeat = time.time()

        if all_candidates and engine == "iq":
            iq_close()   # hand the device over to rtl_fm for recording
        for cand in all_candidates:
            if stop:
                break
            log(f"candidate {cand.get('frequency_mhz')} {cand.get('name')} +{cand.get('power_delta_db', 0):.1f}dB")
            ok, reason = record_candidate(
                cand, settings,
                heartbeat=lambda ch, rec, lvl=None: send_heartbeat(scanned_total, cps, True, ch,
                                                         recordings_uploaded, recording=rec, live_db=lvl),
            )
            if ok:
                recordings_uploaded += 1
                send_heartbeat(scanned_total, cps, True, None, recordings_uploaded)
            else:
                log(f"  skip {cand.get('frequency_mhz')}: {reason}")   # report_reject already tallied it


if __name__ == "__main__":
    main()
