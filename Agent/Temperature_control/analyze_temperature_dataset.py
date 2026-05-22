#!/usr/bin/env python3
"""Validate ePBR temperature CSVs and add ML feature columns.

The collection scripts log raw 1 Hz vessel/jacket/action rows. This script
checks that a CSV is usable for model training, prints a compact summary, and
writes a feature-enriched CSV with lag, rate, previous-action, and future-label
columns.
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = SCRIPT_DIR / "data"
MAX_HEATER_PWM_PERCENT = 80

REQUIRED_COLUMNS = [
    "timestamp_iso",
    "elapsed_s",
    "run_id",
    "controller_type",
    "phase",
    "stage_index",
    "target_temp_c",
    "vessel_temp_c",
    "jacket_temp_c",
    "jacket_minus_vessel_c",
    "heater_pwm_percent",
    "requested_heater_pwm_percent",
    "fan_state",
    "jacket_vessel_gap_warning",
    "safety_limited",
    "safety_reason",
    "ambient_temp_c",
    "pwm_step_start_elapsed_s",
    "seconds_since_pwm_change",
    "arduino_heater_reply",
    "arduino_fan_reply",
]

NUMERIC_REQUIRED_COLUMNS = [
    "elapsed_s",
    "vessel_temp_c",
    "jacket_temp_c",
    "heater_pwm_percent",
    "fan_state",
]

FEATURE_COLUMNS = [
    "vessel_rate_c_per_min",
    "jacket_rate_c_per_min",
    "previous_heater_pwm_percent",
    "previous_fan_state",
    "vessel_temp_lag_5s",
    "vessel_temp_lag_15s",
    "vessel_temp_lag_30s",
    "jacket_temp_lag_5s",
    "jacket_temp_lag_15s",
    "jacket_temp_lag_30s",
    "heater_pwm_percent_lag_5s",
    "heater_pwm_percent_lag_15s",
    "heater_pwm_percent_lag_30s",
    "fan_state_lag_5s",
    "fan_state_lag_15s",
    "fan_state_lag_30s",
    "vessel_temp_t_plus_15s",
    "vessel_temp_t_plus_30s",
    "vessel_temp_t_plus_60s",
    "jacket_temp_t_plus_15s",
    "jacket_temp_t_plus_30s",
    "jacket_temp_t_plus_60s",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate ePBR temperature collection CSVs and write feature-enriched "
            "CSV files for ML training."
        )
    )
    parser.add_argument(
        "csv_paths",
        nargs="*",
        type=Path,
        help=(
            "CSV file(s) to analyze. If omitted, all raw CSVs in the local data/ "
            "directory are analyzed."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for enriched CSVs. Defaults to each input file's directory.",
    )
    parser.add_argument(
        "--suffix",
        default="_features",
        help="Suffix inserted before .csv for enriched output files.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print validation summaries without writing feature CSVs.",
    )
    parser.add_argument(
        "--expected-sample-seconds",
        type=float,
        default=1.0,
        help="Expected interval between rows, in seconds.",
    )
    parser.add_argument(
        "--interval-tolerance-s",
        type=float,
        default=0.25,
        help="Allowed absolute interval error before a row is flagged.",
    )
    return parser.parse_args()


def discover_default_csvs() -> list[Path]:
    if not DEFAULT_DATA_DIR.exists():
        return []
    return sorted(
        path
        for path in DEFAULT_DATA_DIR.glob("*.csv")
        if not path.name.endswith("_features.csv")
    )


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8") as file_obj:
        reader = csv.DictReader(file_obj)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    return fieldnames, rows


def parse_float(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def parse_int(value: str) -> int | None:
    parsed = parse_float(value)
    if parsed is None:
        return None
    if not parsed.is_integer():
        return None
    return int(parsed)


def fmt_float(value: float | None, digits: int = 4) -> str:
    if value is None or not math.isfinite(value):
        return ""
    return f"{value:.{digits}f}"


def describe(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "mean": None, "median": None, "max": None}
    return {
        "min": min(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "max": max(values),
    }


def counter_from_rows(rows: Iterable[dict[str, str]], column: str) -> Counter[str]:
    return Counter((row.get(column) or "").strip() or "<blank>" for row in rows)


def numeric_column(rows: list[dict[str, str]], column: str) -> list[float | None]:
    return [parse_float(row.get(column, "")) for row in rows]


def int_column(rows: list[dict[str, str]], column: str) -> list[int | None]:
    return [parse_int(row.get(column, "")) for row in rows]


def count_missing(rows: list[dict[str, str]], columns: list[str]) -> dict[str, int]:
    missing: dict[str, int] = {}
    for column in columns:
        total = 0
        for row in rows:
            if (row.get(column) or "").strip() == "":
                total += 1
        missing[column] = total
    return missing


def validation_issues(
    fieldnames: list[str],
    rows: list[dict[str, str]],
    elapsed: list[float | None],
    heater_pwm: list[int | None],
    fan_state: list[int | None],
    intervals: list[float],
    *,
    expected_sample_seconds: float,
    interval_tolerance_s: float,
) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    missing_columns = [column for column in REQUIRED_COLUMNS if column not in fieldnames]
    if missing_columns:
        issues.append(("FAIL", f"missing required columns: {', '.join(missing_columns)}"))

    missing_numeric = count_missing(rows, NUMERIC_REQUIRED_COLUMNS)
    for column, total in missing_numeric.items():
        if total:
            issues.append(("FAIL", f"{column} has {total} blank value(s)"))

    for index, value in enumerate(elapsed):
        if value is None:
            issues.append(("FAIL", f"elapsed_s is invalid at row {index + 2}"))
            break

    valid_elapsed = [value for value in elapsed if value is not None]
    if len(valid_elapsed) == len(elapsed):
        for index in range(1, len(valid_elapsed)):
            if valid_elapsed[index] <= valid_elapsed[index - 1]:
                issues.append(("FAIL", f"elapsed_s is not strictly increasing at row {index + 2}"))
                break

    bad_intervals = [
        value
        for value in intervals
        if abs(value - expected_sample_seconds) > interval_tolerance_s
    ]
    if bad_intervals:
        issues.append(
            (
                "WARN",
                (
                    f"{len(bad_intervals)} sample interval(s) outside "
                    f"{expected_sample_seconds:.2f} +/- {interval_tolerance_s:.2f} s"
                ),
            )
        )

    invalid_pwm = [
        value
        for value in heater_pwm
        if value is None or value < 0 or value > MAX_HEATER_PWM_PERCENT
    ]
    if invalid_pwm:
        issues.append(
            (
                "FAIL",
                (
                    f"{len(invalid_pwm)} heater_pwm_percent value(s) outside "
                    f"0..{MAX_HEATER_PWM_PERCENT}"
                ),
            )
        )

    invalid_fan = [value for value in fan_state if value not in (0, 1)]
    if invalid_fan:
        issues.append(("FAIL", f"{len(invalid_fan)} fan_state value(s) outside 0/1"))

    unique_pwm = sorted({value for value in heater_pwm if value is not None})
    unique_fan = sorted({value for value in fan_state if value is not None})
    if len(unique_pwm) < 4:
        issues.append(("WARN", f"only {len(unique_pwm)} distinct heater PWM value(s) present"))
    if unique_fan != [0, 1]:
        issues.append(("WARN", f"fan states present are {unique_fan}, expected [0, 1]"))

    phases = {(row.get("phase") or "").strip() for row in rows}
    has_heating = any(value is not None and value > 0 for value in heater_pwm)
    has_coast = any(
        pwm == 0 and fan == 0
        for pwm, fan in zip(heater_pwm, fan_state, strict=False)
        if pwm is not None and fan is not None
    )
    has_cooling = any(fan == 1 for fan in fan_state)
    if not has_heating:
        issues.append(("WARN", "no heating rows present"))
    if not has_coast and "coast_fan_off" not in phases:
        issues.append(("WARN", "no heater-off fan-off coast rows present"))
    if not has_cooling:
        issues.append(("WARN", "no fan-on cooling rows present"))

    return issues


def shifted_value(values: list[float | int | None], index: int, offset: int) -> float | int | None:
    shifted_index = index + offset
    if shifted_index < 0 or shifted_index >= len(values):
        return None
    return values[shifted_index]


def add_feature_columns(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    elapsed = numeric_column(rows, "elapsed_s")
    vessel = numeric_column(rows, "vessel_temp_c")
    jacket = numeric_column(rows, "jacket_temp_c")
    heater_pwm = int_column(rows, "heater_pwm_percent")
    fan_state = int_column(rows, "fan_state")

    enriched_rows: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        enriched = dict(row)
        if index > 0 and elapsed[index] is not None and elapsed[index - 1] is not None:
            dt_s = elapsed[index] - elapsed[index - 1]  # type: ignore[operator]
        else:
            dt_s = 0.0

        if dt_s > 0 and vessel[index] is not None and vessel[index - 1] is not None:
            vessel_rate = (vessel[index] - vessel[index - 1]) / dt_s * 60.0  # type: ignore[operator]
        else:
            vessel_rate = None

        if dt_s > 0 and jacket[index] is not None and jacket[index - 1] is not None:
            jacket_rate = (jacket[index] - jacket[index - 1]) / dt_s * 60.0  # type: ignore[operator]
        else:
            jacket_rate = None

        enriched["vessel_rate_c_per_min"] = fmt_float(vessel_rate)
        enriched["jacket_rate_c_per_min"] = fmt_float(jacket_rate)
        enriched["previous_heater_pwm_percent"] = (
            "" if index == 0 or heater_pwm[index - 1] is None else str(heater_pwm[index - 1])
        )
        enriched["previous_fan_state"] = (
            "" if index == 0 or fan_state[index - 1] is None else str(fan_state[index - 1])
        )

        for lag_s in (5, 15, 30):
            enriched[f"vessel_temp_lag_{lag_s}s"] = fmt_float(
                shifted_value(vessel, index, -lag_s), digits=2
            )
            enriched[f"jacket_temp_lag_{lag_s}s"] = fmt_float(
                shifted_value(jacket, index, -lag_s), digits=2
            )
            pwm_lag = shifted_value(heater_pwm, index, -lag_s)
            fan_lag = shifted_value(fan_state, index, -lag_s)
            enriched[f"heater_pwm_percent_lag_{lag_s}s"] = "" if pwm_lag is None else str(pwm_lag)
            enriched[f"fan_state_lag_{lag_s}s"] = "" if fan_lag is None else str(fan_lag)

        for horizon_s in (15, 30, 60):
            enriched[f"vessel_temp_t_plus_{horizon_s}s"] = fmt_float(
                shifted_value(vessel, index, horizon_s), digits=2
            )
            enriched[f"jacket_temp_t_plus_{horizon_s}s"] = fmt_float(
                shifted_value(jacket, index, horizon_s), digits=2
            )

        enriched_rows.append(enriched)
    return enriched_rows


def output_path_for(input_path: Path, output_dir: Path | None, suffix: str) -> Path:
    directory = output_dir or input_path.parent
    return directory / f"{input_path.stem}{suffix}.csv"


def write_enriched_csv(
    input_fieldnames: list[str],
    rows: list[dict[str, str]],
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(input_fieldnames)
    for column in FEATURE_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)

    with output_path.open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def format_counts(counter: Counter[str], max_items: int = 12) -> str:
    items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    shown = items[:max_items]
    result = ", ".join(f"{key}={value}" for key, value in shown)
    if len(items) > max_items:
        result += f", ... ({len(items) - max_items} more)"
    return result or "<none>"


def print_summary(
    path: Path,
    rows: list[dict[str, str]],
    issues: list[tuple[str, str]],
    *,
    expected_sample_seconds: float,
    interval_tolerance_s: float,
) -> None:
    elapsed = numeric_column(rows, "elapsed_s")
    vessel = [value for value in numeric_column(rows, "vessel_temp_c") if value is not None]
    jacket = [value for value in numeric_column(rows, "jacket_temp_c") if value is not None]
    gap = [value for value in numeric_column(rows, "jacket_minus_vessel_c") if value is not None]
    heater_pwm = int_column(rows, "heater_pwm_percent")
    fan_state = int_column(rows, "fan_state")
    intervals = [
        elapsed[index] - elapsed[index - 1]  # type: ignore[operator]
        for index in range(1, len(elapsed))
        if elapsed[index] is not None and elapsed[index - 1] is not None
    ]

    enriched = add_feature_columns(rows)
    vessel_rates = [
        value
        for value in numeric_column(enriched, "vessel_rate_c_per_min")
        if value is not None
    ]
    jacket_rates = [
        value
        for value in numeric_column(enriched, "jacket_rate_c_per_min")
        if value is not None
    ]
    fan_on_rates = [
        parse_float(row["vessel_rate_c_per_min"])
        for row in enriched
        if row.get("heater_pwm_percent") == "0" and row.get("fan_state") == "1"
    ]
    fan_off_coast_rates = [
        parse_float(row["vessel_rate_c_per_min"])
        for row in enriched
        if row.get("heater_pwm_percent") == "0" and row.get("fan_state") == "0"
    ]
    fan_on_rates = [value for value in fan_on_rates if value is not None]
    fan_off_coast_rates = [value for value in fan_off_coast_rates if value is not None]

    interval_stats = describe(intervals)
    vessel_stats = describe(vessel)
    jacket_stats = describe(jacket)
    gap_stats = describe(gap)
    vessel_rate_stats = describe(vessel_rates)
    jacket_rate_stats = describe(jacket_rates)
    fan_on_stats = describe(fan_on_rates)
    fan_off_stats = describe(fan_off_coast_rates)

    status = "PASS"
    if any(level == "FAIL" for level, _ in issues):
        status = "FAIL"
    elif issues:
        status = "WARN"

    print(f"\n{path}")
    print(f"  Status: {status}")
    print(f"  Rows: {len(rows)}")
    if elapsed and elapsed[0] is not None and elapsed[-1] is not None:
        print(f"  Elapsed: {elapsed[0]:.2f}s to {elapsed[-1]:.2f}s")
    print(
        "  Sample interval: "
        f"mean={fmt_float(interval_stats['mean'])}s "
        f"median={fmt_float(interval_stats['median'])}s "
        f"min={fmt_float(interval_stats['min'])}s "
        f"max={fmt_float(interval_stats['max'])}s "
        f"expected={expected_sample_seconds:.2f}+/-{interval_tolerance_s:.2f}s"
    )
    print(
        "  Vessel C: "
        f"min={fmt_float(vessel_stats['min'], 2)} "
        f"max={fmt_float(vessel_stats['max'], 2)}"
    )
    print(
        "  Jacket C: "
        f"min={fmt_float(jacket_stats['min'], 2)} "
        f"max={fmt_float(jacket_stats['max'], 2)}"
    )
    print(
        "  Jacket-vessel gap C: "
        f"min={fmt_float(gap_stats['min'], 2)} "
        f"max={fmt_float(gap_stats['max'], 2)}"
    )
    print(
        "  Vessel rate C/min: "
        f"min={fmt_float(vessel_rate_stats['min'])} "
        f"mean={fmt_float(vessel_rate_stats['mean'])} "
        f"max={fmt_float(vessel_rate_stats['max'])}"
    )
    print(
        "  Jacket rate C/min: "
        f"min={fmt_float(jacket_rate_stats['min'])} "
        f"mean={fmt_float(jacket_rate_stats['mean'])} "
        f"max={fmt_float(jacket_rate_stats['max'])}"
    )
    print(
        "  Fan-on heater-off vessel rate C/min: "
        f"mean={fmt_float(fan_on_stats['mean'])} "
        f"min={fmt_float(fan_on_stats['min'])} "
        f"max={fmt_float(fan_on_stats['max'])}"
    )
    print(
        "  Fan-off heater-off vessel rate C/min: "
        f"mean={fmt_float(fan_off_stats['mean'])} "
        f"min={fmt_float(fan_off_stats['min'])} "
        f"max={fmt_float(fan_off_stats['max'])}"
    )
    print(f"  Phase counts: {format_counts(counter_from_rows(rows, 'phase'))}")
    print(f"  PWM counts: {format_counts(Counter(str(value) for value in heater_pwm))}")
    print(f"  Fan counts: {format_counts(Counter(str(value) for value in fan_state))}")
    print(f"  Safety limited counts: {format_counts(counter_from_rows(rows, 'safety_limited'))}")
    print(f"  Safety reasons: {format_counts(counter_from_rows(rows, 'safety_reason'))}")

    guard_counts = Counter(
        phase
        for phase in ((row.get("phase") or "").strip() for row in rows)
        if "guard" in phase or "safety" in phase
    )
    print(f"  Guard/safety phase counts: {format_counts(guard_counts)}")
    for level, message in issues:
        print(f"  {level}: {message}")


def analyze_file(path: Path, args: argparse.Namespace) -> bool:
    fieldnames, rows = read_csv(path)
    elapsed = numeric_column(rows, "elapsed_s")
    heater_pwm = int_column(rows, "heater_pwm_percent")
    fan_state = int_column(rows, "fan_state")
    intervals = [
        elapsed[index] - elapsed[index - 1]  # type: ignore[operator]
        for index in range(1, len(elapsed))
        if elapsed[index] is not None and elapsed[index - 1] is not None
    ]
    issues = validation_issues(
        fieldnames,
        rows,
        elapsed,
        heater_pwm,
        fan_state,
        intervals,
        expected_sample_seconds=args.expected_sample_seconds,
        interval_tolerance_s=args.interval_tolerance_s,
    )
    print_summary(
        path,
        rows,
        issues,
        expected_sample_seconds=args.expected_sample_seconds,
        interval_tolerance_s=args.interval_tolerance_s,
    )

    if not args.summary_only:
        enriched_rows = add_feature_columns(rows)
        output_path = output_path_for(path, args.output_dir, args.suffix)
        write_enriched_csv(fieldnames, enriched_rows, output_path)
        print(f"  Wrote: {output_path}")

    return not any(level == "FAIL" for level, _ in issues)


def main() -> int:
    args = parse_args()
    csv_paths = args.csv_paths or discover_default_csvs()
    if not csv_paths:
        print("No CSV files found to analyze.", file=sys.stderr)
        return 2

    all_ok = True
    for path in csv_paths:
        if not path.exists():
            print(f"Missing CSV: {path}", file=sys.stderr)
            all_ok = False
            continue
        try:
            all_ok = analyze_file(path, args) and all_ok
        except Exception as exc:  # noqa: BLE001 - report file-level failures cleanly
            print(f"\n{path}", file=sys.stderr)
            print(f"  FAIL: {exc}", file=sys.stderr)
            all_ok = False

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
