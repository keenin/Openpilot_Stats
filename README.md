# Openpilot usage (personal)

Private pipeline for **one driver**. Tracks which experimental/nightly openpilot commits you flash, using **your** engaged driving time as the quality signal. Not a fork of comma connect. Not community data.

The public artifact is a **static `index.html`**. The browser never talks to comma. Code in this repo stays private; you can share the Pages URL on Openpilot Discord.

## Architecture

```
Debian box (cron 03:00 PT)
  ├─ JWT + dongle_id from ~/.config/op-usage/credentials.env   # never in git
  ├─ GET comma API route metadata (git_commit, miles, times)
  ├─ GET /v1/route/{name}/files → qlogs → engaged time + not-in-park time
  ├─ SQLite cache (watermark + per-route engaged / not-in-park time)
  └─ write site/index.html → wrangler pages deploy
```

**Include a drive** only if length ≥ 1 mile **and** engaged time > 0.

**List a commit** only if it has ≥ 3 qualifying drives.

**Sort** commits by date of the **last** qualifying drive, newest first. The main table is not sorted by engage %.

**Engage %** = `engaged_time / not_in_park_time` (qlog integral of time the car is **not in Park**). Same formula for a commit (sums) and for a drill-down drive. Parked idling does not inflate the denominator. API wall-clock `total_drive_time_s` is stored for diagnostics / fallback only.

Click the drive **count** to expand that commit (date, miles, engage %). The main view does not list every drive.

### Engaged time

Computed from each drive’s **qlog** (decimated cereal events):

1. Prefer **`selfdriveState.enabled`** (Event union field `@130`, current openpilot).
2. If a qlog has no `selfdriveState` messages, fall back to **`controlsState.enabled`** (`@19` on ControlsState; cereal now exposes this as `controlsState.deprecated.enabled` — same wire field).

The bundled Cap’n Proto stub must keep **`Event.valid @67` outside the union**, matching cereal. A previous stub put `@67` in the union, so `which()` reported `selfdriveState` as `u129` and every route looked like `engaged=0` from `controlsState.enabled` (modern controlsd no longer sets that flag).

Integration uses `logMonoTime` deltas, not sample counts. Gaps > 5s are skipped so a missing segment is not counted as engaged.

Per-route engaged time and not-in-park time are **cached**. Old logs are not re-parsed. Nightly re-checks the last 24h in case uploads are still in flight (`maxqlog` grew). Each successful qlog parse is committed immediately so a multi-hour run can resume after a kill or crash.

### Not-in-park time (engage % denominator)

`not_in_park_time_s` is the integral of **`carState.gearShifter != park`**, same gap rules as engaged time.

Verified against upstream cereal (commaai/openpilot `log.capnp` + commaai/opendbc `car.capnp`, 2026-09):

| Field | Ordinal | Notes |
|-------|---------|--------|
| `Event.carState` | `@22` | `Car.CarState` |
| `CarState.gearShifter` | `@14` | enum `GearShifter` |
| `GearShifter.park` | `@1` | `unknown @0`, `drive @2`, `neutral @3`, `reverse @4`, `sport @5`, `low @6`, `brake @7`, `eco @8`, `manumatic @9` |

Only **`park`** is excluded. `unknown` and every other gear count as not-in-park. `CarState.parkingBrake` (`@39`) is the parking-brake switch — a different signal — and is not used.

If a qlog has no `carState` samples, the cached denominator falls back to API wall-clock `total_drive_time_s`.

### Reparse cached qlogs (required after parser or timing-field changes)

Routes already stored with `qlog_parsed=1` are skipped forever, including rows whose `not_in_park_time_s` is still NULL (pre–not-in-park cache). After upgrading, **invalidate and re-read** so both timing fields are recomputed from qlogs:

```bash
python3 -m op_usage backfill -v --reparse-engaged
```

`--reparse-engaged` clears `qlog_parsed` / timing fields for **routes this run will list**, then re-downloads those qlogs. `backfill` lists full history; `nightly` only lists the watermark − 24h window (historical rows stay intact). If the run is interrupted, resume with plain `backfill` or `nightly` (no flag) — leftover `qlog_parsed=0` rows are picked up. Do not delete the whole sqlite file unless you also want to redo route listing / the watermark.

Equivalent SQL to mark *every* row (same as `backfill --reparse-engaged` after a full list):

```sql
UPDATE drives
SET qlog_parsed = 0,
    engaged_time_s = NULL,
    engaged_source = NULL,
    not_in_park_time_s = NULL;
```

### Refresh `length_miles` (required once after the distance-field fix)

`generate` only reads sqlite. After a successful engaged-time reparse the table can still be empty if every cached `length_miles` is exactly 0 — the include rule is length ≥ 1 mile **and** engaged > 0. Owner diagnostics: engaged and `total_drive_time_s` look real; `length_miles` MIN=MAX=AVG=0.

Live `routes_segments` objects use **`distance`** (miles). Older OpenAPI docs and some payloads still use **`length`** (also miles). The pipeline used to read only `length`, so a current API response stored 0 miles for every route. Cached engaged times are fine.

**Metadata-only refresh** — does not pass `--reparse-engaged`, does not download qlogs, does not delete sqlite:

```bash
python3 -m op_usage backfill -v --metadata-only
```

That re-lists full history (`BACKFILL_START` → now), overwrites `length_miles` / times / git_* via `upsert_route_meta`, skips every qlog, and writes HTML. Plain `backfill` without the flag still downloads qlogs for any route that is not yet `qlog_parsed`. `nightly` only re-lists the watermark − 24h window, so it will not fix historical zeros.

There is no SQL rewrite for this: the miles are not stored under another column. Verbose logs print the first payload’s keys plus `distance`/`length` sample values, and `length_miles>0` / `>=1` counts. After it finishes:

```sql
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN engaged_time_s > 0 THEN 1 ELSE 0 END) AS engaged_gt0,
  SUM(CASE WHEN length_miles >= 1 AND engaged_time_s > 0 THEN 1 ELSE 0 END) AS qualifying,
  MIN(length_miles) AS min_mi, MAX(length_miles) AS max_mi
FROM drives
WHERE engaged_time_s > 0;
```

### Comma API (verified from public docs)

Base URL: `https://api.commadotai.com`  
Auth: `Authorization: JWT <token>` from [jwt.comma.ai](https://jwt.comma.ai) — **not** `Bearer`.

| Call | Role |
|------|------|
| `GET /v1/me` | Confirm the JWT |
| `GET /v1/devices/{dongleId}/routes_segments?start={ms}&end={ms}` | Route list + `git_*`, `distance` (miles; OpenAPI name `length`), segment times |
| `GET /v1/route/{routeName}/files` | Signed `qlogs[]` URLs. **Rate limit 5/min** |
| Signed blob GET | Download `qlog.bz2` / `qlog.zst` |

If a `routes_segments` payload looks like per-minute segments (`canonical_route_name`), those objects are grouped into routes. Cabana/connect use the same files API; this tool only pulls qlogs, never cameras.

### TODOs for the owner (cannot confirm without a live JWT)

- Token lifetime: if nightly gets **401**, mint a new JWT at jwt.comma.ai and update the credentials file. No refresh flow in v1.
- `routes_segments` window size: backfill uses 14-day chunks (`CHUNK_DAYS`). If a chunk looks truncated (~1000 rows), lower it.

## Setup (Debian)

### 1. Python

```bash
sudo apt install -y python3 python3-venv python3-pip capnproto libcapnp-dev
cd ~/Openpilot_Stats   # or wherever this repo lives
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`pycapnp` needs `capnproto` / `libcapnp-dev`. Optional: clone [openpilot](https://github.com/commaai/openpilot) and set `OPENPILOT_PATH` so qlog parsing uses cereal instead of the bundled stub schema.

### 2. JWT + dongle (outside the repo)

```bash
mkdir -p ~/.config/op-usage ~/.cache/op-usage
cp .env.example ~/.config/op-usage/credentials.env
chmod 600 ~/.config/op-usage/credentials.env
```

Fill in:

- **`COMMA_JWT`** — user token from https://jwt.comma.ai
- **`DONGLE_ID`** — Connect → your device (single dongle)

Do not put secrets in this git repo. `.gitignore` already drops `.env`, `credentials.env`, sqlite, and `site/`.

### 3. Demo (no JWT) — verify HTML

```bash
source .venv/bin/activate
python3 -m op_usage demo --out ./site --cache /tmp/op-usage-demo.sqlite
# open site/index.html
python3 -m pytest
```

### 4. First live backfill, then nightly

```bash
python3 -m op_usage backfill -v     # full history once (slow: qlogs + 5/min files cap)
python3 -m op_usage nightly -v      # watermark + last 24h only
python3 -m op_usage generate        # rebuild HTML from cache, no API
```

If a previous run cached `engaged_time_s=0`, or after upgrading to not-in-park engage %, add `--reparse-engaged` to `backfill` (see above).

If engaged times look good but `generate` still writes an empty table, `length_miles` is stale — run `backfill -v --metadata-only` (see “Refresh length_miles”).

### 5. Cloudflare Pages

1. Create a Pages project named `op-usage` (or set `CF_PAGES_PROJECT`).
2. `npx wrangler login` **or** put `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` in the credentials file.
3. After `generate` / `nightly` has produced `site/index.html`:

```bash
chmod +x scripts/deploy.sh scripts/nightly.sh
./scripts/deploy.sh
```

Uses `wrangler pages deploy ./site`. `wrangler.toml` records `pages_build_output_dir = "site"`. Public URL: [op-stats.keenin.com](https://op-stats.keenin.com). No VPS.

Workers static assets instead of Pages: see comments in `wrangler.toml`, then `npx wrangler deploy`.

### 6. Cron (03:00 America/Los_Angeles)

```bash
sudo cp cron/op-usage.cron.example /etc/cron.d/op-usage
# edit user + path; create /var/log/op-usage.log
```

`scripts/nightly.sh` = incremental fetch + deploy.

## Cache schema (SQLite)

Default path: `~/.cache/op-usage/op-usage.sqlite` (`CACHE_PATH`).

```
meta
  watermark_ms     max start_time of listed routes (incremental cursor)
  last_run_iso
  dongle_id
    schema_version        bumped to 3 with not_in_park_time_s;
                          does not auto-wipe qlog rows — use --reparse-engaged

drives             one row per route
  route_name PK
  start/end_time_utc_ms, length_miles
  total_drive_time_s               # API wall-clock (end-start); fallback only
  git_commit, git_branch, git_remote, maxqlog
  engaged_time_s, engaged_source   # cached; old qlogs not re-read
  not_in_park_time_s               # qlog gear integral; engage-% denominator
  qlog_parsed                      # 1 once parse succeeded (incl. 0 engaged)
```

Nightly fetch window: `watermark - 24h` → now. Recheck last day if `maxqlog` increased. First run (`watermark_ms` empty) or `backfill` starts at `BACKFILL_START` (default `2018-01-01`).

## Commands

`-v` / `--verbose` works **before or after** the subcommand (`backfill -v` or `-v backfill`).

| Command | What it does |
|---------|----------------|
| `python -m op_usage demo` | Fixture drives → HTML, no JWT |
| `python -m op_usage backfill` | Full history, parse uncached qlogs, write HTML |
| `python -m op_usage backfill --metadata-only` | Full history **metadata only** (refresh `length_miles`); no qlog downloads |
| `python -m op_usage backfill --reparse-engaged` | Full history: clear listed routes’ cached qlog fields, re-download qlogs |
| `python -m op_usage nightly` | Incremental + 24h recheck, write HTML |
| `python -m op_usage nightly --reparse-engaged` | Same window as nightly; reparse **listed** routes only (does not wipe older cache rows) |
| `python -m op_usage generate` | HTML from sqlite only |
| `python -m op_usage deploy` | `wrangler pages deploy` |
| `python -m op_usage deploy --dry-run` | Print the wrangler command |

## Owner checklist

1. JWT + dongle_id in `~/.config/op-usage/credentials.env`. A **401** means mint a new token at jwt.comma.ai — there is no refresh flow.
2. `pip install -e ".[dev]"` and `python -m op_usage demo` to confirm the page.
3. First live load (or after a parser change): `python -m op_usage backfill -v` (add `--reparse-engaged` when cached qlog fields are wrong). Qlog downloads are throttled (5/min files API).
4. Cloudflare Pages project + `./scripts/deploy.sh`. Public site: `op-stats.keenin.com`.
5. Cron example → `scripts/nightly.sh` (incremental fetch + deploy). Share the Pages URL, not this repo.

## Out of scope (v1)

Graphs, forking connect, live browser calls to comma, ranking as the primary view, other users’ data.
