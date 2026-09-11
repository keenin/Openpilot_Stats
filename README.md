# Openpilot usage (personal)

Private pipeline for **one driver**. Tracks which experimental/nightly openpilot commits you flash, using **your** engaged driving time as the quality signal. Not a fork of comma connect. Not community data.

The public artifact is a **static `index.html`**. The browser never talks to comma. Code in this repo stays private; you can share the Pages URL on Openpilot Discord.

## Architecture

```
Debian box (cron 03:00 PT)
  ├─ JWT + dongle_id from ~/.config/op-usage/credentials.env   # never in git
  ├─ GET comma API route metadata (git_commit, miles, times)
  ├─ GET /v1/route/{name}/files → qlogs → engaged time
  ├─ SQLite cache (watermark + per-route engaged time)
  └─ write site/index.html → wrangler pages deploy
```

**Include a drive** only if length ≥ 1 mile **and** engaged time > 0.

**List a commit** only if it has ≥ 3 qualifying drives.

**Sort** commits by date of the **last** qualifying drive, newest first. The main table is not sorted by engage %.

**Engage %** = `engaged_time / total_drive_time` (wall-clock route duration from API start/end timestamps). Same formula for a commit (sums) and for a drill-down drive.

Click the drive **count** to expand that commit (date, miles, engage %). The main view does not list every drive.

### Engaged time

Computed from each drive’s **qlog** (decimated cereal events):

1. Prefer **`selfdriveState.enabled`** (Event union field `@130`, current openpilot).
2. If a qlog has no `selfdriveState` messages, fall back to **`controlsState.enabled`** (`@19` on ControlsState; cereal now exposes this as `controlsState.deprecated.enabled` — same wire field).

The bundled Cap’n Proto stub must keep **`Event.valid @67` outside the union**, matching cereal. A previous stub put `@67` in the union, so `which()` reported `selfdriveState` as `u129` and every route looked like `engaged=0` from `controlsState.enabled` (modern controlsd no longer sets that flag).

Integration uses `logMonoTime` deltas, not sample counts. Gaps > 5s are skipped so a missing segment is not counted as engaged.

Per-route engaged time is **cached**. Old logs are not re-parsed. Nightly re-checks the last 24h in case uploads are still in flight (`maxqlog` grew).

### Reparse cached zeros (required once after the schema fix)

Routes already stored with `qlog_parsed=1` and `engaged_time_s=0` are skipped forever. After upgrading, **invalidate and re-read**:

```bash
python3 -m op_usage backfill -v --reparse-engaged
```

`--reparse-engaged` also works on `nightly`. Equivalent SQL on `~/.cache/op-usage/op-usage.sqlite`:

```sql
UPDATE drives
SET qlog_parsed = 0, engaged_time_s = NULL, engaged_source = NULL;
```

Then run `backfill` or `nightly` as usual (without the flag). Do not delete the whole sqlite file unless you also want to redo route listing / the watermark.

### Comma API (verified from public docs)

Base URL: `https://api.commadotai.com`  
Auth: `Authorization: JWT <token>` from [jwt.comma.ai](https://jwt.comma.ai) — **not** `Bearer`.

| Call | Role |
|------|------|
| `GET /v1/me` | Confirm the JWT |
| `GET /v1/devices/{dongleId}/routes_segments?start={ms}&end={ms}` | Route list + `git_*`, `length` (miles), segment times |
| `GET /v1/route/{routeName}/files` | Signed `qlogs[]` URLs. **Rate limit 5/min** |
| Signed blob GET | Download `qlog.bz2` / `qlog.zst` |

Fallback listing (if a payload looks like per-minute segments): `GET /v1/devices/{dongleId}/segments?from=&to=`, grouped by `canonical_route_name`.

Cabana/connect use the same files API; this tool only pulls qlogs, never cameras.

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

If a previous run cached `engaged_time_s=0` for every route, add `--reparse-engaged` to `backfill` (see above).

### 5. Cloudflare Pages

1. Create a Pages project named `op-usage` (or set `CF_PAGES_PROJECT`).
2. `npx wrangler login` **or** put `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` in the credentials file.
3. After `generate` / `nightly` has produced `site/index.html`:

```bash
chmod +x scripts/deploy.sh scripts/nightly.sh
./scripts/deploy.sh
```

Uses `wrangler pages deploy ./site`. `wrangler.toml` records `pages_build_output_dir = "site"`. Custom domain later — share the `*.pages.dev` URL for now. No VPS.

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
    schema_version        bumped to 2 with the Event.valid-@67 stub fix;
                          does not auto-wipe engaged rows — use --reparse-engaged

drives             one row per route
  route_name PK
  start/end_time_utc_ms, length_miles, total_drive_time_s
  git_commit, git_branch, git_remote, maxqlog
  engaged_time_s, engaged_source   # cached; old qlogs not re-read
  qlog_parsed                      # 1 once parse succeeded (incl. 0 engaged)
```

Nightly fetch window: `watermark - 24h` → now. Recheck last day if `maxqlog` increased. First run (`watermark_ms` empty) or `backfill` starts at `BACKFILL_START` (default `2018-01-01`).

## Commands

| Command | What it does |
|---------|----------------|
| `python -m op_usage demo` | Fixture drives → HTML, no JWT |
| `python -m op_usage backfill` | Full history, parse uncached qlogs, write HTML |
| `python -m op_usage backfill --reparse-engaged` | Same, but clear cached engaged times first (re-download qlogs) |
| `python -m op_usage nightly` | Incremental + 24h recheck, write HTML |
| `python -m op_usage nightly --reparse-engaged` | Incremental listing, but reparse every listed route’s qlogs |
| `python -m op_usage generate` | HTML from sqlite only |
| `python -m op_usage deploy` | `wrangler pages deploy` |
| `python -m op_usage deploy --dry-run` | Print the wrangler command |

## Next steps (owner)

1. Put **JWT + dongle_id** in `~/.config/op-usage/credentials.env`.
2. `pip install -e ".[dev]"` and `python -m op_usage demo` to confirm the page.
3. `python -m op_usage backfill -v` on the Debian box (leave it running; qlog downloads are throttled).
4. Create the Cloudflare Pages project, `wrangler login`, `./scripts/deploy.sh`.
5. Install the cron example. Share the Pages URL, not this repo.

## Out of scope (v1)

Graphs, forking connect, live browser calls to comma, ranking as the primary view, other users’ data.
