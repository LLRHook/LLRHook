#!/usr/bin/env python3
"""Render T3 Code's local usage cache as GitHub profile SVG cards."""

import argparse
import json
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import NamedTuple
from xml.sax.saxutils import escape


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


def load_records(cache_path: Path, since_ms: int, until_ms: int) -> list[Record]:
    with cache_path.open(encoding="utf-8") as stream:
        cache = json.load(stream)
    if cache.get("version") != 4:
        raise SystemExit(f"unsupported T3 usage cache version: {cache.get('version')}")
    records = []
    for entry in cache["files"].values():
        for row in entry["r"]:
            if not since_ms <= row[0] < until_ms:
                continue
            model = cache["models"][row[1]]
            if model == "<synthetic>":
                continue
            records.append(Record(row[0], entry["p"], model,
                                  cache["sessions"][row[2]], *row[3:7]))
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


def summarize(records: list[Record], rates: dict, start: date, days: int) -> dict:
    providers: dict[str, dict] = {}
    models: dict[tuple[str, str], dict] = {}
    daily: dict[str, list[int]] = {}
    total = sum(rec.tokens for rec in records)
    names = {"claude": "Claude Code", "codex": "Codex"}
    for rec in records:
        cost = record_cost(rec, rates)
        provider = providers.setdefault(rec.provider, {
            "name": names.get(rec.provider, rec.provider.title()),
            "key": rec.provider, "tokens": 0, "sessions": set(),
            "cost": 0.0, "share": 0.0,
        })
        provider["tokens"] += rec.tokens
        provider["sessions"].add(rec.session)
        provider["cost"] += cost
        model = models.setdefault((rec.provider, rec.model), {
            "name": rec.model, "provider": rec.provider,
            "tokens": 0, "cost": 0.0, "share": 0.0,
        })
        model["tokens"] += rec.tokens
        model["cost"] += cost
        buckets = daily.setdefault(rec.provider, [0] * days)
        day = (datetime.fromtimestamp(rec.ts_ms / 1000).date() - start).days
        if 0 <= day < days:
            buckets[day] += rec.tokens
    for provider in providers.values():
        provider["sessions"] = len(provider["sessions"])
        provider["share"] = provider["tokens"] / total * 100 if total else 0.0
    for model in models.values():
        model["share"] = model["tokens"] / total * 100 if total else 0.0
    return {
        "start": start, "end": start + timedelta(days=days - 1),
        "tokens": total, "sessions": len({rec.session for rec in records}),
        "cost": sum(provider["cost"] for provider in providers.values()),
        "cached": sum(rec.cache_read for rec in records),
        "uncached": sum(rec.input + rec.cache_write for rec in records),
        "output": sum(rec.output for rec in records),
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


def render_svg(s: dict, theme: str) -> str:
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
         f'From local Claude Code + Codex logs via T3 Code · updated {date.today():%Y-%m-%d}',
         10, muted)
    parts.extend(["</g>", "</svg>"])
    return "\n".join(parts) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--t3-dir", type=Path, default=Path.home() / ".t3" / "userdata")
    parser.add_argument("--out", type=Path, default=Path("assets"))
    args = parser.parse_args()
    today = date.today()
    start = today - timedelta(days=args.days - 1)
    since_ms = int(datetime.combine(start, time.min).timestamp() * 1000)
    until_ms = int(datetime.combine(today + timedelta(days=1), time.min).timestamp() * 1000)
    records = load_records(args.t3_dir.expanduser() / "usage-scan-cache.json", since_ms, until_ms)
    rates = load_rates(args.t3_dir.expanduser() / "usage-model-rates.json")
    summary = summarize(records, rates, start, args.days)
    out = args.out.expanduser()
    if not out.is_absolute():
        out = Path(__file__).resolve().parent.parent / out
    out.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        with (out / f"usage-{theme}.svg").open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(render_svg(summary, theme))
    print(f'{fmt_tokens(summary["tokens"])} tokens, {summary["sessions"]:,} sessions, '
          f'{fmt_cost(summary["cost"])} -> {args.out.as_posix().rstrip("/")}/')


if __name__ == "__main__":
    main()
