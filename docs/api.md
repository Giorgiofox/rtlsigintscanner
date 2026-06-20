# HTTP API

Base URL: `http://<server-ip>:8096`. All endpoints return JSON unless noted.

## Status and settings

### `GET /api/status`
Full status snapshot: `{receiver, settings, channels, active_scan_channels,
recordings, last_recording, agent, categories}`.

### `GET /api/scan`
Live scan view. Returns `{scanning, cps, active, recording, current_channel_id,
current_frequency_mhz, margin_db, levels_at, channels}`. Each channel has
`id, frequency_mhz, name, system, group_name, mode, level_db, state, rec_count,
fail_count, fail_by, last_rec`. `state` is `idle`, `active` (signal at or above
`margin_db`) or `rec`.

### `GET /api/level`
Lightweight S-meter poll: `{level_db, recording, name, freq, cps, active}`.

### `GET /api/settings`
Return the full settings dict.

### `POST /api/settings`
Update settings. Body is any subset of the settings keys. Returns the updated dict.

## Channels and banks

### `GET /api/categories`
List banks: `[{category, channels, enabled_channels}]`.

### `GET /api/channels`
Query params: `category`, `enabled` (`0`/`1`, default `1`), `q` (search name, freq,
system, group), `limit` (default 200, max 2000).

### `POST /api/channels`
Create a channel. Required `frequency_mhz`; optional `name`, `system` (default
`DISCOVERED`), `group_name`, `mode` (default `NFM`), `enabled`, `priority`.

### `PUT /api/channels/{id}`
Update a channel. Any of: `enabled, frequency_mhz, name, system, group_name, mode,
priority, tone_hz, attenuation, scan_exclude, transcription_language`.

### `DELETE /api/channels/{id}`
Delete a channel.

### `POST /api/channels/bulk`
Apply `set_enabled` and/or `set_scan_exclude` to all channels matching
`{category, q}`. Returns `{ok, updated}`.

### `POST /api/categories/rename`
Body `{old_system, new_system, old_group_name?, new_group_name?}`. With a group
name, renames that group within the system; otherwise renames the whole system.

## Recordings

### `GET /api/recordings`
Query params: `category, mode, channel_id, status, since, until, q, kind, limit`.
`kind` defaults to voice recordings; pass `kind=novoice` to list rejected clips.
Each item includes `audio_url`.

### `GET /api/recordings/usage`
`{bytes, files, voice, novoice}` for the disk-usage indicator.

### `POST /api/recordings/purge`
Bulk delete by filter `{kind?, include_novoice?, category, mode, since, until, q}`.
Deletes DB rows and WAV files. Returns `{ok, deleted}`.

### `GET /api/recordings/{id}/audio`
Stream the WAV file.

### `DELETE /api/recordings/{id}`
Delete a recording and its WAV file.

### `POST /api/recordings/{id}/retranscribe`
Re-queue for transcription. Query param `permissive` (default false): when true,
transcribes the whole audio with VAD and previous-text conditioning disabled, to
recover cut transcripts at the cost of more noise-as-words.

### `POST /api/recordings/upload`
Used by the agent. Required: `channel_id, frequency_hz, started_at,
duration_seconds, sample_rate, audio_wav_base64`. Optional: `frequency_mhz, name,
system, group, category, mode, rms, peak, kind, reject_reason`. `kind` is `voice`
(default) or `novoice`. Returns `{ok, id, sha256}`. No-voice clips are rolling
pruned to the last `keep_rejected_max` per channel.

## Band plan

### `GET /api/bandplan`
Bands grouped by group: `{groups: [{group, bands, enabled, total}]}`.

### `POST /api/bandplan/{id}`
Toggle one band. Body `{enabled}`.

### `POST /api/bandplan/group/toggle`
Toggle a whole group. Body `{group_name, enabled}`.

## Agent

### `GET /api/agent/config`
Used by the Pi. Returns `{settings, channels, bands}` (enabled channels in the
active banks, plus enabled band-plan ranges).

### `POST /api/agent/heartbeat`
Status update: `scanner_id, scanned_total, channels_per_second, active, recording,
current_channel_id, current_frequency_mhz, current_name, recordings_uploaded,
last_error, levels` (map of channel id to signal delta dB), `live_db` (S-meter).

### `POST /api/agent/reject`
Report a candidate that failed the voice gate. Body `{channel_id, reason?}`. Tallied
per channel and reason and shown as the reject count in Live activity.

### `POST /api/agent/exclude`
Report a channel stuck on a continuous carrier. Body `{channel_id, reason?}`. Sets
the channel disabled with an excluded timestamp.

### `GET /api/agent/status`
List all agent status records.
