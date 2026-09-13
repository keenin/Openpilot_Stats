# Openpilot usage (personal)

Private pipeline for **one driver**. Tracks which experimental/nightly openpilot commits you flash, using **your** engaged driving time as the quality signal. Not a fork of comma connect. Not community data.

The public artifact is a **static `index.html`**. The browser never talks to comma. Code in this repo stays private; you can share the Pages URL on Openpilot Discord.

```
Debian box (cron 03:00 PT)
  ├─ JWT + dongle_id from ~/.config/op-usage/credentials.env   # never in git
  ├─ GET comma API route metadata (git_commit, miles, times)
  ├─ GET /v1/route/{name}/files → qlogs → engaged time + not-in-park time
  ├─ SQLite cache (watermark + per-route engaged / not-in-park time)
  └─ write site/index.html → wrangler pages deploy
```

- **Include a drive** only if length ≥ 1 mile **and** engaged time > 0.
- **List a group** only if it has ≥ 3 qualifying drives.
- **Other branches:** one row per git SHA.
- **`master` only** (`master`, `origin/master`, `refs/heads/master`): walk qualifying drives in start-time order and split when the driving-model weights fingerprint changes. A SHA that reappears after a different fingerprint is a **new interval**. Weights are the blob SHAs of driving ONNX/pkl files under `selfdrive/modeld/models` (then the older `openpilot/selfdrive/modeld/models` layout); resolved via the GitHub contents API from each drive’s `git_commit` + `git_remote`, then cached in sqlite (including confirmed misses). UI / cars / CI commits do not split the group — only a weights change does. Unknown fingerprints stay one-row-per-interval (never merged together). The Commit cell is the last SHA in the era (tooltip: first … last). Optional `GITHUB_TOKEN` in credentials.env raises the GitHub rate limit.
- **Sort** by date of the last qualifying drive, newest first (not by engage %).
- **Engage %** = `engaged_time / not_in_park_time` (qlog time the car is **not in Park**). Same formula for a group (sums) and a drill-down drive. API `total_drive_time_s` is fallback / diagnostics only. Miles come from API `distance`.

Click the drive **count** to expand that group (date, miles, engage %). The main view does not list every drive.

**Engaged time** is a qlog integral of `logMonoTime` deltas (gaps > 5s skipped): prefer `selfdriveState.enabled` (`Event` `@130`); if that message is absent, `controlsState.enabled` (`@19`, including cereal’s `deprecated.enabled`). The bundled Cap’n Proto stub must keep `Event.valid @67` *outside* the union.

**Not-in-park** is `carState.gearShifter != park` with the same gap rules. Only `park` is excluded (`unknown` and every other gear count). No `carState` samples → fall back to API wall-clock. `CarState.parkingBrake` is a different signal and is not used. Cereal map: `Event.carState @22`, `gearShifter @14`, `GearShifter.park @1`.

Per-route timing is **cached**. Nightly re-parses when `maxqlog` grew (any age), or when the route is still in-flight / recently ended (`end_time` in the last 24h). Late-appearing or previously unparsed cached routes are not stranded just because their `start_time` is older than the watermark window. Each successful parse is committed immediately so a long run can resume.

### Reparse cached qlogs

After a parser or timing-field change, routes with `qlog_parsed=1` are skipped (including old rows whose `not_in_park_time_s` is still NULL):

```bash
python3 -m op_usage backfill -v --reparse-engaged
```

Clears cached qlog fields for **routes this run will list**, then re-downloads those qlogs. `backfill` lists full history; `nightly --reparse-engaged` only the watermark − 24h window. Resume an interrupted run with plain `backfill` / `nightly`. Do not delete sqlite unless you also want to redo listing / the watermark.

```sql
UPDATE drives
SET qlog_parsed = 0, engaged_time_s = NULL, engaged_source = NULL, not_in_park_time_s = NULL;
```

### Refresh `length_miles`

`generate` only reads sqlite. Live `routes_segments` uses **`distance`** (miles); older docs/payloads use **`length`**. If every cached `length_miles` is 0, the include rule hides every drive (engaged times can still look real). There is no SQL rewrite — miles are not stored under another column.

```bash
python3 -m op_usage backfill -v --metadata-only
```

Re-lists full history and refreshes `length_miles` / times / git_* (last-known-good: a blank `git_*` or `length_miles=0` does not overwrite a good cached value), skips qlogs. Plain `backfill` still downloads unparsed qlogs. `nightly` will not fix historical zeros. Verbose logs print payload keys plus `distance` / `length` samples.

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
| `GET /v1/route/{routeName}/files` | Signed `qlogs[]` URLs. **Rate limit 5/min** |
| Signed blob GET | Download `qlog.bz2` / `qlog.zst` |

Per-minute `canonical_route_name` payloads are grouped into routes. This tool only pulls qlogs, never cameras.

A nightly **401** means mint a new JWT (no refresh flow). Backfill uses 14-day chunks (`CHUNK_DAYS`); if a chunk looks truncated (~1000 rows), lower it.

## Setup (Debian)

### 1. Python

```bash
sudo apt install -y python3 python3-venv python3-pip capnproto libcapnp-dev
cd ~/Openpilot_Stats
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`pycapnp` needs `capnproto` / `libcapnp-dev`. Optional: clone [openpilot](https://github.com/commaai/openpilot) and set `OPENPILOT_PATH` so parsing uses cereal instead of the bundled stub.

### 2. JWT + dongle (outside the repo)

```bash
mkdir -p ~/.config/op-usage ~/.cache/op-usage
cp .env.example ~/.config/op-usage/credentials.env
chmod 600 ~/.config/op-usage/credentials.env
```

Set **`COMMA_JWT`** (https://jwt.comma.ai) and **`DONGLE_ID`** (Connect → your device). Do not commit secrets; `.gitignore` already drops `.env`, `credentials.env`, sqlite, and `site/`.

### 3. Demo (no JWT)

```bash
source .venv/bin/activate
python3 -m op_usage demo --out /tmp/op-usage-demo-site --cache /tmp/op-usage-demo.sqlite
python3 -m pytest
# omit --cache/--out to write temp paths; demo refuses the live cache and ./site
```

### 4. First live backfill, then nightly

```bash
python3 -m op_usage backfill -v     # full history once (slow: qlogs + 5/min files cap)
python3 -m op_usage nightly -v      # watermark + last 24h only
python3 -m op_usage generate        # rebuild HTML from cache, no API
```

Reparse: `backfill -v --reparse-engaged`. Stale miles: `backfill -v --metadata-only`.

### 5. Cloudflare Pages

1. Pages project `op-usage` (or `CF_PAGES_PROJECT`).
2. `npx wrangler login` **or** `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` in credentials.
3. After `generate` / `nightly` has produced `site/index.html`: `./scripts/deploy.sh`

Uses `wrangler pages deploy ./site`. Public URL: [op-stats.keenin.com](https://op-stats.keenin.com). Workers static assets: see `wrangler.toml`, then `npx wrangler deploy`.

### 6. Cron (03:00 America/Los_Angeles)

```bash
sudo cp cron/op-usage.cron.example /etc/cron.d/op-usage
# edit user + path; create /var/log/op-usage.log
```

`scripts/nightly.sh` = incremental fetch + deploy.

## Cache and commands

Default sqlite: `~/.cache/op-usage/op-usage.sqlite` (`CACHE_PATH`). Schema is in `src/op_usage/cache.py` (`schema_version` 3). Nightly window: `watermark - 24h` → now. First run or `backfill` starts at `BACKFILL_START` (default `2018-01-01`). Bumping schema_version does **not** wipe qlog rows — use `--reparse-engaged`.

`-v` / `--verbose` works **before or after** the subcommand.

| Command | What it does |
|---------|----------------|
| `python -m op_usage demo` | Fixture drives → HTML, no JWT. Defaults to temp cache/site; refuses the live sqlite and `./site` |
| `python -m op_usage backfill` | Full history, parse uncached qlogs, write HTML |
| `python -m op_usage backfill --metadata-only` | Full history **metadata only** (refresh `length_miles`); no qlog downloads |
| `python -m op_usage backfill --reparse-engaged` | Full history: clear listed routes’ cached qlog fields, re-download qlogs |
| `python -m op_usage nightly` | Incremental + 24h recheck, write HTML |
| `python -m op_usage nightly --reparse-engaged` | Same window as nightly; reparse **listed** routes only |
| `python -m op_usage generate` | HTML from sqlite only |
| `python -m op_usage deploy` | `wrangler pages deploy` |
| `python -m op_usage deploy --dry-run` | Print the wrangler command |

Out of scope (v1): graphs, forking connect, live browser calls to comma, ranking as the primary view, other users’ data.
