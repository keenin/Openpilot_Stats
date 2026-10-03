I keep this here for myself. It was written with AI for something I wanted, not as a product I maintain for other people. You are welcome to use it or copy it.

# Openpilot usage (personal)


Private pipeline for **one driver**. Tracks which experimental/nightly openpilot commits you flash, using **your** engaged driving time as the quality signal. Not a fork of comma connect. Not community data.


The public artifact is a **static `index.html`**. The browser never talks to comma. Code in this repo stays private; you can share the Pages URL on Openpilot Discord.


```
Debian box (cron 03:00 PT)
  ├─ JWT + dongle_id from ~/.config/op-usage/credentials.env   # never in git
  ├─ GET comma API route metadata (git_commit, miles, times)
  ├─ sync-qlogs: GET /v1/route/{name}/files → missing qlogs only (rate limit 5/min)
  ├─ parse engaged time from local ~/.cache/op-usage/qlogs (never Comma in this step)
  ├─ SQLite cache (watermark + per-route engaged / not-in-park time)
  └─ write site/index.html → wrangler pages deploy
```


- **Include a drive** only if length ≥ 1 mile **and** engaged time > 0.
- **List a group** only if it has ≥ 3 qualifying drives.
- **Other branches:** one row per git SHA.
- **`master` only** (`master`, `origin/master`, `refs/heads/master`): walk qualifying drives in start-time order and split when the driving-model weights fingerprint changes. A SHA that reappears after a different fingerprint is a **new interval**. Weights = blob SHAs of driving ONNX/pkl under `selfdrive/modeld/models` (then `openpilot/selfdrive/modeld/models`), via the GitHub contents API from `git_commit` + `git_remote`, cached in sqlite (including confirmed misses). UI / cars / CI commits do not split the group. Unknown fingerprints stay one-row-per-interval. Commit cell = last SHA (tooltip: first … last). Optional `GITHUB_TOKEN` raises the GitHub rate limit.
- **Sort** by last qualifying drive, newest first (not engage %).
- **Engage %** = `engaged_time / not_in_park_time` (group and drill-down use the same sums). API `total_drive_time_s` is fallback only. Miles from API `distance`.


Click the drive **count** to expand that group (date, miles, engage %). The main view does not list every drive.


**Engaged time** is a qlog integral of `logMonoTime` deltas (gaps > 5s skipped): prefer `selfdriveState.enabled` (`Event` `@130`); else `controlsState.enabled` (`@19`, including `deprecated.enabled`). Bundled stub: `Event.valid @67` stays *outside* the union.


**Override** is the same integral where `selfdriveState.state == 4` (`OpenpilotState.overriding`) — Connect gray while engaged, as **seconds** (not a percent of engaged). `preEnabled` (1) is not counted. No usable selfdriveState samples (older controlsState-only logs) store NULL so they do not fill commit sums with fake zeros. `--reparse-engaged` backfills existing routes after this parser bump.


**Not-in-park** is `carState.gearShifter != park` with the same gap rules. Only `park` is excluded. No `carState` → API wall-clock. `parkingBrake` is unused. Cereal: `Event.carState @22`, `gearShifter @14`, `GearShifter.park @1`. A 0.0 park integral with engaged time is treated as missing (fall back to wall-clock).
