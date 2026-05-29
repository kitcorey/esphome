"""Offline replay validator for the garage-door distance classifier.

Replays a distance series through the OLD classifier (median, window 15) and
the PROPOSED classifier (upper quantile, window 60) — each followed by the
`delta: 0.01` value gate, the 0.50 m threshold, and the 10 s `delayed_on/off`
state debounce — and reports how often each one flaps `door_closed` open.

Two input modes:

  * HA history (default) — fetches `sensor.garage_door_distance` over a window
    via the REST API (`ha_url`/`ha_token` in secrets.yaml). ADVISORY ONLY: HA
    stores the already median+delta-filtered value, so this replay double-
    filters and is a *lower bound* on the real flap count, not proof.

  * Raw JSONL (`--raw <file>`) — replays captured pre-filter HC-SR04 samples
    (the temporary `Distance Raw` sensor, captured over the native API). This
    is the AUTHORITATIVE mode: it sees the true scatter the filter must reject.

Usage (from repo root):
    uv run python scripts/validate_garage_quantile.py                 # last 2h of HA history
    uv run python scripts/validate_garage_quantile.py --hours 6
    uv run python scripts/validate_garage_quantile.py --start 2026-05-28T14:00:00 --end 2026-05-28T14:30:00
    uv run python scripts/validate_garage_quantile.py --raw /tmp/door_raw.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ENTITY = "sensor.garage_door_distance"
THRESHOLD_M = 0.50  # door_threshold_cm default (50 cm) → meters
DELTA_M = 0.01  # delta: 0.01 value gate (1 cm)
DEBOUNCE_S = 10.0  # delayed_on / delayed_off on door_closed
UPDATE_INTERVAL_S = 0.1  # ultrasonic update_interval (nominal; self-limits to
# ~170 ms on hardware). For synthesizing raw timestamps.

# Classifiers. MEDIAN_WINDOW is the retired baseline (kept for the contrast);
# QUANTILE_WINDOW is current production (garage-door.yml).
MEDIAN_WINDOW = 15  # retired median baseline
QUANTILE_WINDOW = 60  # current production quantile window
QUANTILE_SWEEP = [0.7, 0.8, 0.85, 0.9]
PROPOSED_Q = 0.8  # the value going into the distance_quantile substitution

# CAVEAT: for a *severe* obstruction (70–90 % of pings time out / NaN), this
# offline replay UNDER-predicts flaps — the few valid samples plus client-side
# timestamp jitter make the debounce reconstruction fragile, and an on-device
# replay through this model showed 0 flaps where the real device flapped once.
# Treat the replay as directional; an on-device soak (watch binary_sensor.
# garage_door_closed in HA with the obstruction in place) is authoritative.

NON_NUMERIC = {"unavailable", "unknown", "none", ""}


@dataclass
class Sample:
    t: float  # seconds (monotonic-ish; absolute epoch for HA, relative for raw)
    value: float  # distance in meters


# --- ESPHome filter replicas ------------------------------------------------
# Faithful to esphome/components/sensor/filter.cpp (verified against the source
# in the installed esphome package) with send_every: 1, so every input produces
# one output. Windows are sample-count based (not time based). NaN values are
# dropped from the window before computing, matching SortedWindowFilter's
# get_window_values_().


def median_filter(values: list[float], window: int) -> list[float]:
    """Sliding-window median. Even windows average the two middle elements,
    matching ESPHome's MedianFilter::compute_result."""
    out, queue = [], []
    for v in values:
        queue.append(v)
        if len(queue) > window:
            queue.pop(0)
        s = sorted(x for x in queue if x == x)  # drop NaN
        n = len(s)
        if n == 0:
            out.append(float("nan"))
            continue
        mid = n // 2
        out.append(s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0)
    return out


def quantile_filter(values: list[float], window: int, q: float) -> list[float]:
    """Sliding-window quantile. position = ceil(size * q) - 1, matching
    ESPHome's QuantileFilter::compute_result (NOT ceil(q*(size-1)) — the two
    diverge for partial/warmup windows)."""
    out, queue = [], []
    for v in values:
        queue.append(v)
        if len(queue) > window:
            queue.pop(0)
        s = sorted(x for x in queue if x == x)  # drop NaN
        n = len(s)
        if n == 0:
            out.append(float("nan"))
            continue
        position = math.ceil(n * q) - 1
        position = min(max(position, 0), n - 1)
        out.append(s[position])
    return out


def delta_filter(values: list[float], delta: float) -> list[float]:
    """delta: <x> — pass a value only if it differs from the last *published*
    value by STRICTLY MORE than delta (matching DeltaFilter: delta > min);
    otherwise hold the previous published value. The first value always passes.
    The held series is what the binary-sensor lambda reads via .state."""
    out = []
    last = None
    for v in values:
        if last is None or abs(v - last) > delta:
            last = v
        out.append(last)
    return out


def debounce(
    bool_samples: list[tuple[float, bool]], delay_on: float, delay_off: float, initial: bool
) -> list[tuple[float, bool]]:
    """delayed_on / delayed_off binary filter. A raw value must hold for the
    relevant delay before the output follows it; a flip-back inside the delay
    cancels the pending transition. Returns the list of fired transitions as
    (time, new_state). delay_on gates True (closed); delay_off gates False."""
    transitions: list[tuple[float, bool]] = []
    output = initial
    pending_val: bool | None = None
    pending_since: float | None = None
    for t, raw in bool_samples:
        if raw == output:
            pending_val = pending_since = None
            continue
        if pending_val != raw:
            pending_val = raw
            pending_since = t
        delay = delay_on if raw else delay_off
        assert pending_since is not None
        if t - pending_since >= delay:
            output = raw
            transitions.append((pending_since + delay, raw))
            pending_val = pending_since = None
    return transitions


# --- Replay + metrics -------------------------------------------------------


@dataclass
class Metrics:
    flaps: int  # spurious door_closed -> open transitions
    open_time_s: float  # total time reported open
    deepest_excursion_m: float  # how far the published value dipped below threshold (0 if never)
    n_samples: int


def replay(samples: list[Sample], published: list[float]) -> Metrics:
    """Apply threshold + debounce to a published-distance series and score it.
    Assumes the door is physically closed for the whole window, so every open
    is spurious."""
    door_closed_raw = [(s.t, pub >= THRESHOLD_M) for s, pub in zip(samples, published)]
    start_t = samples[0].t
    end_t = samples[-1].t
    transitions = debounce(door_closed_raw, DEBOUNCE_S, DEBOUNCE_S, initial=True)

    flaps = sum(1 for _, ns in transitions if ns is False)

    # Total time reported open, walking the transition list from a closed start.
    open_time = 0.0
    state = True
    last_t = start_t
    for ft, ns in transitions:
        if state is False:
            open_time += ft - last_t
        state = ns
        last_t = ft
    if state is False:
        open_time += end_t - last_t

    below = [THRESHOLD_M - pub for pub in published if pub < THRESHOLD_M]
    deepest = max(below) if below else 0.0

    return Metrics(flaps=flaps, open_time_s=open_time, deepest_excursion_m=deepest, n_samples=len(samples))


# --- Input loaders ----------------------------------------------------------


def load_secrets() -> tuple[str, str]:
    secrets_path = REPO_ROOT / "secrets.yaml"
    with open(secrets_path) as f:
        secrets = yaml.safe_load(f)
    url = secrets.get("ha_url")
    token = secrets.get("ha_token")
    if not url or not token or token == "YOUR_LONG_LIVED_ACCESS_TOKEN":
        print("Error: Set ha_url and ha_token in secrets.yaml")
        print("Generate a token in HA: Profile > Security > Long-Lived Access Tokens")
        sys.exit(1)
    return url.rstrip("/"), token


def fetch_ha_history(entity: str, start: datetime, end: datetime | None) -> list[Sample]:
    url, token = load_secrets()
    headers = {"Authorization": f"Bearer {token}"}
    params = {"filter_entity_id": entity, "no_attributes": ""}
    if end is not None:
        params["end_time"] = end.isoformat()
    resp = requests.get(f"{url}/api/history/period/{start.isoformat()}", headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data or not data[0]:
        return []
    samples = []
    for entry in data[0]:
        state = entry.get("state", "")
        if str(state).lower() in NON_NUMERIC:
            continue
        try:
            value = float(state)
        except (ValueError, TypeError):
            continue
        ts = entry.get("last_updated") or entry.get("last_changed")
        try:
            t = datetime.fromisoformat(ts).timestamp()
        except (ValueError, TypeError):
            continue
        samples.append(Sample(t=t, value=value))
    samples.sort(key=lambda s: s.t)
    return samples


def load_raw_jsonl(path: Path) -> list[Sample]:
    """Replay captured raw HC-SR04 samples. Tolerant of a few field names so a
    simple aioesphomeapi subscribe-and-dump (per validate_sunrise.py) works
    without a strict schema. Synthesizes 500 ms spacing if no timestamp."""
    value_keys = ("value", "distance", "state", "raw", "door_distance_raw")
    time_keys = ("t", "time", "timestamp", "ts")
    samples = []
    idx = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") in ("metadata", "log"):
                continue
            value = next((obj[k] for k in value_keys if isinstance(obj.get(k), (int, float))), None)
            if value is None:
                continue
            value = float(value)
            if value != value:  # NaN guard
                continue
            t = next((float(obj[k]) for k in time_keys if isinstance(obj.get(k), (int, float))), None)
            if t is None:
                t = idx * UPDATE_INTERVAL_S
            samples.append(Sample(t=t, value=value))
            idx += 1
    samples.sort(key=lambda s: s.t)
    return samples


# --- Report -----------------------------------------------------------------


def fmt_metrics(label: str, m: Metrics) -> str:
    deep = f"{m.deepest_excursion_m * 100:.0f} cm below" if m.deepest_excursion_m else "none"
    return f"  {label:<28} flaps={m.flaps:<3} open_time={m.open_time_s:7.1f}s  deepest_excursion={deep}"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--entity", default=DEFAULT_ENTITY, help=f"HA entity to fetch (default: {DEFAULT_ENTITY})")
    p.add_argument("--hours", type=float, default=2.0, help="Hours of HA history back from now (default: 2)")
    p.add_argument("--start", help="ISO8601 window start (overrides --hours)")
    p.add_argument("--end", help="ISO8601 window end (default: now)")
    p.add_argument("--raw", type=Path, help="Replay captured raw HC-SR04 samples (JSONL) — AUTHORITATIVE mode")
    args = p.parse_args()

    if args.raw:
        samples = load_raw_jsonl(args.raw)
        source = f"raw JSONL {args.raw}"
        advisory = False
    else:
        if args.start:
            start = datetime.fromisoformat(args.start)
            if start.tzinfo is None:
                start = start.astimezone()
        else:
            start = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        end = None
        if args.end:
            end = datetime.fromisoformat(args.end)
            if end.tzinfo is None:
                end = end.astimezone()
        samples = fetch_ha_history(args.entity, start, end)
        source = f"HA history {args.entity}"
        advisory = True

    if len(samples) < MEDIAN_WINDOW:
        print(
            f"Not enough samples ({len(samples)}) from {source} to replay "
            f"(need >= {MEDIAN_WINDOW}). Widen the window or check the source."
        )
        return 1

    span = samples[-1].t - samples[0].t
    print(f"\nGarage door classifier replay — {source}")
    print("=" * 78)
    print(
        f"  {len(samples)} samples over {span / 60:.1f} min  "
        f"(threshold {THRESHOLD_M * 100:.0f} cm, debounce {DEBOUNCE_S:.0f} s)"
    )

    if advisory:
        print()
        print("  " + "*" * 72)
        print("  *  ADVISORY (LOWER BOUND) — NOT PROOF.                                 *")
        print("  *  HA history stores the already median+delta-filtered value, so this  *")
        print("  *  replay double-filters and UNDER-counts flaps vs. true raw scatter.  *")
        print("  *  Re-run with --raw <captured.jsonl> for the authoritative test.      *")
        print("  " + "*" * 72)

    values = [s.value for s in samples]

    # Current production: median w15 -> delta -> threshold -> debounce.
    median_pub = delta_filter(median_filter(values, MEDIAN_WINDOW), DELTA_M)
    median_m = replay(samples, median_pub)

    # Proposed: quantile w60 @ q=0.8 -> delta -> threshold -> debounce.
    quant_pub = delta_filter(quantile_filter(values, QUANTILE_WINDOW, PROPOSED_Q), DELTA_M)
    quant_m = replay(samples, quant_pub)

    print("\nClassifier comparison:")
    print(fmt_metrics(f"current (median w{MEDIAN_WINDOW})", median_m))
    print(fmt_metrics(f"proposed (quantile w{QUANTILE_WINDOW} q={PROPOSED_Q})", quant_m))

    print(f"\nQuantile sweep (flap count per q, window {QUANTILE_WINDOW}):")
    for q in QUANTILE_SWEEP:
        pub = delta_filter(quantile_filter(values, QUANTILE_WINDOW, q), DELTA_M)
        m = replay(samples, pub)
        marker = "  <- distance_quantile" if abs(q - PROPOSED_Q) < 1e-9 else ""
        print(f"  q={q:<5} flaps={m.flaps:<3} open_time={m.open_time_s:7.1f}s{marker}")

    print()
    verdict_ok = quant_m.flaps == 0 and median_m.flaps >= 1
    if verdict_ok:
        print(f"PASS: proposed quantile shows 0 flaps where current median shows {median_m.flaps}.")
    elif quant_m.flaps == 0:
        print(
            "OK: proposed quantile shows 0 flaps (current median also clean over "
            "this window — pick a window that contains the fault to see the contrast)."
        )
    else:
        print(
            f"WARNING: proposed quantile still shows {quant_m.flaps} flap(s) over "
            "this window. Inspect the sweep above and consider raising the quantile."
        )
    if advisory:
        print("(Advisory run — confirm against --raw capture before trusting the count.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
