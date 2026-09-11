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
    tz = ZoneInfo(display_tz)
    generated_local = generated_at.astimezone(tz)
    stamp = generated_local.strftime("%Y-%m-%d %H:%M %Z")
    rows = "\n".join(_commit_block(c, tz) for c in commits)
    empty = ""
    if not commits:
        empty = """
        <p class="empty">No commits with ≥3 qualifying drives yet.
        Qualifying = ≥1 mile and engaged time &gt; 0.</p>
        """
    demo_banner = ""
    if mode == "demo":
        demo_banner = (
            '<p class="banner">Demo data — fixture drives, not this machine’s comma account.</p>'
        )
    return _PAGE.format(
        owner=html.escape(owner_name),
        stamp=html.escape(stamp),
        tz=html.escape(display_tz),
        count=len(commits),
        rows=rows,
        empty=empty,
        demo_banner=demo_banner,
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
        <td class="eng">
          <span>{format_duration(commit.engaged_time_s)}</span>
          <span class="pct">{format_pct(commit.engage_pct)}</span>
        </td>
      </tr>
      <tr class="detail" hidden>
        <td colspan="6">
          <table class="nested">
            <thead>
              <tr><th>Drive</th><th>Miles</th><th>Engage %</th></tr>
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
  <meta name="description" content="Personal openpilot usage — engaged time per flashed commit">
  <title>{owner} · openpilot usage</title>
  <style>
    :root {{
      --bg: #0f1218;
      --card: #171b24;
      --ink: #e7ebf0;
      --muted: #8b93a2;
      --line: #2a3140;
      --accent: #3dde7a;
      --accent-dim: #1b3d2a;
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
      padding: 2.2rem 1.2rem 4rem;
    }}
    header h1 {{
      font-size: 1.45rem;
      font-weight: 650;
      letter-spacing: -0.02em;
      margin: 0 0 0.35rem;
    }}
    header p, .note, .empty, footer {{
      color: var(--muted);
      font-size: 0.92rem;
    }}
    .banner {{
      background: var(--accent-dim);
      color: var(--accent);
      padding: 0.55rem 0.8rem;
      border-radius: 8px;
      font-size: 0.9rem;
    }}
    table.main {{
      width: 100%;
      border-collapse: collapse;
      background: var(--card);
      border-radius: 12px;
      overflow: hidden;
      margin-top: 1.2rem;
    }}
    table.main thead th {{
      text-align: left;
      font-size: 0.75rem;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      color: var(--muted);
      padding: 0.75rem 0.9rem;
      border-bottom: 1px solid var(--line);
    }}
    table.main td {{
      padding: 0.7rem 0.9rem;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    .branch {{
      font-weight: 600;
      word-break: break-all;
    }}
    .mono, .hash {{
      font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      font-size: 0.92rem;
    }}
    a.hash {{ color: var(--accent); text-decoration: none; }}
    a.hash:hover {{ text-decoration: underline; }}
    .num {{ font-variant-numeric: tabular-nums; }}
    .eng {{ display: flex; gap: 0.65rem; align-items: baseline; flex-wrap: wrap; }}
    .pct {{
      color: var(--accent);
      font-weight: 650;
      font-variant-numeric: tabular-nums;
    }}
    button.expand {{
      background: var(--btn);
      color: var(--ink);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 0.15rem 0.7rem;
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
      padding: 0.4rem 0.6rem;
      font-size: 0.88rem;
      border-bottom: 1px solid var(--line);
    }}
    footer {{ margin-top: 1.4rem; }}
    @media (max-width: 720px) {{
      table.main thead {{ display: none; }}
      table.main, table.main tbody, table.main tr, table.main td {{ display: block; width: 100%; }}
      table.main td {{ border-bottom: none; padding: 0.25rem 0.9rem; }}
      table.main tbody.commit {{ border-bottom: 1px solid var(--line); padding: 0.6rem 0; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <h1>{owner} · openpilot usage</h1>
      <p>Personal engaged-time by flashed commit. Private drives only — not community data.</p>
      {demo_banner}
    </header>
    <p class="note">Include a drive if it is ≥ 1 mile and engaged time &gt; 0.
      A commit is listed only with ≥ 3 such drives. Sorted by last qualifying drive, newest first.
      Engage % = engaged time / total drive time.</p>
    <table class="main">
      <thead>
        <tr>
          <th>Branch</th>
          <th>Git commit</th>
          <th>Date range</th>
          <th>Drives</th>
          <th>Total miles</th>
          <th>Engaged / %</th>
        </tr>
      </thead>
      {rows}
    </table>
    {empty}
    <footer>Generated {stamp} · {count} commits · {tz} · static page, no live comma API</footer>
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
