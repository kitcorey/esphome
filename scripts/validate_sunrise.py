"""End-to-end validation for the reusable common/sunrise.yml effect.

Compiles bedroom-headboard.yml, OTA-uploads it, triggers `start_sunrise`
over the native API at a short duration, captures every LightState the
device emits, and checks that brightness + RGB + color-temperature
traverse the expected ranges. Exits non-zero on any check failure.

Usage (from repo root):
    uv run python scripts/validate_sunrise.py
    uv run python scripts/validate_sunrise.py --skip-compile --skip-upload --duration 30
    uv run python scripts/validate_sunrise.py --duration 1800 --leave-on \\
        --samples-out /tmp/sunrise_30min.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml
from aioesphomeapi import (
    APIClient,
    APIConnectionError,
    LightInfo,
    LightState,
    LogLevel,
    UserService,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TICK_INTERVAL_S = 2.0
HEADROOM_S = 10.0
MIN_SAMPLES_RATIO = 0.85
START_SERVICE = "start_sunrise"
STOP_SERVICE = "stop_sunrise"

INITIAL_BRIGHTNESS_MAX = 0.02
INITIAL_GREEN_RANGE = (0.10, 0.32)
INITIAL_CT_MIREDS = 416.7
CT_TOLERANCE_MIREDS = 5.0
PHASE2_GREEN_RANGE = (0.40, 0.50)
PHASE2_BLUE_MAX = 0.02
PHASE3_GREEN_RANGE = (0.78, 0.86)
PHASE3_BLUE_RANGE = (0.04, 0.08)
FINAL_BRIGHTNESS_MIN = 0.95
FINAL_CT_MIREDS_MAX = 295.0
FINAL_COLD_WHITE_MIN = 0.6
BRIGHTNESS_BACKSLIDE_TOLERANCE = 0.005
SUNRISE_DONE_BRIGHTNESS = 0.99
SUNRISE_DONE_COLD_WHITE = 0.95
EXPECTED_WARNING = re.compile(
    r"\b(Brightness|White) value (1\.0[0-9]|0\.99[5-9]) is out of range\b"
)


@dataclass(frozen=True)
class Config:
    device_yaml: Path
    device_name: str
    friendly_name: str
    static_ip: str
    sunrise_light_id: str
    api_encryption_key: str
    ota_password: str


@dataclass
class Sample:
    t: float
    state: bool
    brightness: float
    color_brightness: float
    red: float
    green: float
    blue: float
    white: float
    color_temperature: float
    cold_white: float
    warm_white: float


@dataclass
class LogEntry:
    t: float
    level: int
    message: str


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str
    severity: str = "fail"  # "fail" => failing exits 1, "warn" => informational only


@dataclass
class ValidationOutcome:
    samples: list[Sample] = field(default_factory=list)
    logs: list[LogEntry] = field(default_factory=list)
    started_at_utc: str = ""
    completed: bool = False


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--device",
        type=Path,
        default=REPO_ROOT / "bedroom-headboard.yml",
        help="Device YAML (default: %(default)s)",
    )
    p.add_argument("--duration", type=int, default=60, help="Sunrise duration in seconds (default: 60)")
    p.add_argument("--skip-compile", action="store_true", help="Skip esphome compile step")
    p.add_argument("--skip-upload", action="store_true", help="Skip esphome upload step")
    p.add_argument("--leave-on", action="store_true", help="Don't call stop_sunrise after validation")
    p.add_argument(
        "--samples-out",
        type=Path,
        default=None,
        help="JSONL output path for captured samples (default: sunrise_validation_<UTC>.jsonl in CWD)",
    )
    p.add_argument(
        "--log-level",
        default="DEBUG",
        choices=["ERROR", "WARN", "INFO", "CONFIG", "DEBUG", "VERBOSE", "VERY_VERBOSE"],
        help="aioesphomeapi log subscription level (default: %(default)s)",
    )
    p.add_argument("--no-color", action="store_true", help="Disable ANSI colors in report")
    args = p.parse_args()
    args.device = args.device.resolve()
    if args.samples_out is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.samples_out = Path.cwd() / f"sunrise_validation_{ts}.jsonl"
    return args


class _IgnoreUnknownTagsLoader(yaml.SafeLoader):
    """Treat ESPHome's `!include` / `!secret` tags as opaque strings.

    The device YAML uses these tags freely, but we only consume the
    `substitutions:` block so the actual values don't matter.
    """


def _ignore_unknown(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


_IgnoreUnknownTagsLoader.add_multi_constructor("!", _ignore_unknown)


def load_config(device_yaml: Path) -> Config:
    if not device_yaml.exists():
        raise FileNotFoundError(f"Device YAML not found: {device_yaml}")
    with open(device_yaml) as f:
        device = yaml.load(f, Loader=_IgnoreUnknownTagsLoader)
    subs = device.get("substitutions", {}) or {}
    required = ("name", "friendly_name", "static_ip", "sunrise_light_id")
    missing = [k for k in required if k not in subs]
    if missing:
        raise ValueError(f"Missing substitutions in {device_yaml.name}: {missing}")

    secrets_path = REPO_ROOT / "secrets.yaml"
    with open(secrets_path) as f:
        secrets = yaml.safe_load(f)
    for key in ("api_encryption_key", "ota_password"):
        if key not in secrets:
            raise ValueError(f"Missing {key} in {secrets_path}")

    return Config(
        device_yaml=device_yaml,
        device_name=subs["name"],
        friendly_name=subs["friendly_name"],
        static_ip=subs["static_ip"],
        sunrise_light_id=subs["sunrise_light_id"],
        api_encryption_key=secrets["api_encryption_key"],
        ota_password=secrets["ota_password"],
    )


def compile_device(cfg: Config) -> None:
    print(f"[compile] uv run esphome compile {cfg.device_yaml.name}", flush=True)
    subprocess.run(
        ["uv", "run", "esphome", "compile", str(cfg.device_yaml)],
        check=True,
        cwd=REPO_ROOT,
        timeout=600,
    )


def upload_device(cfg: Config) -> None:
    print(f"[upload] uv run esphome upload {cfg.device_yaml.name} --device {cfg.static_ip}", flush=True)
    subprocess.run(
        ["uv", "run", "esphome", "upload", str(cfg.device_yaml), "--device", cfg.static_ip],
        check=True,
        cwd=REPO_ROOT,
        timeout=300,
    )
    print("[upload] done — waiting 8s for device reboot", flush=True)
    time.sleep(8)


def _resolve_light(entities, cfg: Config) -> LightInfo:
    lights = [e for e in entities if isinstance(e, LightInfo)]
    by_object_id = next((e for e in lights if e.object_id == cfg.sunrise_light_id), None)
    if by_object_id is not None:
        return by_object_id
    by_name = next((e for e in lights if e.name == cfg.friendly_name), None)
    if by_name is not None:
        return by_name
    seen = ", ".join(f"{e.object_id}({e.name!r})" for e in lights) or "(none)"
    raise RuntimeError(
        f"Could not find light with object_id={cfg.sunrise_light_id!r} or name={cfg.friendly_name!r}. "
        f"Lights advertised by device: {seen}"
    )


def _resolve_service(services: list[UserService], name: str) -> UserService:
    svc = next((s for s in services if s.name == name), None)
    if svc is None:
        seen = ", ".join(s.name for s in services) or "(none)"
        raise RuntimeError(
            f"User service {name!r} not registered on device. "
            f"Services advertised: {seen}. "
            f"Did you recompile bedroom-headboard.yml after editing common/sunrise.yml?"
        )
    return svc


async def run_validation(cfg: Config, args: argparse.Namespace) -> ValidationOutcome:
    outcome = ValidationOutcome(started_at_utc=datetime.now(timezone.utc).isoformat())
    log_level = getattr(LogLevel, f"LOG_LEVEL_{args.log_level}")
    client = APIClient(
        cfg.static_ip,
        port=6053,
        password=None,
        noise_psk=cfg.api_encryption_key,
        expected_name=cfg.device_name,
    )
    sunrise_done = asyncio.Event()
    loop = asyncio.get_running_loop()
    start_t: float | None = None

    def on_state(state) -> None:
        if not isinstance(state, LightState) or start_t is None:
            return
        if state.key != light.key:
            return
        sample = Sample(
            t=time.monotonic() - start_t,
            state=state.state,
            brightness=state.brightness,
            color_brightness=state.color_brightness,
            red=state.red,
            green=state.green,
            blue=state.blue,
            white=state.white,
            color_temperature=state.color_temperature,
            cold_white=state.cold_white,
            warm_white=state.warm_white,
        )
        outcome.samples.append(sample)
        print(
            f"  t={sample.t:6.2f}s  bri={sample.brightness:.3f}  "
            f"rgb=({sample.red:.2f},{sample.green:.2f},{sample.blue:.2f})  "
            f"ww={sample.warm_white:.2f}  cw={sample.cold_white:.2f}  "
            f"ct={sample.color_temperature:.1f}",
            flush=True,
        )
        # Require state=True: ESPHome reports cached brightness/cold_white from
        # the prior on-state even while the light is off, so without this guard
        # the very first state notification can satisfy the threshold and trip
        # completion before the sunrise has actually started.
        if (
            sample.state
            and sample.brightness >= SUNRISE_DONE_BRIGHTNESS
            and sample.cold_white >= SUNRISE_DONE_COLD_WHITE
        ):
            loop.call_soon_threadsafe(sunrise_done.set)

    def on_log(msg) -> None:
        if start_t is None:
            return
        try:
            text = msg.message.decode("utf8", "backslashreplace")
        except Exception:
            text = repr(msg.message)
        outcome.logs.append(LogEntry(t=time.monotonic() - start_t, level=int(msg.level), message=text))

    try:
        try:
            await client.connect(login=True)
        except APIConnectionError as e:
            raise RuntimeError(
                f"Could not connect to {cfg.device_name} at {cfg.static_ip}:6053 "
                f"({e}). Try `ping {cfg.static_ip}` and confirm the device is on the IoT VLAN."
            ) from e

        entities, services = await client.list_entities_services()
        light = _resolve_light(entities, cfg)
        start_svc = _resolve_service(services, START_SERVICE)
        stop_svc = _resolve_service(services, STOP_SERVICE)

        client.subscribe_logs(on_log, log_level=log_level)
        client.subscribe_states(on_state)

        print(
            f"[validate] triggering {START_SERVICE}(duration_s={args.duration}) on "
            f"{cfg.device_name} (light key={light.key})",
            flush=True,
        )
        start_t = time.monotonic()
        try:
            await client.execute_service(start_svc, {"duration_s": int(args.duration)})
        except KeyError as e:
            raise RuntimeError(
                f"Argument name mismatch when calling {START_SERVICE}: missing {e}. "
                f"Service args advertised: {[a.name for a in start_svc.args]}"
            ) from e

        try:
            await asyncio.wait_for(sunrise_done.wait(), timeout=args.duration + HEADROOM_S)
            outcome.completed = True
            print(f"[validate] sunrise_done event observed at t={time.monotonic() - start_t:.1f}s", flush=True)
        except asyncio.TimeoutError:
            print(
                f"[validate] WARNING: sunrise didn't reach completion threshold "
                f"(brightness>={SUNRISE_DONE_BRIGHTNESS}, cold_white>={SUNRISE_DONE_COLD_WHITE}) "
                f"within {args.duration + HEADROOM_S:.0f}s. Running checks against partial capture.",
                flush=True,
            )

        if not args.leave_on:
            print(f"[validate] calling {STOP_SERVICE}", flush=True)
            await client.execute_service(stop_svc, {})
            await asyncio.sleep(7)
    finally:
        try:
            await client.disconnect()
        except Exception:  # pragma: no cover — disconnect best effort
            pass

    return outcome


# --- Checks -----------------------------------------------------------------

CheckFn = Callable[[list[Sample], list[LogEntry], int], CheckResult]


def check_enough_samples(samples: list[Sample], _logs: list[LogEntry], duration: int) -> CheckResult:
    expected = max(5, int((duration / TICK_INTERVAL_S) * MIN_SAMPLES_RATIO))
    actual = len(samples)
    return CheckResult(
        name="enough_samples",
        passed=actual >= expected,
        detail=f"{actual} samples observed (expected >= {expected})",
    )


def check_first_sample_dim_red(samples: list[Sample], _logs, _duration) -> CheckResult:
    early = [s for s in samples if s.t >= 0.5 and s.state]
    if not early:
        return CheckResult("first_sample_dim_red", False, "no early sample with state=on")
    s = early[0]
    problems = []
    if s.brightness > INITIAL_BRIGHTNESS_MAX:
        problems.append(f"brightness={s.brightness:.3f} > {INITIAL_BRIGHTNESS_MAX}")
    if not (INITIAL_GREEN_RANGE[0] <= s.green <= INITIAL_GREEN_RANGE[1]):
        problems.append(f"green={s.green:.3f} not in {INITIAL_GREEN_RANGE}")
    if abs(s.color_temperature - INITIAL_CT_MIREDS) > CT_TOLERANCE_MIREDS:
        problems.append(f"ct={s.color_temperature:.1f} not within ±{CT_TOLERANCE_MIREDS} of {INITIAL_CT_MIREDS}")
    if s.red < 0.95:
        problems.append(f"red={s.red:.3f} < 0.95")
    if s.blue > 0.02:
        problems.append(f"blue={s.blue:.3f} > 0.02")
    detail = (
        f"first sample t={s.t:.2f}s bri={s.brightness:.3f} green={s.green:.3f} ct={s.color_temperature:.1f}"
        + (f"; problems: {', '.join(problems)}" if problems else "")
    )
    return CheckResult("first_sample_dim_red", not problems, detail)


def check_brightness_monotonic(samples: list[Sample], _logs, _duration) -> CheckResult:
    on_samples = [s for s in samples if s.state]
    if len(on_samples) < 2:
        return CheckResult("brightness_monotonic", False, "fewer than 2 on-samples")
    running_max = on_samples[0].brightness
    worst = None
    for s in on_samples:
        if s.brightness < running_max - BRIGHTNESS_BACKSLIDE_TOLERANCE:
            drop = running_max - s.brightness
            if worst is None or drop > worst[1]:
                worst = (s, drop)
        running_max = max(running_max, s.brightness)
    if worst is None:
        return CheckResult("brightness_monotonic", True, "non-decreasing within tolerance")
    s, drop = worst
    return CheckResult(
        "brightness_monotonic",
        False,
        f"brightness backslid by {drop:.3f} at t={s.t:.2f}s (sample bri={s.brightness:.3f})",
    )


def check_brightness_reaches_max(samples: list[Sample], _logs, _duration) -> CheckResult:
    on_samples = [s for s in samples if s.state]
    if not on_samples:
        return CheckResult("brightness_reaches_max", False, "no on-samples")
    peak = max(s.brightness for s in on_samples)
    return CheckResult(
        "brightness_reaches_max",
        peak >= FINAL_BRIGHTNESS_MIN,
        f"peak brightness {peak:.3f} (expected >= {FINAL_BRIGHTNESS_MIN})",
    )


def check_phase1_to_2_boundary_seen(samples: list[Sample], _logs, _duration) -> CheckResult:
    matches = [
        s for s in samples
        if PHASE2_GREEN_RANGE[0] <= s.green <= PHASE2_GREEN_RANGE[1] and s.blue <= PHASE2_BLUE_MAX
    ]
    return CheckResult(
        "phase1_to_2_boundary_seen",
        bool(matches),
        f"{len(matches)} sample(s) with green ∈ {PHASE2_GREEN_RANGE} and blue ≤ {PHASE2_BLUE_MAX}",
    )


def check_phase2_to_3_boundary_seen(samples: list[Sample], _logs, _duration) -> CheckResult:
    matches = [
        s for s in samples
        if PHASE3_GREEN_RANGE[0] <= s.green <= PHASE3_GREEN_RANGE[1]
        and PHASE3_BLUE_RANGE[0] <= s.blue <= PHASE3_BLUE_RANGE[1]
    ]
    return CheckResult(
        "phase2_to_3_boundary_seen",
        bool(matches),
        f"{len(matches)} sample(s) with green ∈ {PHASE3_GREEN_RANGE} and blue ∈ {PHASE3_BLUE_RANGE}",
    )


def check_final_state_warm_white(samples: list[Sample], _logs, _duration) -> CheckResult:
    on_samples = [s for s in samples if s.state]
    if not on_samples:
        return CheckResult("final_state_warm_white", False, "no on-samples")
    s = max(on_samples, key=lambda x: x.brightness)
    problems = []
    if s.brightness < FINAL_BRIGHTNESS_MIN:
        problems.append(f"brightness={s.brightness:.3f} < {FINAL_BRIGHTNESS_MIN}")
    if s.color_temperature > FINAL_CT_MIREDS_MAX:
        problems.append(f"ct={s.color_temperature:.1f} > {FINAL_CT_MIREDS_MAX} mireds (too warm)")
    if s.cold_white < FINAL_COLD_WHITE_MIN:
        problems.append(f"cold_white={s.cold_white:.3f} < {FINAL_COLD_WHITE_MIN}")
    detail = (
        f"max-bri sample t={s.t:.2f}s bri={s.brightness:.3f} ct={s.color_temperature:.1f} cw={s.cold_white:.3f}"
        + (f"; problems: {', '.join(problems)}" if problems else "")
    )
    return CheckResult("final_state_warm_white", not problems, detail)


def check_no_error_logs(_samples, logs: list[LogEntry], _duration) -> CheckResult:
    errors = [l for l in logs if l.level == int(LogLevel.LOG_LEVEL_ERROR)]
    sample = errors[0].message if errors else ""
    return CheckResult(
        "no_error_logs",
        not errors,
        f"{len(errors)} ERROR log line(s)" + (f"; first: {sample}" if sample else ""),
    )


def check_no_unexpected_warnings(_samples, logs: list[LogEntry], _duration) -> CheckResult:
    warns = [l for l in logs if l.level == int(LogLevel.LOG_LEVEL_WARN)]
    unexpected = [l for l in warns if not EXPECTED_WARNING.search(l.message)]
    sample = unexpected[0].message if unexpected else ""
    return CheckResult(
        "no_unexpected_warnings",
        not unexpected,
        f"{len(unexpected)} unexpected WARN line(s) ({len(warns) - len(unexpected)} whitelisted)"
        + (f"; first: {sample}" if sample else ""),
        severity="warn",
    )


CHECKS: list[CheckFn] = [
    check_enough_samples,
    check_first_sample_dim_red,
    check_brightness_monotonic,
    check_brightness_reaches_max,
    check_phase1_to_2_boundary_seen,
    check_phase2_to_3_boundary_seen,
    check_final_state_warm_white,
    check_no_error_logs,
    check_no_unexpected_warnings,
]


def run_checks(samples: list[Sample], logs: list[LogEntry], duration: int) -> list[CheckResult]:
    results = []
    for fn in CHECKS:
        try:
            results.append(fn(samples, logs, duration))
        except Exception as e:  # don't let one bad check abort the report
            results.append(CheckResult(fn.__name__.removeprefix("check_"), False, f"check raised: {e!r}"))
    return results


# --- I/O --------------------------------------------------------------------

def write_samples(outcome: ValidationOutcome, cfg: Config, args: argparse.Namespace, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        meta = {
            "type": "metadata",
            "device": cfg.device_name,
            "duration_s": args.duration,
            "started_at_utc": outcome.started_at_utc,
            "completed": outcome.completed,
        }
        f.write(json.dumps(meta) + "\n")
        for s in outcome.samples:
            f.write(json.dumps({"type": "sample", **asdict(s)}) + "\n")
        for l in outcome.logs:
            f.write(json.dumps({"type": "log", **asdict(l)}) + "\n")
    print(f"[output] wrote {len(outcome.samples)} samples + {len(outcome.logs)} log lines to {path}", flush=True)


def print_report(checks: list[CheckResult], samples: list[Sample], logs: list[LogEntry], use_color: bool) -> int:
    GREEN = "\033[32m" if use_color else ""
    RED = "\033[31m" if use_color else ""
    YELLOW = "\033[33m" if use_color else ""
    BOLD = "\033[1m" if use_color else ""
    RESET = "\033[0m" if use_color else ""

    print()
    print(f"{BOLD}=== Sunrise validation report ==={RESET}")
    print(f"  samples captured: {len(samples)}    log lines: {len(logs)}")
    print()
    failed = 0
    warned = 0
    for c in checks:
        if c.passed:
            tag = f"{GREEN}[PASS]{RESET}"
        elif c.severity == "warn":
            tag = f"{YELLOW}[WARN]{RESET}"
            warned += 1
        else:
            tag = f"{RED}[FAIL]{RESET}"
            failed += 1
        print(f"  {tag} {c.name} — {c.detail}")
    print()
    if failed:
        print(f"{RED}{BOLD}Result: FAILED{RESET}  ({failed} failure(s), {warned} warning(s))")
        return 1
    if warned:
        print(f"{YELLOW}{BOLD}Result: PASSED with warnings{RESET}  ({warned} warning(s))")
    else:
        print(f"{GREEN}{BOLD}Result: PASSED{RESET}")
    return 0


async def _emergency_stop(cfg: Config) -> None:
    """Best-effort stop_sunrise after Ctrl-C so the bedroom light doesn't strand high."""
    client = APIClient(
        cfg.static_ip,
        port=6053,
        password=None,
        noise_psk=cfg.api_encryption_key,
        expected_name=cfg.device_name,
    )
    try:
        await asyncio.wait_for(client.connect(login=True), timeout=10)
        _, services = await client.list_entities_services()
        stop = next((s for s in services if s.name == STOP_SERVICE), None)
        if stop is not None:
            await client.execute_service(stop, {})
            print(f"[emergency_stop] sent {STOP_SERVICE}", flush=True)
        else:
            print("[emergency_stop] stop_sunrise service not found on device", flush=True)
    except Exception as e:
        print(f"[emergency_stop] failed: {e!r}", flush=True)
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.device)
    print(
        f"[setup] device={cfg.device_name} ip={cfg.static_ip} light={cfg.sunrise_light_id} "
        f"duration={args.duration}s leave_on={args.leave_on}",
        flush=True,
    )

    if not args.skip_compile:
        compile_device(cfg)
    else:
        print("[compile] skipped (--skip-compile)", flush=True)

    if not args.skip_upload:
        upload_device(cfg)
    else:
        print("[upload] skipped (--skip-upload)", flush=True)

    try:
        outcome = asyncio.run(run_validation(cfg, args))
    except KeyboardInterrupt:
        print("\n[main] KeyboardInterrupt — running emergency stop_sunrise", flush=True)
        try:
            asyncio.run(_emergency_stop(cfg))
        except KeyboardInterrupt:
            pass
        return 130

    write_samples(outcome, cfg, args, args.samples_out)
    checks = run_checks(outcome.samples, outcome.logs, args.duration)
    return print_report(checks, outcome.samples, outcome.logs, use_color=not args.no_color)


if __name__ == "__main__":
    sys.exit(main())
