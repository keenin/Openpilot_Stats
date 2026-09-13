"""Generate a single static index.html. No browser calls to comma."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from op_usage.aggregate import CommitRow, DriveView

GITHUB_SSH = re.compile(r"^git@github\.com:(.+?)(?:\.git)?$")
GITHUB_HTTPS = re.compile(r"^https://github\.com/(.+?)(?:\.git)?$")


def render_site(
    commits: list[CommitRow],
    *,
    owner_name: str,
    generated_at: datetime,
    display_tz: str,
    mode: str = "live",
) -> str:
    del owner_name, mode  # not shown; page is table + last-updated only
    tz = ZoneInfo(display_tz)
    generated_local = generated_at.astimezone(tz)
    stamp = generated_local.strftime("%Y-%m-%d %H:%M %Z")
    rows = "\n".join(_commit_block(c, tz) for c in commits)
    return _PAGE.format(
        stamp=html.escape(stamp),
        rows=rows,
    )


def write_site(path: Path, html_text: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    index = path / "index.html"
    index.write_text(html_text, encoding="utf-8")
    return index


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_pct(pct: float) -> str:
    return f"{pct:.1f}%"


def format_miles(miles: float) -> str:
    return f"{miles:,.1f}"


def format_date_range(first_ms: int, last_ms: int, tz: ZoneInfo) -> str:
    first = datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc).astimezone(tz)
    last = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).astimezone(tz)
    if first.date() == last.date():
        return first.strftime("%b %-d, %Y")
    if first.year == last.year:
        return f"{first.strftime('%b %-d')} → {last.strftime('%b %-d, %Y')}"
    return f"{first.strftime('%b %-d, %Y')} → {last.strftime('%b %-d, %Y')}"


def format_drive_date(ms: int, tz: ZoneInfo) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M")


def commit_url(remote: str, full_hash: str) -> str | None:
    if not remote or not full_hash:
        return None
    repo = None
    m = GITHUB_SSH.match(remote.strip())
    if m:
        repo = m.group(1)
    m = GITHUB_HTTPS.match(remote.strip())
    if m:
        repo = m.group(1)
    if not repo:
        return None
    return f"https://github.com/{repo}/commit/{full_hash}"


def _commit_block(commit: CommitRow, tz: ZoneInfo) -> str:
    url = commit_url(commit.git_remote, commit.git_commit)
    short = html.escape(commit.short_hash)
    full = html.escape(commit.git_commit)
    if url:
        hash_html = (
            f'<a class="hash" href="{html.escape(url)}" title="{full}" '
            f'target="_blank" rel="noopener noreferrer">{short}</a>'
        )
    else:
        hash_html = f'<span class="hash" title="{full}">{short}</span>'
    cid = html.escape(commit.git_commit)
    nested = "\n".join(_drive_row(d, tz) for d in reversed(commit.drives))
    return f"""
    <tbody class="commit" data-commit="{cid}">
      <tr>
        <td class="branch">{html.escape(commit.git_branch)}</td>
        <td class="mono">{hash_html}</td>
        <td>{html.escape(format_date_range(commit.first_drive_ms, commit.last_drive_ms, tz))}</td>
        <td>
          <button type="button" class="expand" aria-expanded="false" data-target="{cid}">
            {commit.drive_count}
          </button>
        </td>
        <td class="num">{format_miles(commit.total_miles)}</td>
        <td class="num">{format_duration(commit.engaged_time_s)}</td>
        <td class="num pct">{format_pct(commit.engage_pct)}</td>
      </tr>
      <tr class="detail" hidden>
        <td colspan="7">
          <table class="nested">
            <thead>
              <tr><th>Drive</th><th>Miles</th><th title="engaged time / time not in Park">Engage %</th></tr>
            </thead>
            <tbody>
              {nested}
            </tbody>
          </table>
        </td>
      </tr>
    </tbody>
    """


def _drive_row(drive: DriveView, tz: ZoneInfo) -> str:
    return (
        f"<tr>"
        f"<td>{html.escape(format_drive_date(drive.start_time_utc_ms, tz))}</td>"
        f"<td class='num'>{format_miles(drive.length_miles)}</td>"
        f"<td class='num'>{format_pct(drive.engage_pct)}</td>"
        f"</tr>"
    )


_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Openpilot Stats</title>
  <style>
    :root {{
      --bg: #0f1218;
      --card: #171b24;
      --ink: #e7ebf0;
      --muted: #8b93a2;
      --line: #2a3140;
      --accent: #3dde7a;
      --btn: #222838;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: var(--bg);
      color: var(--ink);
      line-height: 1.45;
    }}
    main {{
      max-width: 1080px;
      margin: 0 auto;
      padding: 1.5rem 1rem 2.5rem;
    }}
    table.main {{
      width: 100%;
      border-collapse: collapse;
      background: var(--card);
      border-radius: 10px;
      overflow: hidden;
    }}
    table.main thead th {{
      text-align: left;
      font-size: 0.75rem;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 600;
      padding: 0.65rem 0.8rem;
      border-bottom: 1px solid var(--line);
    }}
    table.main td {{
      padding: 0.6rem 0.8rem;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    .branch {{ word-break: break-all; }}
    .mono, .hash {{
      font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      font-size: 0.92rem;
    }}
    a.hash {{ color: var(--accent); text-decoration: none; }}
    a.hash:hover {{ text-decoration: underline; }}
    .num {{ font-variant-numeric: tabular-nums; }}
    .pct {{ color: var(--accent); font-weight: 650; }}
    button.expand {{
      background: var(--btn);
      color: var(--ink);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 0.1rem 0.65rem;
      cursor: pointer;
      font: inherit;
      font-variant-numeric: tabular-nums;
    }}
    button.expand:hover, button.expand[aria-expanded="true"] {{
      border-color: var(--accent);
      color: var(--accent);
    }}
    table.nested {{
      width: 100%;
      border-collapse: collapse;
      background: #12161e;
      border-radius: 8px;
    }}
    table.nested th, table.nested td {{
      padding: 0.35rem 0.55rem;
      font-size: 0.88rem;
      border-bottom: 1px solid var(--line);
    }}
    .updated {{
      color: var(--muted);
      font-size: 0.8rem;
      margin: 0.7rem 0.15rem 0;
    }}
    @media (max-width: 720px) {{
      table.main thead {{ display: none; }}
      table.main, table.main tbody, table.main tr, table.main td {{ display: block; width: 100%; }}
      table.main td {{ border-bottom: none; padding: 0.2rem 0.8rem; }}
      table.main tbody.commit {{ border-bottom: 1px solid var(--line); padding: 0.5rem 0; }}
    }}
  </style>
</head>
<body>
  <main>
    <table class="main">
      <thead>
        <tr>
          <th>Branch</th>
          <th>Commit</th>
          <th>Date range</th>
          <th>Drives</th>
          <th>Miles</th>
          <th>Engaged time</th>
          <th title="engaged time / time not in Park">Engage %</th>
        </tr>
      </thead>
      {rows}
    </table>
    <p class="updated">Updated {stamp}</p>
  </main>
  <script>
    document.querySelectorAll("button.expand").forEach(function (btn) {{
      btn.addEventListener("click", function () {{
        var row = btn.closest("tbody").querySelector("tr.detail");
        var open = btn.getAttribute("aria-expanded") === "true";
        btn.setAttribute("aria-expanded", open ? "false" : "true");
        if (row) row.hidden = open;
      }});
    }});
  </script>
</body>
</html>
"""
