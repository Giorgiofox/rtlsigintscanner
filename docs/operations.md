# Operations

## Deploy and update

```bash
docker compose up -d --build      # build image and (re)start the container
docker compose logs -f scanner    # follow logs
docker compose ps                 # container state and port mapping
```

The server code, static UI and templates are baked into the image (build context
`./server`), so any change to them needs a rebuild. Only `./data` and `./config`
are bind-mounted, so the database, recordings, models and channel CSVs survive a
rebuild. The UI links assets with a `?v=NN` query; bump it when you change
`static/app.js` or `static/style.css` so browsers reload.

## Configuration

Most behavior lives in the `settings` table and is editable in the UI (gear icon)
or via `POST /api/settings`. The agent reads it through `/api/agent/config` and
applies changes within `CONFIG_POLL_SECONDS`. No agent restart needed for setting
changes.

## Tuning the voice gate

If you are losing real transmissions, the gate is too strict. Order of attack:

1. Turn on `keep_rejected` (Settings, No-voice review). Rejected clips are kept.
2. In Live activity, click the reject count on a channel to listen to its no-voice
   clips and read the reason. The reason tells you which stage rejected it.
3. Tune by reason:
   - `flat_noise(dyn=...)` on FM channels: lower `min_dynamic_ratio` (default 2.6).
     `trim_to_voice` already confirmed voiced frames, so this gate is the usual
     cause of lost continuous FM voice.
   - `flat_noise(dyn=...)` on AM aviation: lower `am_min_dynamic_ratio` (default 1.4).
   - `below_open`: lower `open_threshold` (AM is quiet, try ~0.02).
   - `too_short`: lower `min_record_seconds`.
   - `flat` (in eval): lower `eval_min_dyn`.

If instead you are recording noise or carriers, raise the same values.

## Storage

The disk pill in the top bar shows total recording size. The Settings modal has a
storage panel with one-click purges: all no-voice clips, recordings older than 24 h
or 7 days, or all recordings. Deletions remove both the DB rows and the WAV files
and cannot be undone. No-voice clips are also auto-pruned to the last
`keep_rejected_max` per channel.

## Transcription

The faster-whisper worker runs in the server container, lazy-loads the model on the
first job, and polls for `queued` recordings. Set `WHISPER_ENABLED=0` to turn it
off (jobs stay queued). Each transcript carries a confidence score; a low score
means noisy or unreliable text. Use the re-transcribe button (permissive mode) to
re-run a clip without VAD when a transmission was cut.

## Backup

Back up `data/scanner.sqlite` (the channel list, settings and recording metadata)
and, if you want the audio, `data/recordings/`. The whisper model cache in
`data/models/` is large and re-downloadable, so it is optional.

## Troubleshooting

- Agent reports no samples or the dongle wedges: the agent issues a rate-limited
  `USBDEVFS_RESET`. If two dongles share one USB bus, give each a distinct serial
  and set `SCANNER_DEVICE` so the agent never touches the other one.
- Port already in use on rebuild: another container holds `8096`. Find it with
  `docker ps | grep 8096` and stop it.
- UI not updating after a change: rebuild the image (the UI is baked in) and bump
  the `?v=NN` asset version.
