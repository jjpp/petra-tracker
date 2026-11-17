# SPDX-License-Identifier: GPL-3.0-or-later
"""Compare ETA engines on a recorded trip.

Replays raw API responses captured during a real trip (order_list and
order_detail JSON files, one per poll, named by UTC time like
2030-01-15T07-00-00Z.json), asks each engine for an ETA at every new GPS fix,
and compares the predicted arrival with the actual one (car arrived at pickup
= person event type 3; arrived at destination = tripEnded).

Captures contain personal data: keep them outside this repository.
NOTE: engines other than straight_line receive the car's positions and the
trip's pickup/destination coordinates.

Usage:
  python tools/compare_eta.py --list-dir CAP/list --detail-dir CAP/detail \
      [--engines straight_line,osrm,openrouteservice,waze] [--osrm-url URL] \
      [--ors-key KEY] [--waze-region EU] [--speed 25] [--every 60]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.petra_tracker.core.eta import (  # noqa: E402
    STRAIGHT_LINE,
    EtaParams,
    drive_minutes,
    eta_target,
    haversine_m,
)
from custom_components.petra_tracker.core.models import (  # noqa: E402
    PERSON_EVENT_ARRIVED,
    LatLon,
    parse_order,
)
from custom_components.petra_tracker.core.routing import RoutingError, make_provider  # noqa: E402


def snapshot_time(path: Path) -> datetime:
    return datetime.strptime(path.stem[:20], "%Y-%m-%dT%H-%M-%SZ").replace(tzinfo=UTC)


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def actual_arrivals(list_dir: Path, book_ref: int) -> dict[str, datetime]:
    """When the car really reached the pickup and the destination."""
    out: dict[str, datetime] = {}
    for path in sorted(list_dir.glob("*.json")):
        data = load(path)
        for item in (data or {}).get("items", []) if isinstance(data, dict) else []:
            if item.get("bookRefNo") != book_ref:
                continue
            trip = parse_order(item)
            if trip is None:
                continue
            for ev in trip.events:
                if ev.type == PERSON_EVENT_ARRIVED:
                    out.setdefault("to_pickup", ev.time)
            if trip.trip_ended:
                out.setdefault("to_destination", trip.trip_ended)
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list-dir", type=Path, required=True)
    ap.add_argument("--detail-dir", type=Path, required=True)
    ap.add_argument("--engines", default=STRAIGHT_LINE)
    ap.add_argument("--osrm-url", default="")
    ap.add_argument("--ors-url", default="")
    ap.add_argument("--ors-key", default="")
    ap.add_argument("--waze-region", default="EU")
    ap.add_argument("--speed", type=float, default=25.0, help="assumed km/h for straight_line")
    ap.add_argument("--every", type=int, default=60, help="at most one sample per N seconds")
    args = ap.parse_args()
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]

    samples = []  # (time, leg, origin, dest, book_ref)
    last_fix_time = None
    last_sample = None
    for path in sorted(args.detail_dir.glob("*.json")):
        data = load(path)
        if not isinstance(data, dict):
            continue
        trip = parse_order(data)
        if trip is None:
            continue
        trip.apply_detail(data)
        target, fix = eta_target(trip, True), trip.last_fix
        if target is None or fix is None or fix.time == last_fix_time:
            continue
        now = snapshot_time(path)
        if last_sample and (now - last_sample).total_seconds() < args.every:
            continue
        last_fix_time, last_sample = fix.time, now
        leg, dest = target
        origin = LatLon(fix.lat, fix.lon)
        if haversine_m(origin, dest) > 60:
            samples.append((now, leg.value, origin, dest, trip.book_ref))
    if not samples:
        sys.exit("no driving samples found (need detail snapshots with GPS while approaching/onboard)")

    arrivals = actual_arrivals(args.list_dir, samples[0][4])
    print(f"samples: {len(samples)}; actual arrivals: "
          + ", ".join(f"{k} {v:%H:%M:%S}" for k, v in sorted(arrivals.items())))

    errors: dict[tuple[str, str], list[float]] = {}
    async with aiohttp.ClientSession() as session:
        providers = {
            e: make_provider(e, session, url=args.osrm_url if e == "osrm" else args.ors_url,
                             api_key=args.ors_key, waze_region=args.waze_region)
            for e in engines
        }
        print(f"\n{'time':8} {'leg':15} " + " ".join(f"{e[:14]:>14}" for e in engines) + "   actual")
        for now, leg, origin, dest, _ in samples:
            actual = arrivals.get(leg)
            remaining = (actual - now).total_seconds() / 60 if actual else None
            cells = []
            for e in engines:
                provider = providers[e]
                try:
                    if provider is None:
                        minutes = drive_minutes(haversine_m(origin, dest), EtaParams(assumed_speed_kmh=args.speed))
                    else:
                        minutes = (await provider.route(origin, dest)).duration_s / 60
                except RoutingError as err:
                    cells.append(f"{'ERR':>14}")
                    print(f"  ({e}: {err})", file=sys.stderr)
                    continue
                if remaining is not None:
                    errors.setdefault((e, leg), []).append(minutes - remaining)
                cells.append(f"{minutes:14.1f}")
                if provider is not None:
                    await asyncio.sleep(1.1)  # be polite to public servers
            act = f"{remaining:6.1f}" if remaining is not None else "     ?"
            print(f"{now:%H:%M:%S} {leg:15} " + " ".join(cells) + f"   {act}")

    print("\nError in minutes (predicted - actual; + = too pessimistic):")
    for (e, leg), errs in sorted(errors.items()):
        print(f"  {e:17} {leg:15} n={len(errs):3}  mean={statistics.mean(errs):+5.1f}  "
              f"mean|err|={statistics.mean(abs(x) for x in errs):4.1f}  worst={max(errs, key=abs):+5.1f}")


if __name__ == "__main__":
    asyncio.run(main())
