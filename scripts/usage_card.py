#!/usr/bin/env python3
"""Export local usage logs and render GitHub profile SVG cards."""

import argparse
import hashlib
import json
import math
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import NamedTuple
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo


class Record(NamedTuple):
    ts_ms: int
    provider: str
    model: str
    session: str
    input: int
    cache_read: int
    cache_write: int
    output: int

    @property
    def tokens(self) -> int:
        return self.input + self.cache_read + self.cache_write + self.output


def parse_claude(root: Path, since_ms: int) -> list[Record]:
    records: dict[str, Record] = {}
    root = root.expanduser().resolve()
    if os.name == "nt":
        root = Path("\\\\?\\" + str(root))
    for path in sorted(root.rglob("*.jsonl")):
        if path.stat().st_mtime * 1000 < since_ms:
            continue
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    message = event["message"]
                    usage = message["usage"]
                    model = message["model"]
                    if not usage or not model or model == "<synthetic>":
                        continue
                    ts_ms = int(datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp() * 1000)
                    key = f'{message["id"]}:{event.get("requestId", "")}'
                    records[key] = Record(
                        ts_ms, "claude", model, event["sessionId"],
                        int(usage.get("input_tokens", 0)),
                        int(usage.get("cache_read_input_tokens", 0)),
                        int(usage.get("cache_creation_input_tokens", 0)),
                        int(usage.get("output_tokens", 0)),
                    )
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
    return [rec for rec in records.values() if rec.ts_ms >= since_ms]


def parse_codex(root: Path, since_ms: int) -> list[Record]:
    records = []
    root = root.expanduser().resolve()
    if os.name == "nt":
        root = Path("\\\\?\\" + str(root))
    for path in sorted(root.rglob("*.jsonl")):
        if path.stat().st_mtime * 1000 < since_ms:
            continue
        session = ""
        model = None
        previous_total = None
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                    payload = event["payload"]
                    if event["type"] == "session_meta":
                        session = session or payload["id"]  # forks append the parent's meta
                    elif event["type"] == "turn_context":
                        model = payload.get("model")
                    elif event["type"] == "event_msg" and payload.get("type") == "token_count":
                        info = payload.get("info")
                        if info is None:
                            continue
                        total = info["total_token_usage"]["total_tokens"]
                        duplicate = total == previous_total
                        previous_total = total
                        if duplicate or not model:
                            continue
                        usage = info["last_token_usage"]
                        cached = int(usage.get("cached_input_tokens", 0))
                        write = int(usage.get("cache_write_input_tokens", 0))
                        ts_ms = int(datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp() * 1000)
                        if ts_ms >= since_ms:
                            records.append(Record(
                                ts_ms, "codex", model, session,
                                max(0, int(usage.get("input_tokens", 0)) - cached - write),
                                cached, write, int(usage.get("output_tokens", 0)),
                            ))
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
    return records


def load_rates(rates_path: Path) -> dict[str, dict]:
    with rates_path.open(encoding="utf-8") as stream:
        return json.load(stream)["document"]


def record_cost(rec: Record, rates: dict) -> float:
    rate = rates.get(rec.model) or {}
    return float(
        rec.input * (rate.get("input_cost_per_token") or 0)
        + rec.cache_read * (rate.get("cache_read_input_token_cost") or 0)
        + rec.cache_write * (rate.get("cache_creation_input_token_cost") or 0)
        + rec.output * (rate.get("output_cost_per_token") or 0)
    )


def summarize(rows: list[list], start: date, days: int) -> dict:
    rows = [row for row in rows if start <= date.fromisoformat(row[0]) < start + timedelta(days=days)]
    providers: dict[str, dict] = {}
    models: dict[tuple[str, str], dict] = {}
    daily: dict[str, list[int]] = {}
    total = sum(sum(row[4:8]) for row in rows)
    names = {"claude": "Claude Code", "codex": "Codex"}
    for row in rows:
        day, provider_key, model_name, session = row[:4]
        tokens, cost = sum(row[4:8]), row[8]
        provider = providers.setdefault(provider_key, {
            "name": names.get(provider_key, provider_key.title()),
            "key": provider_key, "tokens": 0, "sessions": set(),
            "cost": 0.0, "share": 0.0,
        })
        provider["tokens"] += tokens
        provider["sessions"].add(session)
        provider["cost"] += cost
        model = models.setdefault((provider_key, model_name), {
            "name": model_name, "provider": provider_key,
            "tokens": 0, "cost": 0.0, "share": 0.0,
        })
        model["tokens"] += tokens
        model["cost"] += cost
        buckets = daily.setdefault(provider_key, [0] * days)
        buckets[(date.fromisoformat(day) - start).days] += tokens
    for provider in providers.values():
        provider["sessions"] = len(provider["sessions"])
        provider["share"] = provider["tokens"] / total * 100 if total else 0.0
    for model in models.values():
        model["share"] = model["tokens"] / total * 100 if total else 0.0
    return {
        "start": start, "end": start + timedelta(days=days - 1),
        "tokens": total, "sessions": len({row[3] for row in rows}),
        "cost": sum(provider["cost"] for provider in providers.values()),
        "cached": sum(row[5] for row in rows),
        "uncached": sum(row[4] + row[6] for row in rows),
        "output": sum(row[7] for row in rows),
        "providers": sorted(providers.values(), key=lambda p: p["tokens"], reverse=True),
        "models": sorted(models.values(), key=lambda m: m["tokens"], reverse=True),
        "daily": daily,
    }


def fmt_tokens(n: int) -> str:
    for divisor, suffix in ((10**9, "B"), (10**6, "M"), (10**3, "K")):
        if abs(n) >= divisor:
            value = float(f"{n / divisor:.3g}")
            return f"{value:g}{suffix}"
    return str(n)


def fmt_cost(x: float) -> str:
    return f"${x:,.2f}"


def render_svg(s: dict, theme: str, updated: date) -> str:
    palettes = {
        "light": ("#ffffff", "#d0d7de", "#1f2328", "#59636e", "#eaeef2"),
        "dark": ("#0d1117", "#30363d", "#e6edf3", "#9198a1", "#21262d"),
    }
    bg, border, foreground, muted, grid = palettes[theme]
    models = s["models"][:6]
    height = 418 + len(models) * 28
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="840" height="{height}" viewBox="0 0 840 {height}">',
        f'<rect x="0.5" y="0.5" width="839" height="{height - 1}" rx="10" fill="{bg}" stroke="{border}"/>',
        '<g font-family="-apple-system, BlinkMacSystemFont, &quot;Segoe UI&quot;, Helvetica, Arial, sans-serif">',
    ]

    def text(x: float, y: float, value: object, size: int = 12,
             color: str = foreground, weight: int = 400, anchor: str = "start") -> None:
        parts.append(f'<text x="{x:g}" y="{y:g}" font-size="{size}" '
                     f'fill="{color}" font-weight="{weight}" text-anchor="{anchor}">'
                     f'{escape(str(value))}</text>')

    def color(provider: str) -> str:
        return foreground if provider == "codex" else "#D97757" if provider == "claude" else "#8250df"

    def dot(x: float, y: float, provider: str) -> None:
        parts.append(f'<circle cx="{x:g}" cy="{y:g}" r="4" fill="{color(provider)}"/>')

    def separator(y: int) -> None:
        parts.append(f'<path d="M24 {y}H816" stroke="{grid}"/>')

    start, end = s["start"], s["end"]
    days = (end - start).days + 1
    date_range = f'{start:%b} {start.day} – {end:%b} {end.day}, {end.year} · last {days} days'
    text(24, 34, "Tokens burned", 15, weight=600)
    text(816, 34, date_range, 12, muted, anchor="end")
    text(24, 91, fmt_tokens(s["tokens"]), 40, weight=700)
    text(24, 113, f'{s["sessions"]:,} sessions', 12, muted)
    for index, provider in enumerate(s["providers"][:3]):
        y = 149 + index * 52
        dot(28, y - 4, provider["key"])
        parts.append(f'<text y="{y}" font-size="14" fill="{foreground}">'
                     f'<tspan x="40">{escape(provider["name"])}</tspan>'
                     f'<tspan dx="8" font-size="11" fill="{muted}">'
                     f'{provider["sessions"]:,} sessions</tspan></text>')
        text(300, y, fmt_tokens(provider["tokens"]), 14, weight=600, anchor="end")
        text(40, y + 19, f'{provider["share"]:.1f}% of tokens · {fmt_cost(provider["cost"])}', 12, muted)

    text(330, 72, "Daily processed tokens", 13, weight=600)
    chart_left, chart_right, chart_top, baseline = 366.0, 816.0, 96.0, 240.0
    daily_max = max((max(values, default=0) for values in s["daily"].values()), default=0)
    scale = 1.0
    if daily_max > 0:
        magnitude = 10 ** math.floor(math.log10(daily_max))
        scale = next(multiplier * magnitude for multiplier in (1, 2, 2.5, 5, 10)
                     if multiplier * magnitude >= daily_max)
    for fraction in (0.0, 0.5, 1.0):
        y = baseline - fraction * (baseline - chart_top)
        parts.append(f'<path d="M{chart_left:g} {y:g}H{chart_right:g}" stroke="{grid}"/>')
        text(chart_left - 8, y + 3, fmt_tokens(round(scale * fraction)), 10, muted, anchor="end")
    for provider in s["providers"]:
        values = s["daily"][provider["key"]]
        points = [(chart_left + i * (chart_right - chart_left) / max(days - 1, 1),
                   baseline - value / scale * (baseline - chart_top))
                  for i, value in enumerate(values)]
        path = f'M{points[0][0]:.2f},{points[0][1]:.2f}'
        for i in range(len(points) - 1):
            p0, p1 = points[max(i - 1, 0)], points[i]
            p2, p3 = points[i + 1], points[min(i + 2, len(points) - 1)]
            c1x = p1[0] + (p2[0] - p0[0]) / 6
            c2x = p2[0] - (p3[0] - p1[0]) / 6
            c1y = min(baseline, max(chart_top, p1[1] + (p2[1] - p0[1]) / 6))
            c2y = min(baseline, max(chart_top, p2[1] - (p3[1] - p1[1]) / 6))
            path += f' C{c1x:.2f},{c1y:.2f} {c2x:.2f},{c2y:.2f} {p2[0]:.2f},{p2[1]:.2f}'
        series_color = color(provider["key"])
        area = path + f' L{points[-1][0]:.2f},{baseline:g} L{points[0][0]:.2f},{baseline:g} Z'
        parts.append(f'<path d="{area}" fill="{series_color}" fill-opacity="0.12"/>')
        parts.append(f'<path d="{path}" fill="none" stroke="{series_color}" stroke-width="1.5"/>')
    for index in sorted({0, (days - 1) // 2, days - 1}):
        day = start + timedelta(days=index)
        x = chart_left + index * (chart_right - chart_left) / max(days - 1, 1)
        anchor = "start" if index == 0 else "end" if index == days - 1 else "middle"
        text(x, 260, f'{day:%b} {day.day}'.upper(), 10, muted, anchor=anchor)

    text(24, 300, "Totals", 13, weight=600)
    totals = [("Processed tokens", fmt_tokens(s["tokens"])),
              ("Cached input", fmt_tokens(s["cached"])),
              ("Uncached input", fmt_tokens(s["uncached"])),
              ("Output", fmt_tokens(s["output"])),
              ("Est. API cost", fmt_cost(s["cost"]))]
    for index, (label, value) in enumerate(totals):
        x = 24 + index * (816 - 24) / 5
        text(x, 325, label, 12, muted)
        text(x, 347, value, 15, weight=600)
    text(24, 380, "Model", 11, muted)
    for x, label in ((560, "Cost"), (680, "Share"), (816, "Tokens")):
        text(x, 380, label, 11, muted, anchor="end")
    separator(389)
    for index, model in enumerate(models):
        y = 408 + index * 28
        dot(28, y - 4, model["provider"])
        text(40, y, model["name"], 13)
        text(560, y, fmt_cost(model["cost"]), 13, anchor="end")
        share = "<0.1%" if model["share"] < 0.1 else f'{model["share"]:.1f}%'
        text(680, y, share, 13, anchor="end")
        text(816, y, fmt_tokens(model["tokens"]), 13, anchor="end")
        separator(y + 9)
    text(24, height - 15,
         f'From local Claude Code + Codex logs via T3 Code · updated {updated:%Y-%m-%d}',
         10, muted)
    parts.extend(["</g>", "</svg>"])
    return "\n".join(parts) + "\n"


def main() -> None:
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    exporter = subparsers.add_parser("export")
    exporter.add_argument("--days", type=int, default=45)
    exporter.add_argument("--claude-root", type=Path, default=Path.home() / ".claude" / "projects")
    exporter.add_argument("--codex-root", type=Path, default=Path.home() / ".codex" / "sessions")
    exporter.add_argument("--rates", type=Path, default=Path.home() / ".t3" / "userdata" / "usage-model-rates.json")
    exporter.add_argument("--data", type=Path, default=Path("data/usage.json"))
    renderer = subparsers.add_parser("render")
    renderer.add_argument("--days", type=int, default=30)
    renderer.add_argument("--tz")
    renderer.add_argument("--data", type=Path, default=Path("data/usage.json"))
    renderer.add_argument("--out", type=Path, default=Path("assets"))
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")

    def resolve(path: Path) -> Path:
        path = path.expanduser()
        return path if path.is_absolute() else repo / path

    data_path = resolve(args.data)
    if args.command == "export":
        today = date.today()
        start = today - timedelta(days=args.days - 1)
        since_ms = int(datetime.combine(start, time.min).timestamp() * 1000)
        records = parse_claude(resolve(args.claude_root), since_ms) + parse_codex(resolve(args.codex_root), since_ms)
        rates_path = resolve(args.rates)
        rates = load_rates(rates_path) if rates_path.exists() else {}
        aggregated: dict[tuple[str, str, str, str], list] = {}
        for rec in records:
            day = datetime.fromtimestamp(rec.ts_ms / 1000).date().isoformat()
            session_hash = hashlib.sha256(rec.session.encode("utf-8")).hexdigest()[:12]
            key = (day, rec.provider, rec.model, session_hash)
            values = aggregated.setdefault(key, [0, 0, 0, 0, 0.0])
            for index, value in enumerate(rec[4:]):
                values[index] += value
            values[4] += record_cost(rec, rates)
        rows = []
        if data_path.exists():
            with data_path.open(encoding="utf-8") as stream:
                rows = [row for row in json.load(stream)["rows"] if row[0] < start.isoformat()]
        rows.extend([*key, *values[:4], round(values[4], 4)] for key, values in aggregated.items())
        cutoff = (today - timedelta(days=60)).isoformat()
        rows = sorted(row for row in rows if row[0] >= cutoff)
        data = {"version": 1, "timezone": "America/New_York",
                "updated": datetime.now().astimezone().isoformat(), "rows": rows}
        data_path.parent.mkdir(parents=True, exist_ok=True)
        with data_path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(data, separators=(",", ":")) + "\n")
        print(f'{len(records):,} records, {len(rows):,} rows -> {args.data.as_posix()}')
        return

    with data_path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    today = datetime.now(ZoneInfo(args.tz or data["timezone"])).date()
    start = today - timedelta(days=args.days - 1)
    summary = summarize(data["rows"], start, args.days)
    updated = datetime.fromisoformat(data["updated"]).date()
    out = resolve(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        with (out / f"usage-{theme}.svg").open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(render_svg(summary, theme, updated))
    print(f'{fmt_tokens(summary["tokens"])} tokens, {summary["sessions"]:,} sessions, '
          f'{fmt_cost(summary["cost"])} -> {args.out.as_posix().rstrip("/")}/')


if __name__ == "__main__":
    main()
