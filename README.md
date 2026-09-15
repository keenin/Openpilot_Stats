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

**Not-in-park** is `carState.gearShifter != park` with the same gap rules. Only `park` is excluded. No `carState` → API wall-clock. `parkingBrake` is unused. Cereal: `Event.carState @22`, `gearShifter @14`, `GearShifter.park @1`. A 0.0 park integral with engaged time is treated as missing (fall back to wall-clock).

Per-route timing is **cached**. Nightly **lists** watermark − 24h plus in-flight / recently-ended cached drives so settling `maxqlog` growth is visible. Completed drives from the last 7 days (a Friday route whose start is outside that window) get a tiny targeted metadata re-list. **Parse never hits Comma for qlog blobs** — it reads `~/.cache/op-usage/qlogs/<dongle_id>/<route_id>/<segment>.qlog` (override `OP_USAGE_QLOG_DIR`). If a route needs parse but local files are missing or incomplete vs `maxqlog`, it is skipped (`qlogs_missing_local`) and left unparsed so a later `sync-qlogs` can fill the gap. Ancient unparsed rows are parsed from disk without widening the list window. Successful parses commit immediately (parent process; workers never write sqlite). Independent routes parse in a process pool (`--jobs` / `OP_USAGE_JOBS`, default `min(32, CPU count)`). `--jobs 1` is serial.

The 3am cron (`scripts/nightly.sh`) is:

1. `nightly --metadata-only` — list / refresh sqlite metadata
2. `sync-qlogs` — Comma `/files` + CDN, **qlogs only**, skip routes already complete on disk
3. `nightly` — parse from local files + generate, then `deploy.sh`

`backfill` / `nightly` / `--reparse-engaged` do **not** download qlogs. `sync-qlogs` (alias `download-qlogs`) is the only path that calls `/files`. The on-disk layout matches the bulk fill at `~/.cache/op-usage/qlogs`, so existing files are reused with no re-fetch.

### Local qlog store

Default: `~/.cache/op-usage/qlogs` (same cache family as sqlite). Writes are atomic (`.partial` then rename). Completeness is `0.qlog` … `{maxqlog}.qlog` when `maxqlog` is known.

```bash
python3 -m op_usage sync-qlogs -v
python3 -m op_usage backfill -v          # parse local only; no /files
```

### Reparse cached qlogs

`qlog_parsed=1` rows are skipped (including NULL `not_in_park_time_s`):

```bash
python3 -m op_usage backfill -v --reparse-engaged
python3 -m op_usage backfill -v --reparse-engaged --jobs 15
```

Clears qlog fields for **routes this run will list**, then re-reads **local** files (no re-download). `backfill` = full history; `nightly --reparse-engaged` = watermark − 24h. Resume with plain `backfill` / `nightly`. Missing local files increment `qlogs_missing_local` and stay unparsed until `sync-qlogs`. Do not delete sqlite unless you also want to redo listing / the watermark.

`--jobs N` (or `OP_USAGE_JOBS`) runs N parse worker processes. Default is `min(32, CPU count)`. `--jobs 1` is the old one-core loop. Sync-qlogs stays sequential.

```sql
UPDATE drives
SET qlog_parsed = 0, engaged_time_s = NULL, engaged_source = NULL, not_in_park_time_s = NULL,
    weighted_engaged_time_s = NULL, steady_frac = NULL, parser_version = NULL;
```

### Refresh `length_miles`

`generate` only reads sqlite. Live `routes_segments` uses **`distance`** (miles); older payloads use **`length`**. Cached `length_miles=0` hides every drive under the include rule. There is no SQL rewrite.

```bash
python3 -m op_usage backfill -v --metadata-only
```

Re-lists full history; refreshes `length_miles` / times / git_* (blank `git_*` or `length_miles=0` does not overwrite a good cached value); skips qlog parse. `nightly` will not fix historical zeros.

```sql
SELECT COUNT(*) AS total,
  SUM(CASE WHEN engaged_time_s > 0 THEN 1 ELSE 0 END) AS engaged_gt0,
  SUM(CASE WHEN length_miles >= 1 AND engaged_time_s > 0 THEN 1 ELSE 0 END) AS qualifying,
  MIN(length_miles) AS min_mi, MAX(length_miles) AS max_mi
FROM drives WHERE engaged_time_s > 0;
```

### Comma API

Base: `https://api.commadotai.com`  
Auth: `Authorization: JWT <token>` from [jwt.comma.ai](https://jwt.comma.ai) — **not** `Bearer`.

| Call | Role |
|------|------|
| `GET /v1/me` | Confirm the JWT |
| `GET /v1/devices/{dongleId}/routes_segments?start={ms}&end={ms}` | Route list + `git_*`, `distance` (miles; OpenAPI name `length`), segment times |
| `GET /v1/route/{routeName}/files` | Signed `qlogs[]` URLs. **Rate limit 5/min**. Used only by `sync-qlogs`, never by parse/backfill |
| Signed blob GET | Download `qlog.bz2` / `qlog.zst` into the local store (`sync-qlogs` only) |

Per-minute `canonical_route_name` payloads are grouped into routes. Qlogs only, never cameras. Parse reads the local store; `/files` is the explicit sync step.

A nightly **401** means mint a new JWT. Backfill uses 14-day chunks (`CHUNK_DAYS`); if a chunk looks truncated (~1000 rows), lower it.

## Setup (Debian)

### 1. Python

```bash
sudo apt install -y python3 python3-venv python3-pip capnproto libcapnp-dev
cd ~/Openpilot_Stats
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`pycapnp` needs `capnproto` / `libcapnp-dev`. Optional: clone [openpilot](https://github.com/commaai/openpilot) and set `OPENPILOT_PATH` to use cereal instead of the stub.

### 2. JWT + dongle (outside the repo)

```bash
mkdir -p ~/.config/op-usage ~/.cache/op-usage ~/.cache/op-usage/qlogs
cp .env.example ~/.config/op-usage/credentials.env
chmod 600 ~/.config/op-usage/credentials.env
```

Set **`COMMA_JWT`** (https://jwt.comma.ai) and **`DONGLE_ID`**. Do not commit secrets.

### 3. Demo (no JWT)

```bash
source .venv/bin/activate
python3 -m op_usage demo --out /tmp/op-usage-demo-site --cache /tmp/op-usage-demo.sqlite
python3 -m pytest
```

Omit `--cache`/`--out` for temp paths. Demo refuses the live cache and `./site`.

### 4. Live backfill, then nightly

```bash
python3 -m op_usage backfill -v --metadata-only   # list history into sqlite
python3 -m op_usage sync-qlogs -v                 # fill ~/.cache/op-usage/qlogs
python3 -m op_usage backfill -v                   # parse local + generate
python3 -m op_usage nightly -v                    # incremental list + parse local
python3 -m op_usage generate
```

Reparse local files: `backfill -v --reparse-engaged` (add `--jobs N` to use N cores). Stale miles: `backfill -v --metadata-only`. Download gaps only: `sync-qlogs -v`.

### 5. Cloudflare Pages

1. Pages project `op-usage` (or `CF_PAGES_PROJECT`).
2. `npx wrangler login` **or** `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` in credentials.
3. After `generate` / `nightly` has produced `site/index.html`: `./scripts/deploy.sh`

Public URL: [op-stats.keenin.com](https://op-stats.keenin.com). Workers static assets: `wrangler.toml` then `npx wrangler deploy`.

### 6. Cron (03:00 America/Los_Angeles)

```bash
sudo cp cron/op-usage.cron.example /etc/cron.d/op-usage
# edit user + path; create /var/log/op-usage.log
```

## Commands

Default sqlite: `~/.cache/op-usage/op-usage.sqlite` (`CACHE_PATH`). Local qlogs: `~/.cache/op-usage/qlogs` (`OP_USAGE_QLOG_DIR`). Schema: `src/op_usage/cache.py` (`schema_version` 4). First run / `backfill` starts at `BACKFILL_START` (default `2018-01-01`). Bumping schema_version does **not** wipe qlog rows — use `--reparse-engaged` so old routes get engaged / not-in-park from **local** files. Parse workers: `--jobs` / `OP_USAGE_JOBS` (default `min(32, CPU count)`).

`-v` / `--verbose` works **before or after** the subcommand.

| Command | What it does |
|---------|----------------|
| `python -m op_usage demo` | Fixture drives → HTML, no JWT. Temp cache/site by default; refuses live sqlite and `./site` |
| `python -m op_usage sync-qlogs` | Download missing qlogs into `OP_USAGE_QLOG_DIR` (skips complete routes; `/files` 5/min). Alias: `download-qlogs` |
| `python -m op_usage backfill` | Full history metadata + parse **local** qlogs + HTML. Does not call `/files`. `--jobs N` for process-pool parse |
| `python -m op_usage backfill --metadata-only` | Full history metadata only (refresh `length_miles`); no qlog parse |
| `python -m op_usage backfill --reparse-engaged` | Full history: clear listed routes’ cached qlog fields, re-read local files |
| `python -m op_usage nightly` | Incremental list + 24h recheck, parse local qlogs, write HTML |
| `python -m op_usage nightly --reparse-engaged` | Same window as nightly; reparse **listed** routes from local files |
| `python -m op_usage generate` | HTML from sqlite only |
| `python -m op_usage deploy` | `wrangler pages deploy` |
| `python -m op_usage deploy --dry-run` | Print the wrangler command |

Out of scope (v1): graphs, forking connect, live browser calls to comma, ranking as the primary view, other users’ data.
