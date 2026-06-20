const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? '').replace(/[&<>'"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[c]));
const mhz = (hz) => (hz / 1e6).toFixed(hz % 1000 ? 4 : 3);
const fmtDur = (s) => { s = Number(s || 0); return s < 60 ? `${s.toFixed(1)}s` : `${Math.floor(s / 60)}m${Math.round(s % 60)}s`; };
const fmtBytes = (b) => { b = Number(b || 0); return b < 1048576 ? (b / 1024).toFixed(0) + ' KB' : b < 1073741824 ? (b / 1048576).toFixed(1) + ' MB' : (b / 1073741824).toFixed(2) + ' GB'; };
const fmtTime = (v) => v ? new Date(v).toLocaleString('it-IT', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
// relative "time ago": <1min -> NOW (red/live); else whole minutes, then hours/days. No seconds.
function ageStr(v) {
  if (!v) return { txt: '', now: false };
  const ms = Date.now() - new Date(v).getTime();
  if (ms < 60000) return { txt: 'NOW', now: true };
  const m = Math.floor(ms / 60000);
  if (m < 60) return { txt: m + 'min ago', now: false };
  const h = Math.floor(m / 60);
  if (h < 24) return { txt: h + 'h ago', now: false };
  return { txt: Math.floor(h / 24) + 'd ago', now: false };
}
const ageSpan = (v) => { const a = ageStr(v); return `<span class="rec-age ${a.now ? 'now' : ''}" data-ts="${esc(v || '')}">${a.txt}</span>`; };
async function api(p, o) { const r = await fetch(p, { cache: 'no-store', ...(o || {}) }); if (!r.ok) throw new Error(await r.text()); return r.json(); }
const post = (p, body) => api(p, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
const put = (p, body) => api(p, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
let settings = null, margin = 5;

// ---- status bar ----
async function pollStatus() {
  let s; try { s = await api('/api/status'); } catch (e) { return; }
  settings = s.settings; margin = Number(settings.detection_margin_db || 5);
  const a = s.agent || {};
  $('pillAgent').classList.toggle('on', !!a.active);
  $('pCps').textContent = Number(a.channels_per_second || 0).toFixed(1);
  $('pScan').textContent = s.active_scan_channels || 0;
  $('pRec').textContent = s.recordings || 0;
  const rec = !!a.recording;
  $('pillVfo').classList.toggle('rec', rec);
  $('pVfo').textContent = rec && a.current_frequency_mhz ? Number(a.current_frequency_mhz).toFixed(4) : (a.active ? 'SCAN' : 'STOP');
  $('pVfoName').textContent = rec ? '● REC ' + (a.current_name || '') : '';
  $('power').textContent = settings.enabled ? 'RUN' : 'STOP';
  $('power').classList.toggle('off', !settings.enabled);
  if ($('smeter')) $('smeter').classList.toggle('hidden', settings.show_smeter === false);
}
$('power').onclick = async () => { await post('/api/settings', { enabled: !settings.enabled }); pollStatus(); };

// ---- settings modal (gear): sliders with min/max + explanations ----
const CFG_SECTIONS = [
  ['Engine & detection', [
    { k: 'engine', type: 'select', opts: [['search', 'search (rtl_power, light)'], ['channelizer', 'channelizer (in-process, fast)']], help: 'How channels are scanned. channelizer retunes in-process (≈25 ch/s); search spawns rtl_power per band (≈3-7 ch/s).' },
    { k: 'gain', type: 'gain', min: 0, max: 49.6, step: 0.1, help: 'RTL tuner gain in dB. 0 = automatic. Higher reveals weak signals but can overload/raise noise. Typical 30-45.' },
    { k: 'detection_margin_db', min: 2, max: 20, step: 1, unit: 'dB', help: 'A channel counts as active when its power is this many dB above the band noise floor. Lower = catches weaker signals (more false hits); higher = only strong.' },
    { k: 'ppm', min: -100, max: 100, step: 1, unit: 'ppm', help: 'Frequency correction for the dongle crystal error. Leave 0 unless tuning is off (often when the RTL is hot).' },
  ]],
  ['Squelch & recording', [
    { k: 'open_threshold', min: 0.005, max: 0.2, step: 0.005, help: 'Audio level to START a recording (probe RMS). Lower = opens on quieter audio. AM is quiet, keep low (~0.02).' },
    { k: 'close_threshold', min: 0.005, max: 0.2, step: 0.005, help: 'Audio level below which audio counts as silence while recording.' },
    { k: 'eval_min_dyn', min: 1, max: 4, step: 0.1, help: 'Voice-vs-carrier sensitivity. A flat carrier scores ~1; speech scores higher. Below this = rejected as not-voice. Lower = keeps more.' },
    { k: 'am_min_dynamic_ratio', min: 1.1, max: 3, step: 0.1, help: 'Final voice gate for AM aviation ONLY. AM/ATC voice is much less dynamic than FM (flat carrier ~1.1, weak ATC ~1.5). Lower = keeps weaker air-band transmissions (slightly more carrier risk).' },
    { k: 'silence_release_seconds', min: 0.5, max: 8, step: 0.5, unit: 's', help: 'How long the voice must stop before the squelch closes and the recording ends. Higher = does not split on pauses.' },
    { k: 'close_rel', min: 0.1, max: 0.6, step: 0.05, help: 'Gap detection: audio below this fraction of the loudest voice counts as silence. Lets the recording END over a constant carrier (a steady carrier is below the voice peak). Higher = closes sooner.' },
    { k: 'tail_seconds', min: 0, max: 10, step: 0.5, unit: 's', help: 'Seconds of trailing radio noise kept (ungated) at the END of each recording, so you hear the channel tail instead of an abrupt cut.' },
    { k: 'min_record_seconds', min: 0.3, max: 5, step: 0.1, unit: 's', help: 'Recordings shorter than this are discarded (drops blips).' },
    { k: 'max_record_seconds', min: 5, max: 120, step: 5, unit: 's', help: 'Hard cap on a single recording length.' },
    { k: 'trim_silence', type: 'bool', help: 'Trim dead carrier/noise off the START and END (a channel with a constant carrier never lets the squelch close, so captures run to the cap). Keeps tail_seconds of radio tail after the last voice.' },
    { k: 'trim_preroll_seconds', min: 0, max: 2, step: 0.1, unit: 's', help: 'Audio kept just BEFORE the first detected voice, so onsets are not clipped.' },
  ]],
  ['Audio cleanup', [
    { k: 'audio_hpf_hz', min: 100, max: 500, step: 10, unit: 'Hz', help: 'High-pass cutoff: removes sub-audible rumble/CTCSS below this.' },
    { k: 'audio_lpf_hz', min: 2400, max: 5000, step: 100, unit: 'Hz', help: 'Low-pass cutoff: removes hiss above the voice band (~3000 Hz for radio voice).' },
    { k: 'audio_norm_level', min: 0.1, max: 0.6, step: 0.02, help: 'Target loudness after normalization. Higher = louder recordings.' },
  ]],
  ['No-voice review', [
    { k: 'keep_rejected', type: 'bool', help: 'Keep the audio of rejected (no-voice) candidates so you can listen and understand WHY a channel fails — carrier? noise? real voice cut by the gate? Click the ⊘ count in Live activity to hear them. OFF (default) = clips are discarded.' },
    { k: 'keep_rejected_max', min: 1, max: 20, step: 1, help: 'How many recent no-voice clips to keep per channel (older ones auto-delete).' },
  ]],
  ['Display', [
    { k: 'show_smeter', type: 'bool', help: 'Show the analog S-meter (needle) above the Live activity panel.' },
  ]],
];
function openConfig() {
  let html = '';
  for (const [section, items] of CFG_SECTIONS) {
    html += `<div class="cfg-sec"><h4>${section}</h4>`;
    for (const p of items) {
      const v = settings[p.k];
      if (p.type === 'select') {
        html += `<div class="cfg-row"><div class="cfg-lbl">${p.k}</div>
          <select data-k="${p.k}" data-t="select">${p.opts.map(o => `<option value="${o[0]}" ${String(v) === o[0] ? 'selected' : ''}>${o[1]}</option>`).join('')}</select>
          <div class="cfg-help">${p.help}</div></div>`;
      } else if (p.type === 'bool') {
        html += `<div class="cfg-row"><div class="cfg-lbl">${p.k}</div>
          <select data-k="${p.k}" data-t="bool"><option value="true" ${v ? 'selected' : ''}>yes</option><option value="false" ${!v ? 'selected' : ''}>no</option></select>
          <div class="cfg-help">${p.help}</div></div>`;
      } else {
        const cur = (p.type === 'gain' && (v === '' || v == null)) ? 0 : Number(v);
        html += `<div class="cfg-row"><div class="cfg-lbl">${p.k} <span class="cfg-val" id="val_${p.k}">${p.type === 'gain' && cur === 0 ? 'auto' : cur}${p.unit ? ' ' + p.unit : ''}</span></div>
          <input type="range" data-k="${p.k}" data-t="${p.type || 'num'}" min="${p.min}" max="${p.max}" step="${p.step}" value="${cur}">
          <div class="cfg-help">${p.help}${p.type === 'gain' ? ' (0 = auto)' : ''}</div></div>`;
      }
    }
    html += '</div>';
  }
  html += `<div class="cfg-sec"><h4>Storage</h4>
    <div class="cfg-help" id="cfgUsage">loading…</div>
    <div class="cfg-actions">
      <button class="warn" data-purge='{"kind":"novoice"}'>🗑 Delete all no-voice clips</button>
      <button class="warn" data-purge='{"older":1}'>🗑 Delete recordings older than 24 h</button>
      <button class="warn" data-purge='{"older":7}'>🗑 Delete recordings older than 7 days</button>
      <button class="danger" data-purge='{"all":true}'>🗑 Delete ALL recordings</button>
    </div></div>`;
  $('cfgBody').innerHTML = html;
  $('cfgBody').querySelectorAll('[data-k]').forEach(el => {
    const k = el.dataset.k, t = el.dataset.t;
    const handler = () => {
      let val;
      if (t === 'bool') val = el.value === 'true';
      else if (t === 'select') val = el.value;
      else if (t === 'gain') { const n = Number(el.value); val = n === 0 ? '' : String(n); }
      else val = Number(el.value);
      if (el.type === 'range') { const lbl = $('val_' + k); if (lbl) lbl.textContent = (t === 'gain' && Number(el.value) === 0 ? 'auto' : el.value); }
      post('/api/settings', { [k]: val }).then(() => { settings[k] = val; });
    };
    el.addEventListener(el.type === 'range' ? 'input' : 'change', handler);
  });
  // Storage panel: live usage + delete actions
  const showUsage = () => api('/api/recordings/usage').then(u => {
    const el = $('cfgUsage'); if (el) el.textContent = `${fmtBytes(u.bytes)} · ${u.files} files (${u.voice} voice, ${u.novoice} no-voice)`;
  }).catch(() => {});
  showUsage();
  $('cfgBody').querySelectorAll('[data-purge]').forEach(btn => btn.onclick = async () => {
    const spec = JSON.parse(btn.dataset.purge), body = {};
    let label = 'recordings';
    if (spec.kind) { body.kind = spec.kind; label = 'all no-voice clips'; }
    else if (spec.older) { body.until = new Date(Date.now() - spec.older * 86400e3).toISOString(); body.include_novoice = true; label = `recordings older than ${spec.older === 1 ? '24 hours' : spec.older + ' days'}`; }
    else if (spec.all) { body.include_novoice = true; label = 'ALL recordings'; }
    if (!confirm(`Delete ${label}? Files are deleted permanently and cannot be undone.`)) return;
    try { const r = await post('/api/recordings/purge', body); alert(`Deleted ${r.deleted} recordings.`); }
    catch (e) { alert('Delete failed: ' + e); }
    showUsage(); loadUsage(); loadFeed();
  });
  $('cfgModal').classList.remove('hidden');
}
$('gear').onclick = openConfig;
$('cfgClose').onclick = () => $('cfgModal').classList.add('hidden');
$('cfgModal').onclick = (e) => { if (e.target === $('cfgModal')) $('cfgModal').classList.add('hidden'); };

// ---- Monitor: auto-play new transmissions as they arrive (near-live scanner) ----
let monitorOn = false, lastMonId = null;
function monNowShow(r) {
  const el = $('monNow'); if (!el) return;
  if (!r) { el.classList.add('hidden'); el.textContent = ''; return; }
  el.classList.remove('hidden');
  el.innerHTML = `♪ <b>${esc(Number(r.frequency_mhz).toFixed(4))}</b> ${esc(r.name || '')} <small>${esc(r.system || '')}</small>`;
}
monitorOn = localStorage.getItem('monOn') === '1';   // restore across reloads
$('monitor').classList.toggle('on', monitorOn);
$('monitor').onclick = () => {
  monitorOn = !monitorOn;
  localStorage.setItem('monOn', monitorOn ? '1' : '0');
  $('monitor').classList.toggle('on', monitorOn);
  if (!monitorOn) { stopMon(); }
  else lastMonId = null;   // arm: next new recording plays (don't replay backlog)
};
function stopMon() { const a = $('monAudio'); a.pause(); try { a.currentTime = 0; } catch (e) {} $('monStop').classList.add('hidden'); monNowShow(null); }
$('monStop').onclick = stopMon;
$('monAudio').addEventListener('ended', () => { monNowShow(null); $('monStop').classList.add('hidden'); });
async function monitorPoll() {
  if (!monitorOn) return;
  let recs; try { recs = await api('/api/recordings?limit=1'); } catch (e) { return; }
  if (!recs.length) return;
  const n = recs[0];
  if (lastMonId === null) { lastMonId = n.id; return; }
  if (n.id === lastMonId) return;
  lastMonId = n.id;
  const a = $('monAudio');
  if (a.paused) { a.src = n.audio_url; a.play().then(() => { monNowShow(n); $('monStop').classList.remove('hidden'); }).catch(() => {}); }
}

// ---- banks: toggle a whole bank ON/OFF (soft — remembers per-channel enabled) ----
async function loadBanks() {
  const cats = await api('/api/categories');  // [{category, channels, enabled_channels}]
  const active = (settings && settings.active_categories) || [];
  if ($('bandsInfo')) $('bandsInfo').textContent = `${active.length}/${cats.length} on`;
  $('bands').innerHTML = cats.map(c => {
    const on = active.includes(c.category);   // bank active = in scan (regardless of per-channel)
    return `<div class="band ${on ? 'on' : ''}">
      <div class="band-info"><b>${esc(c.category)}</b><small>${c.enabled_channels}/${c.channels} ch enabled</small></div>
      <div class="tg ${on ? 'on' : ''}" data-cat="${esc(c.category)}"></div></div>`;
  }).join('');
  document.querySelectorAll('#bands .tg').forEach(t => t.onclick = async () => {
    const cat = t.dataset.cat;
    const next = new Set((settings && settings.active_categories) || []);
    next.has(cat) ? next.delete(cat) : next.add(cat);
    await post('/api/settings', { active_categories: [...next] });
    await pollStatus(); loadBanks();
  });
}

// ---- recordings feed ----
function transBlock(r) {
  const t = r.transcription_status;
  if (t === 'done' && r.transcript) {
    const c = r.transcript_confidence;
    const badge = (c != null) ? `<span class="conf ${c >= 0.6 ? 'hi' : c >= 0.35 ? 'mid' : 'lo'}" title="Transcription reliability (low = noisy/unreliable)">${Math.round(c * 100)}%</span>` : '';
    return `<div class="rec-transcript">${badge}${esc(r.transcript)}</div>`;
  }
  return `<div class="rec-transcript empty">${({ queued: 'queued…', running: 'transcribing…', error: 'error' }[t]) || 'no transcript'}</div>`;
}
function recCard(r, fresh) {
  const a = ageStr(r.started_at);
  return `<article class="rec-card ${fresh ? 'fresh' : ''} ${a.now ? 'now' : ''}" data-ts="${esc(r.started_at || '')}">
    <div class="rec-top"><span class="rec-freq">${esc(Number(r.frequency_mhz).toFixed(4))}</span>
      <span class="rec-name">${esc(r.name || 'Channel')}</span><span class="rec-tag">${esc(r.mode || '')}</span>
      <span class="rec-tag">${esc(r.system || '')}</span><span class="rec-time">${fmtTime(r.started_at)} · ${fmtDur(r.duration_seconds)} ${ageSpan(r.started_at)}</span></div>
    ${transBlock(r)}
    <div class="rec-foot"><button class="playbtn" data-src="${r.audio_url}">▶ play</button>
      <span class="rec-id mono" title="${esc(r.id)}">#${esc(String(r.id).slice(0, 8))}</span>
      <button class="retx" data-id="${esc(r.id)}" title="Re-transcribe (permissive: transcribes the whole audio)">↻ re-tx</button>
      <button class="del" data-id="${esc(r.id)}">✕</button></div>
  </article>`;
}
function recItem(r) {  // compact row inside a frequency group (freq/name are in the group header)
  const a = ageStr(r.started_at);
  return `<div class="rg-item ${a.now ? 'now' : ''}" data-ts="${esc(r.started_at || '')}">
    <div class="rg-item-top"><span class="rec-tag">${esc(r.mode || '')}</span>
      <span class="rec-time">${fmtTime(r.started_at)} · ${fmtDur(r.duration_seconds)} ${ageSpan(r.started_at)}</span></div>
    ${transBlock(r)}
    <div class="rec-foot"><button class="playbtn" data-src="${r.audio_url}">▶ play</button>
      <span class="rec-id mono" title="${esc(r.id)}">#${esc(String(r.id).slice(0, 8))}</span>
      <button class="retx" data-id="${esc(r.id)}" title="Re-transcribe (permissive: transcribes the whole audio)">↻ re-tx</button>
      <button class="del" data-id="${esc(r.id)}">✕</button></div>
  </div>`;
}
const audioPlaying = () => [...document.querySelectorAll('audio')].filter(a => a.id !== 'monAudio').some(a => !a.paused && !a.ended);
// lazy audio + live spectrogram: ▶ becomes an <audio> + a scrolling FFT canvas on click.
// Speech shows horizontal formant bands with gaps; noise/carrier shows a flat uniform wash.
let _audioCtx = null;
function playWithSpectrogram(btn) {
  const wrap = document.createElement('div'); wrap.className = 'player';
  const a = document.createElement('audio'); a.controls = true; a.preload = 'none'; a.src = btn.dataset.src;
  const cv = document.createElement('canvas'); cv.className = 'spec'; cv.width = 360; cv.height = 50;
  wrap.append(a, cv); btn.replaceWith(wrap);
  a.play().catch(() => {});
  try {
    _audioCtx = _audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const ac = _audioCtx; if (ac.state === 'suspended') ac.resume();
    const an = ac.createAnalyser(); an.fftSize = 512; an.smoothingTimeConstant = 0.4;
    ac.createMediaElementSource(a).connect(an); an.connect(ac.destination);
    const bins = an.frequencyBinCount, data = new Uint8Array(bins);
    const g = cv.getContext('2d'); g.fillStyle = '#06090d'; g.fillRect(0, 0, cv.width, cv.height);
    const useBins = Math.max(8, Math.floor(bins * (3600 / (ac.sampleRate / 2))));  // voice band only
    let raf;
    const draw = () => {
      if (a.paused || a.ended) { cancelAnimationFrame(raf); return; }
      an.getByteFrequencyData(data);
      g.drawImage(cv, 2, 0, cv.width - 2, cv.height, 0, 0, cv.width - 2, cv.height);  // scroll left
      for (let y = 0; y < cv.height; y++) {
        const v = data[Math.floor((1 - y / cv.height) * useBins)] || 0, t = v / 255;
        g.fillStyle = v < 12 ? '#070b10' : `hsl(${190 - t * 150},90%,${8 + t * 52}%)`;
        g.fillRect(cv.width - 2, y, 2, 1);
      }
      raf = requestAnimationFrame(draw);
    };
    a.addEventListener('ended', () => cancelAnimationFrame(raf));
    draw();
  } catch (e) { /* spectrogram optional; audio still plays */ }
}
$('feed').addEventListener('click', (e) => {
  const b = e.target.closest('.playbtn'); if (b) { playWithSpectrogram(b); return; }
  const rt = e.target.closest('.retx');
  if (rt) {
    post('/api/recordings/' + encodeURIComponent(rt.dataset.id) + '/retranscribe?permissive=1', {})
      .then(() => { rt.textContent = '↻ queued'; rt.disabled = true; setTimeout(loadFeed, 2000); })
      .catch(() => {});
  }
});
async function loadFeed() {
  if (audioPlaying()) return;
  const url = new URL('/api/recordings', location.origin);
  url.searchParams.set('limit', '300');
  if ($('search').value.trim()) url.searchParams.set('q', $('search').value.trim());
  if ($('fCat').value) url.searchParams.set('category', $('fCat').value);
  if ($('fMode').value) url.searchParams.set('mode', $('fMode').value);
  const tv = $('fTime').value;
  if (tv === 'date') {
    $('fDate').style.display = '';
    if ($('fDate').value) { url.searchParams.set('since', $('fDate').value + 'T00:00:00Z'); url.searchParams.set('until', $('fDate').value + 'T23:59:59Z'); }
  } else {
    $('fDate').style.display = 'none';
    if (tv) url.searchParams.set('since', new Date(Date.now() - Number(tv) * 3600e3).toISOString());
  }
  const recs = await api(url.pathname + url.search);
  if (!recs.length) { $('feed').innerHTML = '<div class="empty-msg">No recordings. Enable a bank and press RUN.</div>'; return; }
  if ($('fGroup').value === 'freq') {
    const g = new Map();
    for (const r of recs) { const k = r.frequency_mhz; if (!g.has(k)) g.set(k, { f: k, name: r.name, system: r.system, items: [] }); g.get(k).items.push(r); }
    const groups = [...g.values()].sort((a, b) => b.items[0].started_at.localeCompare(a.items[0].started_at));
    $('feed').innerHTML = groups.map((gr) => `<div class="rec-group">
      <div class="rg-head"><span class="rg-caret">▾</span><span class="rec-freq">${esc(Number(gr.f).toFixed(4))}</span>
        <span class="rec-name">${esc(gr.name || 'Channel')}</span><span class="rec-tag">${esc(gr.system || '')}</span>
        <span class="rg-last">${fmtTime(gr.items[0].started_at)}</span><span class="rg-count">${gr.items.length}</span></div>
      <div class="rg-items">${gr.items.map(recItem).join('')}</div></div>`).join('');
    document.querySelectorAll('.rg-head').forEach(h => h.onclick = (e) => {
      if (e.target.closest('audio,button')) return;
      h.parentElement.classList.toggle('collapsed');
    });
  } else {
    $('feed').innerHTML = recs.map((r, i) => recCard(r, i === 0)).join('');
  }
  document.querySelectorAll('.del').forEach(b => b.onclick = async () => { if (confirm('Delete?')) { await api('/api/recordings/' + encodeURIComponent(b.dataset.id), { method: 'DELETE' }); loadFeed(); } });
}

// ---- recordings disk usage + bulk delete ----
async function loadUsage() {
  try {
    const u = await api('/api/recordings/usage');
    $('pSize').textContent = fmtBytes(u.bytes || 0);
    $('pillSize').title = `${u.files} files · ${u.voice} voice · ${u.novoice} no-voice — disk used by recordings`;
  } catch (e) {}
}
// ---- scan activity (dB bars) ----
async function pollScan() {
  let s; try { s = await api('/api/scan'); } catch (e) { return; }
  $('scanInfo').textContent = `${s.scanning} ch · ${Number(s.cps || 0).toFixed(1)} ch/s`;
  const m = Number(s.margin_db || margin);
  const LO = -5, HI = 30, span = HI - LO;
  if (!s.channels.length) { $('scanlist').innerHTML = '<div class="empty-msg">Nothing scanning.<br>Enable a bank.</div>'; return; }
  const sortBy = ($('scanSort') && $('scanSort').value) || 'activity';
  const n = v => Number(v) || 0;
  const cmps = {
    activity: (a, b) => String(b.last_rec || '').localeCompare(String(a.last_rec || '')),
    rec: (a, b) => n(b.rec_count) - n(a.rec_count),
    fail: (a, b) => n(b.fail_count) - n(a.fail_count),
    signal: (a, b) => n(b.level_db) - n(a.level_db),
    mem: (a, b) => n(a.id) - n(b.id),
    freq: (a, b) => Number(a.frequency_mhz) - Number(b.frequency_mhz),
  };
  s.channels.sort((a, b) => (cmps[sortBy] || cmps.activity)(a, b) || Number(a.frequency_mhz) - Number(b.frequency_mhz));
  $('scanlist').innerHTML = s.channels.map(c => {
    const db = Number(c.level_db ?? LO);
    const pct = Math.max(0, Math.min(100, ((db - LO) / span) * 100));
    const thr = Math.max(0, Math.min(100, ((m - LO) / span) * 100));
    const lvl = c.level_db == null ? '—' : `${db > 0 ? '+' : ''}${db}`;
    const rc = c.rec_count || 0, fc = c.fail_count || 0, fb = c.fail_by || {};
    const fbTip = Object.keys(fb).length ? Object.entries(fb).map(([k, v]) => `${k}: ${v}`).join(' · ') : 'no rejects yet';
    const dud = rc === 0;   // yellow = no good (voice) recording yet on this channel
    return `<div class="scan-row ${c.state}${dud ? ' dud' : ''}">
      <span class="sf">${esc(Number(c.frequency_mhz).toFixed(4))}</span>
      <span class="sn">${esc(c.name || '')} <small>${esc(c.system || '')}</small></span>
      <span class="rc ${rc ? '' : 'zero'}" title="${rc} valid voice recordings">${rc}▸</span>
      <span class="fc ${fc ? '' : 'zero'}" data-cid="${c.id}" data-nm="${esc(c.name || '')} ${esc(Number(c.frequency_mhz).toFixed(4))}" title="no-voice rejects — ${esc(fbTip)}${fc ? ' · click to listen' : ''}">${fc}⊘</span>
      <span class="sdb">${lvl}${c.level_db == null ? '' : ' dB'}</span>
      <button class="srow-off" data-cid="${c.id}" title="Remove from scan (re-enable from Memories)">✕</button>
      <span class="track"><i style="width:${pct}%"></i><span class="thr" style="left:${thr}%"></span></span>
    </div>`;
  }).join('');
  $('scanlist').querySelectorAll('.fc:not(.zero)').forEach(el => el.onclick = () => openNoVoice(el.dataset.cid, el.dataset.nm));
  $('scanlist').querySelectorAll('.srow-off').forEach(el => el.onclick = async () => {
    await put('/api/channels/' + el.dataset.cid, { enabled: false });
    pollScan(); loadMemories(); pollStatus();
  });
}
// real-time S-meter: poll a tiny endpoint fast so the needle tracks the live signal
async function pollLevel() {
  let d; try { d = await api('/api/level'); } catch (e) { return; }
  updateSmeter(d.level_db, d.name || (d.active ? '' : 'idle'), d.recording);
}

// ---- memories ----
function memRow(c) {
  return `<div class="mem-row ${c.enabled ? '' : 'off'}" data-id="${c.id}">
    <div class="tg sm m-en ${c.enabled ? 'on' : ''}" title="Scan on/off"></div>
    <input class="m-fq" value="${esc(c.frequency_mhz || '')}">
    <input class="mname m-nm" value="${esc(c.name || '')}">
    <button class="del m-del" title="Delete">✕</button></div>`;
}
async function loadMemories() {
  const url = new URL('/api/channels', location.origin);
  url.searchParams.set('limit', '500'); url.searchParams.set('enabled', $('memEnabled').value);
  if ($('memSearch').value.trim()) url.searchParams.set('q', $('memSearch').value.trim());
  if ($('memCat').value) url.searchParams.set('category', $('memCat').value);
  const rows = await api(url.pathname + url.search);
  $('memCount').textContent = `${rows.length}`;
  $('memories').innerHTML = rows.map(memRow).join('') || '<div class="empty-msg">None.</div>';
  document.querySelectorAll('.mem-row').forEach((row, i) => {
    const c = rows[i]; const save = (p) => put('/api/channels/' + c.id, p);
    const tg = row.querySelector('.m-en');
    tg.onclick = () => {   // real-time enable/disable of a single frequency
      const on = !tg.classList.contains('on');
      tg.classList.toggle('on', on); row.classList.toggle('off', !on);
      save({ enabled: on }).then(pollStatus);
    };
    row.querySelector('.m-fq').onchange = (e) => save({ frequency_mhz: e.target.value });
    row.querySelector('.m-nm').onchange = (e) => save({ name: e.target.value });
    row.querySelector('.m-del').onclick = async () => { if (confirm('Delete?')) { await api('/api/channels/' + c.id, { method: 'DELETE' }); loadMemories(); pollStatus(); } };
  });
}
async function loadCats() {
  const cats = await api('/api/categories');
  const opts = '<option value="">banks</option>' + cats.map(c => `<option>${esc(c.category)}</option>`).join('');
  ['fCat', 'memCat'].forEach(id => { const s = $(id).value; $(id).innerHTML = opts; $(id).value = s; });
}

async function bulkMem(on) {
  const n = document.querySelectorAll('.mem-row').length;
  if (!confirm(`${on ? 'Enable' : 'Disable'} all ${n} shown channels?`)) return;
  await post('/api/channels/bulk', { category: $('memCat').value || null, q: $('memSearch').value.trim() || null, set_enabled: on });
  await loadMemories(); pollStatus(); loadBanks();
}
$('memOn').onclick = () => bulkMem(true);
$('memOff').onclick = () => bulkMem(false);
['search', 'fCat', 'fMode', 'fGroup', 'fTime', 'fDate'].forEach(id => $(id).addEventListener('input', loadFeed));
['memSearch', 'memCat', 'memEnabled'].forEach(id => $(id).addEventListener('input', loadMemories));

// ---- no-voice clips: listen to a channel's rejected captures (needs keep_rejected ON) ----
async function openNoVoice(cid, label) {
  $('nvTitle').textContent = 'No-voice clips · ' + (label || '');
  $('nvBody').innerHTML = '<div class="nv-empty">loading…</div>';
  $('nvModal').classList.remove('hidden');
  let recs;
  try { recs = await api('/api/recordings?kind=novoice&limit=10&channel_id=' + encodeURIComponent(cid)); }
  catch (e) { $('nvBody').innerHTML = '<div class="nv-empty">error loading</div>'; return; }
  if (!recs.length) {
    $('nvBody').innerHTML = `<div class="nv-empty">No clips kept yet for this channel.<br>Turn ON <b>Settings ⚙ → No-voice review → keep_rejected</b>, then wait for activity here.</div>`;
    return;
  }
  $('nvBody').innerHTML = recs.map(r => `<div class="nv-item">
    <div class="nv-meta"><span class="rec-id mono" title="${esc(r.id)}">#${esc(String(r.id).slice(0, 8))}</span> · <b>${esc(r.reject_reason || 'no-voice')}</b> · ${fmtTime(r.started_at)} · ${fmtDur(r.duration_seconds)} · ${ageStr(r.started_at).txt}</div>
    <audio controls preload="none" src="${r.audio_url}"></audio></div>`).join('');
}
$('nvClose').onclick = () => $('nvModal').classList.add('hidden');
$('nvModal').onclick = (e) => { if (e.target === $('nvModal')) $('nvModal').classList.add('hidden'); };

// ---- live "time ago" ticker: refresh NOW/Nmin labels + NOW borders without refetching ----
function tickAges() {
  document.querySelectorAll('.rec-age[data-ts]').forEach(el => { const a = ageStr(el.dataset.ts); el.textContent = a.txt; el.classList.toggle('now', a.now); });
  document.querySelectorAll('.rec-card[data-ts], .rg-item[data-ts]').forEach(el => el.classList.toggle('now', ageStr(el.dataset.ts).now));
}

// ---- analog S-meter (needle): S1..S9..+60, red S9+ zone, shows current strongest signal ----
const smPt = (cx, cy, r, deg) => { const a = deg * Math.PI / 180; return [cx + r * Math.cos(a), cy - r * Math.sin(a)]; };
function smBuild() {
  const cx = 120, cy = 170, R = 152, LR = 124;   // pivot off-canvas (hidden), wide arc
  const majors = [['1', 0], ['3', .15], ['5', .3], ['7', .45], ['9', .6], ['+20', .73], ['+40', .865], ['+60', 1]];
  const arc = (d1, d2) => { const [ax, ay] = smPt(cx, cy, R, d1), [bx, by] = smPt(cx, cy, R, d2); return `M ${ax.toFixed(1)} ${ay.toFixed(1)} A ${R} ${R} 0 0 1 ${bx.toFixed(1)} ${by.toFixed(1)}`; };
  if ($('smArc')) $('smArc').setAttribute('d', arc(135, 45));
  if ($('smArcRed')) $('smArcRed').setAttribute('d', arc(135 - 0.6 * 90, 45));   // S9 -> +60 in red
  let ticks = '', labels = '';
  for (let i = 0; i <= 20; i++) {
    const f = i / 20, deg = 135 - f * 90, big = majors.some(m => Math.abs(m[1] - f) < 0.012), red = f >= 0.6 - 1e-9;
    const [x1, y1] = smPt(cx, cy, R, deg), [x2, y2] = smPt(cx, cy, R - (big ? 20 : 10), deg);
    ticks += `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" class="sm-tick ${big ? 'maj' : ''} ${red ? 'red' : ''}"/>`;
  }
  for (const [lab, f] of majors) {
    const deg = 135 - f * 90;
    const [x, y] = smPt(cx, cy, LR, deg);
    labels += `<text x="${x.toFixed(1)}" y="${y.toFixed(1)}" class="sm-lab ${f >= 0.6 ? 'red' : ''}">${lab}</text>`;
  }
  if ($('smTicks')) $('smTicks').innerHTML = ticks;
  if ($('smLabels')) $('smLabels').innerHTML = labels;
}
function updateSmeter(delta, label, rec) {
  const v = (delta == null || isNaN(delta)) ? null : Number(delta);
  const frac = v == null ? 0 : Math.max(0, Math.min(1, v / 33));   // ~0..33 dB over floor -> S1..+60
  const n = $('smNeedle'); if (n) n.style.transform = `rotate(${((frac - 0.5) * 90).toFixed(1)}deg)`;
  if ($('smVal')) $('smVal').textContent = v == null ? '—' : ((v > 0 ? '+' : '') + v.toFixed(0));
  if ($('smCh')) $('smCh').textContent = label || '';
  if ($('smeter')) $('smeter').classList.toggle('rec', !!rec);
}

if ($('scanSort')) {
  const sv = localStorage.getItem('scanSort'); if (sv) $('scanSort').value = sv;
  $('scanSort').onchange = () => { localStorage.setItem('scanSort', $('scanSort').value); pollScan(); };
}

async function boot() { smBuild(); await pollStatus(); await loadCats(); await loadBanks(); await loadFeed(); await pollScan(); await loadMemories(); loadUsage(); pollLevel(); }
boot().catch(console.error);
setInterval(pollStatus, 2500);
setInterval(pollScan, 1500);
setInterval(loadFeed, 8000);
setInterval(loadBanks, 15000);
setInterval(monitorPoll, 3000);
setInterval(tickAges, 15000);
setInterval(loadUsage, 15000);
setInterval(pollLevel, 450);
