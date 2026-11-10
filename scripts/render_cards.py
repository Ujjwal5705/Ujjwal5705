#!/usr/bin/env python3
"""Render self-contained GitHub profile SVGs; optionally refresh their JSON input.

    python -m pip install -r requirements.txt
    python scripts/render_cards.py
    STATIC=1 python scripts/render_cards.py --output preview --preview
    GITHUB_TOKEN=... python scripts/render_cards.py --refresh-only

The only runtime dependency is fontTools' pyftsubset. Normal rendering is offline.
"""

from __future__ import annotations

import argparse
import base64
import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from html import escape
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SVG_NS = "http://www.w3.org/2000/svg"
WIDTH = 960
FONTS = {"heading": "TASAExplorer.ttf", "mono": "OxygenMono.ttf"}
QUERY = """query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}"""


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def load_days(payload: dict, username: str) -> list[tuple[date, int]]:
    """Reject incomplete snapshots: missing days must never become fake zeroes."""
    if payload.get("schema_version") != 1 or payload.get("username") != username:
        raise ValueError("Contribution JSON schema or username does not match the profile")
    days = []
    for row in payload["days"]:
        count = row["count"]
        if type(count) is not int or count < 0:
            raise ValueError("Contribution counts must be nonnegative integers")
        days.append((date.fromisoformat(row["date"]), count))
    if not days or len(days) > 371:
        raise ValueError("Expected a nonempty contribution calendar of at most one year plus an edge week")
    for previous, current in zip(days, days[1:]):
        if current[0] - previous[0] != timedelta(days=1):
            raise ValueError("Contribution dates must be sorted, unique, and contiguous")
    return days


def refresh(path: Path, username: str) -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise ValueError("--refresh requires GITHUB_TOKEN or GH_TOKEN; offline rendering does not")
    now = datetime.now(timezone.utc)
    first = now.date() - timedelta(days=364)
    variables = {"login": username, "from": f"{first.isoformat()}T00:00:00Z",
                 "to": now.isoformat(timespec="seconds")}
    request = Request("https://api.github.com/graphql", method="POST",
                      data=json.dumps({"query": QUERY, "variables": variables}).encode(),
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                               "User-Agent": "github-profile-cards", "Accept": "application/json"})
    with urlopen(request, timeout=45) as response:
        result = json.load(response)
    if result.get("errors"):
        raise ValueError("GitHub GraphQL returned errors; keeping the previous snapshot")
    weeks = result["data"]["user"]["contributionsCollection"]["contributionCalendar"]["weeks"]
    days = [{"date": day["date"], "count": day["contributionCount"]}
            for week in weeks for day in week["contributionDays"]
            if first.isoformat() <= day["date"] <= now.date().isoformat()]
    payload = {"schema_version": 1, "username": username,
               "generated_at": now.isoformat(timespec="seconds"),
               "source": "https://api.github.com/graphql", "days": days}
    validated = load_days(payload, username)
    if validated[0][0] != first or validated[-1][0] != now.date() or len(validated) != 365:
        raise ValueError("GitHub returned an incomplete calendar; keeping the previous snapshot")
    write_atomic(path, json.dumps(payload, indent=2) + "\n")
    print(f"Refreshed {len(days)} days from GitHub for {username}")


def statistics(days: list[tuple[date, int]]) -> dict:
    monthly = defaultdict(int)
    longest = run = 0
    for day, count in days:
        monthly[day.strftime("%Y-%m")] += count
        run = run + 1 if count else 0
        longest = max(longest, run)
    # A zero on the snapshot's last day gives yesterday's streak time to continue.
    streak = 0
    end = len(days) - 1 if days[-1][1] else len(days) - 2
    for index in range(end, -1, -1):
        if not days[index][1]:
            break
        streak += 1
    busiest = max(days, key=lambda item: (item[1], item[0]))
    return {"total": sum(count for _, count in days), "active": sum(count > 0 for _, count in days),
            "streak": streak, "longest": longest, "busiest": busiest,
            "monthly": list(monthly.items())}


def text(x, y, value, size=14, cls="mono", **attributes) -> str:
    attrs = " ".join(f'{key.replace("_", "-")}="{escape(str(value), quote=True)}"'
                     for key, value in attributes.items())
    return (f'<text x="{x}" y="{y}" class="{cls}" font-size="{size}" {attrs}>'
            f'{escape(str(value))}</text>')


def label(x, y, value, **attributes) -> str:
    attributes.setdefault("letter_spacing", ".8")
    return text(x, y, value, 11, "mono muted", **attributes)


def rule(y, x1=36, x2=924) -> str:
    return f'<path d="M{x1} {y}H{x2}" class="border" fill="none"/>'


def arrow(x, y, size=24) -> str:
    return (f'<g transform="translate({x} {y}) scale({size / 24})" class="arrow">'
            '<path d="M4 20 20 4M5 4h15v15" fill="none" stroke="currentColor" '
            'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></g>')


@lru_cache(maxsize=128)
def subset_font(family: str, characters: str) -> str:
    """Run pyftsubset for the exact glyphs used by this font in this card."""
    sibling = Path(sys.executable).with_name("pyftsubset")
    command = str(sibling) if sibling.exists() else shutil.which("pyftsubset")
    if not command:
        raise RuntimeError("pyftsubset is missing. Run: python -m pip install -r requirements.txt")
    with tempfile.TemporaryDirectory(prefix="profile-font-") as directory:
        source = Path(directory) / "characters.txt"
        output = Path(directory) / "subset.woff"
        source.write_text(characters, encoding="utf-8")
        subprocess.run([command, str(ROOT / "assets/fonts" / FONTS[family]),
                        f"--text-file={source}", f"--output-file={output}", "--flavor=woff",
                        "--no-hinting", "--no-recalc-timestamp", "--canonical-order",
                        "--no-ignore-missing-unicodes"], check=True, capture_output=True, text=True)
        encoded = base64.b64encode(output.read_bytes()).decode("ascii")
    weight = "100 900" if family == "heading" else "400"
    name = "TASA" if family == "heading" else "Oxygen"
    return (f"@font-face{{font-family:'{name}';font-weight:{weight};font-style:normal;"
            f"src:url('data:font/woff;base64,{encoded}') format('woff')}}")


def font_styles(body: str) -> str:
    glyphs = {family: set() for family in FONTS}
    tree = ET.fromstring(f'<svg xmlns="{SVG_NS}">{body}</svg>')
    for element in tree.iter(f"{{{SVG_NS}}}text"):
        family = "heading" if "heading" in element.get("class", "").split() else "mono"
        glyphs[family].update("".join(element.itertext()))
    return "\n".join(subset_font(family, "".join(sorted(chars)))
                     for family, chars in glyphs.items() if chars)


def smil_draw(path: str, delay: float, duration: float, static: bool, cls="accent", **attrs) -> str:
    attributes = " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items())
    animation = "" if static else (
        f'<animate attributeName="stroke-dashoffset" from="1" to="0" '
        f'begin="{delay:.2f}s" dur="{duration:.2f}s" fill="freeze"/>')
    return (f'<path d="{path}" class="smil-stroke {cls}" fill="none" pathLength="1" '
            f'stroke-dasharray="1" stroke-dashoffset="{0 if static else 1}" {attributes}>'
            f'{animation}</path>')


def card(title: str, description: str, body: str, height: int, delay: float, static: bool,
         width: int = WIDTH) -> str:
    styles = font_styles(body) + """
svg{--bg:#fefcf8;--fg:#1c1917;--muted:#57534e;--subtle:#79716b;--accent:#89937a;
    --border:#e7e5e4;--surface:#f5f5f4;--heat0:#eeece7;--heat1:#dde1d5;
    --heat2:#b7bfa8;--heat3:#89937a;--heat4:#5d674f;color:var(--fg)}
@media(prefers-color-scheme:dark){svg{--bg:#1c1917;--fg:#ededed;--muted:#a8a29e;
    --subtle:#a6a09b;--accent:#9da591;--border:#39342f;--surface:#292524;
    --heat0:#292524;--heat1:#42493a;--heat2:#626e52;--heat3:#89937a;--heat4:#9da591}}
text{fill:var(--fg)}.heading{font-family:'TASA',sans-serif;font-weight:600;letter-spacing:-.5px}
.hero-title{font-weight:650;letter-spacing:-1.2px}.body-copy{font-weight:400;letter-spacing:0}
.metric{font-weight:550;letter-spacing:-.6px;font-variant-numeric:tabular-nums}
.project-title{font-weight:550;letter-spacing:-.35px}
.mono{font-family:'Oxygen',monospace;font-weight:400}.muted{fill:var(--muted)}
.subtle{fill:var(--subtle)}.accent{stroke:var(--accent)}.accent-fill{fill:var(--accent)}
.border{stroke:var(--border);stroke-width:1}.surface{fill:var(--surface)}
.arrow{color:var(--accent)}.frame{fill:var(--bg);stroke:var(--border);stroke-width:1}
.heat0{fill:var(--heat0)}.heat1{fill:var(--heat1)}.heat2{fill:var(--heat2)}
.heat3{fill:var(--heat3)}.heat4{fill:var(--heat4)}
"""
    if not static:
        styles += f"""
@keyframes arrive{{from{{opacity:0;transform:translateY(4px)}}to{{opacity:1;transform:translateY(0)}}}}
@keyframes grow{{from{{transform:scaleY(0)}}to{{transform:scaleY(1)}}}}
@media(prefers-reduced-motion:no-preference){{
  .reveal{{animation:arrive .45s cubic-bezier(.2,.7,.2,1) {delay:.2f}s both}}
  .bar{{transform-box:fill-box;transform-origin:center bottom;
        animation:grow .7s cubic-bezier(.2,.7,.2,1) both;animation-delay:var(--delay)}}
}}
/* CSS !important overrides SMIL's presentation value, too. */
@media(prefers-reduced-motion:reduce){{
  *,*::before,*::after{{animation:none!important;transition:none!important}}
  .reveal{{opacity:1!important;transform:none!important}}
  .bar{{transform:none!important}}
  .smil-stroke{{stroke-dashoffset:0!important}}
}}
"""
    return (f'<svg xmlns="{SVG_NS}" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">\n'
            f'<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>\n'
            f'<style><![CDATA[{styles}]]></style>\n'
            f'<rect class="frame" x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="8"/>\n'
            f'<g class="reveal">{body}</g>\n</svg>\n')


def hero(profile: dict, static: bool, portrait: Path | None) -> str:
    body = label(40, 39, "SOFTWARE ENGINEER")
    body += text(920, 39, "@" + profile["username"], 11, "mono subtle", text_anchor="end")
    body += text(38, 102, profile["name"], 54, "heading hero-title")
    body += text(40, 140, profile["about"], 17, "heading body-copy muted")
    body += smil_draw("M40 174H920", .12, .8, static, stroke_width="1", opacity=".65")
    for x, caption, value in ((40, "EDUCATION", profile["education"]),
                              (388, "OPEN SOURCE", profile["open_source"]),
                              (748, "BASED IN", profile["location"])):
        body += label(x, 204, caption)
        body += text(x, 228, value, 14, "heading body-copy")
    if portrait:
        raw = portrait.read_bytes()
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif raw.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        else:
            raise ValueError("Portrait must be a PNG or JPEG so it can be embedded in an img-safe SVG")
        uri = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
        body += ('<defs><clipPath id="portrait"><circle cx="868" cy="98" r="36"/></clipPath></defs>'
                 f'<image href="{uri}" x="832" y="62" width="72" height="72" '
                 'preserveAspectRatio="xMidYMid slice" clip-path="url(#portrait)"/>')
    return card(profile["name"] + " — Software Engineer", profile["about"], body, 252, 0, static)


def tile(item: dict, index: int, static: bool) -> str:
    body = text(24, 42, item["title"], 24, "heading project-title") + arrow(269, 26, 17)
    body += text(24, 69, item["subtitle"], 13, "heading body-copy muted")
    body += text(24, 101, item["footer"], 10, "mono subtle")
    return card(item["title"], item["subtitle"], body, 124, .24 + index * .12, static, 312)


def activity(days: list[tuple[date, int]], stats: dict, static: bool) -> str:
    first, last = days[0][0], days[-1][0]
    body = text(36, 39, "GitHub activity", 26, "heading")
    body += text(36, 62, f'Contributions over the last {len(days)} days', 14, "heading body-copy muted")
    body += text(924, 39, f'{first:%d %b %Y} - {last:%d %b %Y}', 11, "mono subtle", text_anchor="end")
    busiest_date, busiest_count = stats["busiest"]
    metrics = [(f'{stats["total"]:,}', "CONTRIBUTIONS"),
               (f'{stats["streak"]} days', "CURRENT STREAK"),
               (str(stats["active"]), "ACTIVE DAYS"),
               (str(busiest_count), "BUSIEST DAY")]
    for index, (value, caption) in enumerate(metrics):
        x = 36 + index * 230
        body += text(x, 113, value, 31, "heading metric") + label(x, 137, caption)
        if index:
            body += f'<path d="M{x - 24} 88V143" class="border"/>'
    body += text(726, 155, busiest_date.strftime("%d %b %Y") if busiest_count else "No activity yet",
                 10, "mono subtle")
    body += label(36, 178, "MONTHLY CONTRIBUTIONS")
    body += text(924, 178, "First and last months may be partial", 10, "mono subtle", text_anchor="end")
    monthly = stats["monthly"]
    maximum = max(1, max(value for _, value in monthly))
    ceiling = max(10, math.ceil(maximum / 50) * 50)
    for ratio in (0, .5, 1):
        y = 292 - ratio * 90
        body += f'<path d="M65 {y}H924" class="border" stroke-dasharray="2 6"/>'
        body += text(48, y + 4, str(round(ceiling * ratio)), 10, "mono subtle", text_anchor="end")
    step = 852 / len(monthly)
    for index, (month, count) in enumerate(monthly):
        x = 72 + index * step
        height = count / ceiling * 90
        body += (f'<rect x="{x:.2f}" y="{292 - height:.2f}" width="{step - 18:.2f}" '
                 f'height="{height:.2f}" rx="2" class="bar accent-fill" '
                 f'style="--delay:{.9 + index * .04:.2f}s"><title>{month}: {count} contributions</title></rect>')
        center = x + (step - 18) / 2
        body += text(f"{center:.2f}", f"{282 - height:.2f}", count, 10, "mono muted", text_anchor="middle")
        body += text(f"{center:.2f}", 314, calendar.month_abbr[int(month[-2:])].upper(),
                     10, "mono muted", text_anchor="middle")
    body += rule(334)
    body += text(36, 357, f'Longest streak: {stats["longest"]} days', 10, "mono subtle")
    body += text(924, 357, f'Data through {last:%d %b %Y}', 10, "mono subtle", text_anchor="end")
    desc = (f'{stats["total"]} contributions from {first} to {last}. '
            f'Current streak {stats["streak"]} days; {stats["active"]} active days. '
            f'Busiest day: {busiest_date}, {busiest_count} contributions. '
            + "Monthly totals: " + ", ".join(f"{month}: {value}" for month, value in monthly))
    return card("GitHub contributions by month", desc, body, 376, .72, static)


def repository(repo: dict, index: int, static: bool) -> str:
    body = text(29, 45, f'{index + 1:02d}', 12, "mono subtle")
    body += '<path d="M66 27V130" class="border"/>'
    body += text(90, 46, repo["title"], 26, "heading project-title")
    body += text(924, 42, repo["category"], 10, "mono subtle", text_anchor="end")
    for line_index, value in enumerate(textwrap.wrap(repo["description"], width=108)):
        body += text(90, 77 + line_index * 21, value, 15, "heading body-copy muted")
    body += text(90, 130, " / ".join(repo["tags"]), 11, "mono subtle")
    body += arrow(900, 112, 18)
    return card(repo["title"], repo["description"], body, 156, 1.15 + index * .31, static)


def heat_level(count: int, maximum: int) -> int:
    return 0 if count == 0 else min(4, max(1, math.ceil(count / max(1, maximum) * 4)))


def graph(days: list[tuple[date, int]], stats: dict, delay: float, static: bool) -> str:
    body = text(36, 39, "Contribution calendar", 26, "heading")
    body += text(36, 62, "Daily contributions and weekly activity", 14, "heading body-copy muted")
    body += text(924, 39, f'{stats["active"]} active days', 11, "mono subtle", text_anchor="end")
    first_sunday = days[0][0] - timedelta(days=(days[0][0].weekday() + 1) % 7)
    columns = ((days[-1][0] - first_sunday).days // 7) + 1
    step = min(16, 854 / columns)
    size = step - 4
    start_x, start_y = 69, 106
    for row, name in ((1, "M"), (3, "W"), (5, "F")):
        body += text(36, start_y + row * step + 10, name, 10, "mono subtle")
    maximum = max(count for _, count in days)
    months = set()
    weekly = defaultdict(int)
    for day, count in days:
        offset = (day - first_sunday).days
        column, row = divmod(offset, 7)
        weekly[column] += count
        x, y = start_x + column * step, start_y + row * step
        month = day.strftime("%Y-%m")
        if month not in months and (day.day <= 7 or not months):
            # Do not overprint the first partial month's label with the next one.
            if column == 0 or column >= 3:
                body += text(f"{x:.2f}", 93, day.strftime("%b").upper(), 10, "mono subtle")
            months.add(month)
        body += (f'<rect x="{x:.2f}" y="{y:.2f}" width="{size:.2f}" height="{size:.2f}" '
                 f'rx="2" class="heat{heat_level(count, maximum)}">'
                 f'<title>{day.isoformat()}: {count} contributions</title></rect>')
    baseline = 246
    weekly_max = max(1, max(weekly.values()))
    points = [(start_x + index * step + size / 2, baseline - weekly[index] / weekly_max * 23)
              for index in range(columns)]
    path = "M" + " L".join(f"{x:.2f} {y:.2f}" for x, y in points)
    body += smil_draw(path, delay + .15, 1.2, static, stroke_width="1.8", stroke_linejoin="round")
    body += rule(264)
    body += text(36, 290, f'GitHub contribution data / {days[-1][0]:%d %b %Y}', 10, "mono subtle")
    body += text(748, 290, "Less", 10, "mono subtle")
    for level in range(5):
        body += f'<rect x="{789 + level * 17}" y="280" width="12" height="12" rx="2" class="heat{level}"/>'
    body += text(924, 290, "More", 10, "mono subtle", text_anchor="end")
    return card("GitHub contribution graph", f'A sage contribution calendar for {days[0][0]} through '
                f'{days[-1][0]}, with a weekly contribution line.', body, 310, delay, static)


def preview_html(profile: dict) -> str:
    images = []
    images.append('<img src="hero.svg" width="100%" alt="Ujjwal Sharma"/>')
    images.append('<div class="tiles">' + "\n".join(
        f'<a href="{escape(item["url"], quote=True)}"><img src="{item["file"]}.svg" '
        f'width="100%" alt="{escape(item["title"], quote=True)}"/></a>' for item in profile["tiles"]) + '</div>')
    images.append('<img src="activity.svg" width="100%" alt="Contributions by month"/>')
    for repo in profile["repos"]:
        name = f'repo-{repo["slug"].lower()}.svg'
        images.append(f'<a href="{repo["url"]}"><img src="{name}" width="100%" alt="{escape(repo["title"])}"/></a>')
    images.append('<img src="contributions.svg" width="100%" alt="Contribution calendar"/>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Ujjwal Sharma / Profile preview</title><style>'
            ':root{color-scheme:light dark}*{box-sizing:border-box}body{margin:0;'
            'padding:32px 16px;background:#fefcf8}main{max-width:960px;margin:auto;'
            'display:flex;flex-direction:column;gap:14px}img{display:block;height:auto}'
            '.tiles{display:flex;justify-content:space-between}.tiles a{width:32.5%}'
            '@media(prefers-color-scheme:dark){body{background:#1c1917}}</style><main>'
            + "\n".join(images) + '</main></html>\n')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=ROOT / "data/contributions.json")
    parser.add_argument("--profile", type=Path, default=ROOT / "data/profile.json")
    parser.add_argument("--output", type=Path, default=ROOT / "cards")
    parser.add_argument("--portrait", type=Path, help="Optional local PNG/JPEG; embedded as a data URI")
    parser.add_argument("--refresh", action="store_true", help="Refresh JSON from GitHub, then render")
    parser.add_argument("--refresh-only", action="store_true", help="Refresh JSON without rendering")
    parser.add_argument("--preview", action="store_true", help="Write an img-based index.html beside the cards")
    args = parser.parse_args()
    profile = read_json(args.profile)
    if args.refresh or args.refresh_only:
        refresh(args.data, profile["username"])
    if args.refresh_only:
        return
    static = os.environ.get("STATIC") == "1"
    days = load_days(read_json(args.data), profile["username"])
    stats = statistics(days)
    output = {"hero.svg": hero(profile, static, args.portrait)}
    for index, item in enumerate(profile["tiles"]):
        output[item["file"] + ".svg"] = tile(item, index, static)
    output["activity.svg"] = activity(days, stats, static)
    for index, repo in enumerate(profile["repos"]):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", repo["slug"]):
            raise ValueError("Repository slugs must be safe file names")
        output[f'repo-{repo["slug"].lower()}.svg'] = repository(repo, index, static)
    output["contributions.svg"] = graph(days, stats, 1.2 + len(profile["repos"]) * .31, static)
    # Build and validate everything before replacing any existing card.
    for svg in output.values():
        ET.fromstring(svg)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, svg in output.items():
        write_atomic(args.output / name, svg)
    if args.preview:
        write_atomic(args.output / "index.html", preview_html(profile))
    print(f'Rendered {len(output)} {"static" if static else "animated"} cards in {args.output}')


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Card generation failed: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stderr, file=sys.stderr)
        sys.exit(1)
