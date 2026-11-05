"""Generate a single static index.html. No browser calls to comma."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from op_usage import MIN_DRIVES_PER_COMMIT, MIN_MILES
from op_usage.aggregate import (
    CommitRow,
    DriveView,
    WeightsLookup,
    aggregate_commits,
    is_master_branch,
)
from op_usage.cache import DriveRow
from op_usage.commute import partition_drives
from op_usage.weights import github_owner_repo


def render_site(
    commits: list[CommitRow],
    *,
    generated_at: datetime,
    display_tz: str,
    commute: list[CommitRow] | None = None,
    other: list[CommitRow] | None = None,
) -> str:
    """One table, or commute beside non-commute when ``commute`` is not None.

    ``commute is None`` keeps the original page: ``commits`` is the only
    table. When ``commute`` is a list (empty is fine), that list is the
    left column, ``other`` is the right, and rows share one group order.
    A missing group is a blank slot so the other side stays aligned.
    ``commits`` is the unsplit grouping used for the Combined line.
    """
    tz = ZoneInfo(display_tz)
    generated_local = generated_at.astimezone(tz)
    stamp = generated_local.strftime("%Y-%m-%d %H:%M %Z")
    if commute is None:
        body = _main_table(_commit_rows(commits, tz))
    else:
        other_rows = [] if other is None else other
        body = _split_columns(commute, other_rows, commits, tz)
    return _PAGE.format(stamp=html.escape(stamp), body=body)


def render_drives(
    drives: list[DriveRow],
    *,
    generated_at: datetime,
    display_tz: str,
    commute_route_ids: set[str] | None = None,
    weights_lookup: WeightsLookup | None = None,
    min_miles: float = MIN_MILES,
    min_drives: int = MIN_DRIVES_PER_COMMIT,
) -> str:
    """Group drives and render HTML.

    ``commute_route_ids is None`` (file missing) is one table. A set,
    including an empty one, splits commute vs non-commute and runs the
    same grouping rules on each subset. Combined is the unsplit groups:
    every drive the single table would have shown.
    """
    grouped = {
        "min_miles": min_miles,
        "min_drives": min_drives,
        "weights_lookup": weights_lookup,
    }
    commits = aggregate_commits(drives, **grouped)
    if commute_route_ids is None:
        return render_site(commits, generated_at=generated_at, display_tz=display_tz)
    commute_drives, other_drives = partition_drives(drives, commute_route_ids)
    return render_site(
        commits,
        generated_at=generated_at,
        display_tz=display_tz,
        commute=aggregate_commits(commute_drives, **grouped),
        other=aggregate_commits(other_drives, **grouped),
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


@dataclass(frozen=True)
class SectionStats:
    """Sums over the commit rows a table actually shows.

    Engage % is engaged / not_in_park over those sums, same as a CommitRow.
    """

    drive_count: int
    total_miles: float
    engaged_time_s: float
    override_time_s: float
    not_in_park_time_s: float

    @property
    def engage_pct(self) -> float:
        if self.not_in_park_time_s <= 0:
            return 0.0
        return 100.0 * self.engaged_time_s / self.not_in_park_time_s


def section_stats(commits: list[CommitRow]) -> SectionStats:
    return SectionStats(
        drive_count=sum(c.drive_count for c in commits),
        total_miles=sum(c.total_miles for c in commits),
        engaged_time_s=sum(c.engaged_time_s for c in commits),
        override_time_s=sum(c.override_time_s for c in commits),
        not_in_park_time_s=sum(c.not_in_park_time_s for c in commits),
    )


def format_section_stats(stats: SectionStats) -> str:
    label = "1 drive" if stats.drive_count == 1 else f"{stats.drive_count} drives"
    return (
        f"{label} · {format_miles(stats.total_miles)} mi · "
        f"{format_duration(stats.engaged_time_s)} engaged · "
        f"{format_duration(stats.override_time_s)} override · "
        f"{format_pct(stats.engage_pct)}"
    )


def format_date_range(first_ms: int, last_ms: int, tz: ZoneInfo) -> str:
    first = _local(first_ms, tz)
    last = _local(last_ms, tz)
    if first.date() == last.date():
        return first.strftime("%b %-d, %Y")
    if first.year == last.year:
        return f"{first.strftime('%b %-d')} → {last.strftime('%b %-d, %Y')}"
    return f"{first.strftime('%b %-d, %Y')} → {last.strftime('%b %-d, %Y')}"


def format_drive_date(ms: int, tz: ZoneInfo) -> str:
    return _local(ms, tz).strftime("%Y-%m-%d %H:%M")


def _local(ms: int, tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz)


def commit_url(remote: str, full_hash: str) -> str | None:
    if not full_hash:
        return None
    repo = github_owner_repo(remote)
    if not repo:
        return None
    return f"https://github.com/{repo}/commit/{full_hash}"


def _hash_title(commit: CommitRow) -> str:
    last = commit.git_commit
    first = commit.era_first_commit or last
    if first and last and first.lower() != last.lower():
        return f"{first} … {last}"
    return last


def _section_caption(title: str, stats: SectionStats) -> str:
    return f"{title} · {format_section_stats(stats)}"


def _commit_rows(commits: list[CommitRow], tz: ZoneInfo) -> str:
    return "\n".join(_commit_block(c, tz) for c in commits)


def group_key(row: CommitRow) -> tuple[str, str, str]:
    """Identity shared by the two sides of one group.

    Other branches are one row per SHA, so the SHA lines up even when the
    two sides show different branch labels. A master row is a weights era;
    the commit span on the row (first SHA, last SHA) is the key.
    """
    last = (row.git_commit or "").lower()
    first = (row.era_first_commit or row.git_commit or "").lower()
    if is_master_branch(row.git_branch):
        return ("master", first, last)
    return ("sha", last, "")


def aligned_group_rows(
    commute: list[CommitRow],
    other: list[CommitRow],
) -> list[tuple[CommitRow | None, CommitRow | None]]:
    """Pairs in one order: newest last-drive on either side first.

    The side that does not have a group is None. The page renders that as
    a blank slot so the following groups stay on the same row.
    """
    left = {group_key(row): row for row in commute}
    right = {group_key(row): row for row in other}

    def newest(key: tuple[str, str, str]) -> int:
        times = [side[key].last_drive_ms for side in (left, right) if key in side]
        return max(times) if times else 0

    keys = sorted(set(left) | set(right), key=lambda key: (-newest(key), key))
    return [(left.get(key), right.get(key)) for key in keys]


def _split_columns(
    commute: list[CommitRow],
    other: list[CommitRow],
    combined: list[CommitRow],
    tz: ZoneInfo,
) -> str:
    parts = [
        '    <div class="columns">',
        _section_label("commute", "Commute", commute),
        _section_label("other", "Non-commute", other),
        _colhead("commute"),
        _colhead("other"),
    ]
    for left, right in aligned_group_rows(commute, other):
        parts.append(_slot(left, tz, "commute"))
        parts.append(_slot(right, tz, "other"))
    parts.append("    </div>")
    combined_line = html.escape(_section_caption("Combined", section_stats(combined)))
    parts.append(f'    <p class="totals">{combined_line}</p>')
    return "\n".join(parts)


def _section_label(side: str, title: str, rows: list[CommitRow]) -> str:
    caption = html.escape(_section_caption(title, section_stats(rows)))
    return f'      <p class="section {side}">{caption}</p>'


def _colhead(side: str) -> str:
    return (
        f'      <div class="colhead {side}">\n'
        f'        <table class="main">\n'
        f"{_COLS}\n"
        f"{_THEAD}\n"
        f"        </table>\n"
        f"      </div>"
    )


def _slot(row: CommitRow | None, tz: ZoneInfo, side: str) -> str:
    if row is None:
        return f'      <div class="slot {side} empty"></div>'
    return (
        f'      <div class="slot {side}">\n'
        f'        <table class="main">\n'
        f"{_COLS}\n"
        f"          {_commit_block(row, tz)}\n"
        f"        </table>\n"
        f"      </div>"
    )


def _main_table(rows: str, caption: str | None = None) -> str:
    # Label sits outside the table. table.main uses overflow:hidden for the
    # rounded card, which would clip a <caption>.
    label = ""
    if caption:
        label = f'    <p class="section">{html.escape(caption)}</p>\n'
    return (
        f"{label}"
        f'    <table class="main">\n'
        f"{_THEAD}\n"
        f"      {rows}\n"
        f"    </table>"
    )


_COLS = """          <colgroup>
            <col style="width:20%">
            <col style="width:13%">
            <col style="width:17%">
            <col style="width:8%">
            <col style="width:9%">
            <col style="width:12%">
            <col style="width:11%">
            <col style="width:10%">
          </colgroup>"""


_THEAD = """      <thead>
        <tr>
          <th>Branch</th>
          <th>Commit</th>
          <th>Date range</th>
          <th>Drives</th>
          <th>Miles</th>
          <th>Engaged time</th>
          <th title="Connect gray / selfdriveState.overriding while engaged">Override</th>
          <th title="engaged time / time not in Park">Engage %</th>
        </tr>
      </thead>"""


def _commit_block(commit: CommitRow, tz: ZoneInfo) -> str:
    url = commit_url(commit.git_remote, commit.git_commit)
    short = html.escape(commit.short_hash)
    full = html.escape(_hash_title(commit))
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
        <td class="num">{format_duration(commit.override_time_s)}</td>
        <td class="num pct">{format_pct(commit.engage_pct)}</td>
      </tr>
      <tr class="detail" hidden>
        <td colspan="8">
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
    main:has(.columns) {{
      max-width: 1600px;
    }}
    .columns {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      column-gap: 1.5rem;
      row-gap: 0.55rem;
      align-items: stretch;
    }}
    .columns table.main {{
      table-layout: fixed;
      width: 100%;
    }}
    .columns table.main th,
    .columns table.main td {{
      padding-left: 0.4rem;
      padding-right: 0.4rem;
    }}
    .columns .branch {{
      word-break: normal;
      overflow-wrap: break-word;
    }}
    .slot.empty {{
      background: var(--card);
      border-radius: 10px;
      min-height: 2.75rem;
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
    p.section {{
      margin: 0 0.15rem 0.45rem;
      font-size: 0.95rem;
      font-weight: 650;
    }}
    .totals {{
      margin: 1rem 0.15rem 0;
      font-size: 0.95rem;
      font-weight: 650;
    }}
    @media (max-width: 720px) {{
      .columns {{
        display: flex;
        flex-direction: column;
      }}
      .columns .commute {{ order: 0; }}
      .columns .other {{ order: 1; }}
      .slot.empty {{ display: none; }}
      .colhead {{ display: none; }}
      p.section.other {{ margin-top: 1.35rem; }}
      table.main thead {{ display: none; }}
      table.main, table.main tbody, table.main tr, table.main td {{ display: block; width: 100%; }}
      table.main tr.detail[hidden] {{ display: none; }}
      table.main td {{ border-bottom: none; padding: 0.2rem 0.8rem; }}
      table.main tbody.commit {{ border-bottom: 1px solid var(--line); padding: 0.5rem 0; }}
    }}
  </style>
</head>
<body>
  <main>
{body}
    <p class="updated">Updated {stamp}</p>
    <p class="updated">Override is Connect gray (selfdriveState.overriding) while engaged, in seconds.</p>
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
