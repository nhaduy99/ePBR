#!/usr/bin/env python3
"""Train a lightweight ePBR vessel/jacket temperature dynamics model.

The Raspberry Pi image currently has numpy available but not scikit-learn, so
this script uses ridge regression with explicit feature engineering. The saved
JSON artifact is intentionally simple enough to load from control scripts
without extra Python packages beyond numpy.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from analyze_temperature_dataset import add_feature_columns, discover_default_csvs


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_DIR = SCRIPT_DIR / "models"
DEFAULT_HORIZONS_S = (15, 30, 60)
MODEL_VERSION = 1

RAW_FEATURE_COLUMNS = [
    "elapsed_s",
    "target_temp_c",
    "vessel_temp_c",
    "jacket_temp_c",
    "jacket_minus_vessel_c",
    "heater_pwm_percent",
    "fan_state",
    "ambient_temp_c",
    "seconds_since_pwm_change",
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
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train a vessel/jacket future-temperature model from collected CSV data."
    )
    parser.add_argument(
        "csv_paths",
        nargs="*",
        type=Path,
        help="Raw or feature CSVs to train on. Defaults to the newest raw CSV in data/.",
    )
    parser.add_argument(
        "--all-raw",
        action="store_true",
        help="Use every raw CSV in data/ instead of only the newest one.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_MODEL_DIR / "temperature_dynamics_model_latest.json",
        help="Path for the trained JSON model artifact.",
    )
    parser.add_argument(
        "--ridge-alpha",
        type=float,
        default=10.0,
        help="L2 regularization strength for ridge regression.",
    )
    parser.add_argument(
        "--test-fraction",
        type=float,
        default=0.25,
        help="Chronological holdout fraction for evaluation.",
    )
    return parser.parse_args()


def latest_raw_csv() -> Path:
    raw_csvs = discover_default_csvs()
    if not raw_csvs:
        raise FileNotFoundError(f"No raw CSV files found in {SCRIPT_DIR / 'data'}")
    return max(raw_csvs, key=lambda path: path.stat().st_mtime)


def selected_csvs(args: argparse.Namespace) -> list[Path]:
    if args.csv_paths:
        return args.csv_paths
    if args.all_raw:
        return discover_default_csvs()
    return [latest_raw_csv()]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as file_obj:
        return [dict(row) for row in csv.DictReader(file_obj)]


def parse_float(row: dict[str, str], column: str) -> float | None:
    value = (row.get(column) or "").strip()
    if not value:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def ensure_features(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not rows:
        return []
    if "vessel_temp_t_plus_60s" in rows[0]:
        return rows
    return add_feature_columns(rows)


def target_columns(horizons_s: tuple[int, ...]) -> list[str]:
    columns: list[str] = []
    for horizon_s in horizons_s:
        columns.append(f"vessel_temp_delta_t_plus_{horizon_s}s")
        columns.append(f"jacket_temp_delta_t_plus_{horizon_s}s")
    return columns


def engineered_feature_names() -> list[str]:
    extra = [
        "heater_fraction",
        "fan_on",
        "vessel_jacket_gap_abs",
        "vessel_x_heater",
        "jacket_x_heater",
        "gap_x_heater",
        "gap_x_fan",
        "vessel_rate_x_heater",
        "jacket_rate_x_fan",
        "heater_changed",
        "fan_changed",
    ]
    return RAW_FEATURE_COLUMNS + extra


def feature_vector(row: dict[str, str]) -> list[float] | None:
    raw_values: dict[str, float] = {}
    for column in RAW_FEATURE_COLUMNS:
        value = parse_float(row, column)
        if value is None:
            return None
        raw_values[column] = value

    heater = raw_values["heater_pwm_percent"]
    fan = raw_values["fan_state"]
    vessel = raw_values["vessel_temp_c"]
    jacket = raw_values["jacket_temp_c"]
    gap = raw_values["jacket_minus_vessel_c"]
    vessel_rate = raw_values["vessel_rate_c_per_min"]
    jacket_rate = raw_values["jacket_rate_c_per_min"]
    previous_heater = raw_values["previous_heater_pwm_percent"]
    previous_fan = raw_values["previous_fan_state"]

    engineered = [
        heater / 70.0,
        fan,
        abs(gap),
        vessel * heater / 70.0,
        jacket * heater / 70.0,
        gap * heater / 70.0,
        gap * fan,
        vessel_rate * heater / 70.0,
        jacket_rate * fan,
        1.0 if heater != previous_heater else 0.0,
        1.0 if fan != previous_fan else 0.0,
    ]
    return [raw_values[column] for column in RAW_FEATURE_COLUMNS] + engineered


def target_vector(row: dict[str, str], horizons_s: tuple[int, ...]) -> list[float] | None:
    vessel_now = parse_float(row, "vessel_temp_c")
    jacket_now = parse_float(row, "jacket_temp_c")
    if vessel_now is None or jacket_now is None:
        return None

    values: list[float] = []
    for horizon_s in horizons_s:
        vessel_future = parse_float(row, f"vessel_temp_t_plus_{horizon_s}s")
        jacket_future = parse_float(row, f"jacket_temp_t_plus_{horizon_s}s")
        if vessel_future is None or jacket_future is None:
            return None
        values.extend([vessel_future - vessel_now, jacket_future - jacket_now])
    return values


def build_matrix(
    csv_paths: list[Path], horizons_s: tuple[int, ...]
) -> tuple[np.ndarray, np.ndarray, list[dict[str, Any]]]:
    x_rows: list[list[float]] = []
    y_rows: list[list[float]] = []
    source_summaries: list[dict[str, Any]] = []

    for path in csv_paths:
        rows = ensure_features(read_rows(path))
        usable = 0
        for row in rows:
            features = feature_vector(row)
            targets = target_vector(row, horizons_s)
            if features is None or targets is None:
                continue
            x_rows.append(features)
            y_rows.append(targets)
            usable += 1
        source_summaries.append(
            {
                "path": str(path),
                "rows": len(rows),
                "usable_training_rows": usable,
                "modified_time": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            }
        )

    if not x_rows:
        raise ValueError("No usable training rows after dropping incomplete feature/label rows")
    return np.array(x_rows, dtype=float), np.array(y_rows, dtype=float), source_summaries


def chronological_split(
    x_values: np.ndarray, y_values: np.ndarray, test_fraction: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if not 0.0 < test_fraction < 0.8:
        raise ValueError("--test-fraction must be greater than 0 and less than 0.8")
    test_rows = max(1, int(round(len(x_values) * test_fraction)))
    train_rows = len(x_values) - test_rows
    if train_rows < 100:
        raise ValueError("Not enough rows for a chronological train/test split")
    return (
        x_values[:train_rows],
        y_values[:train_rows],
        x_values[train_rows:],
        y_values[train_rows:],
    )


def fit_ridge(
    x_train: np.ndarray, y_train: np.ndarray, ridge_alpha: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    x_mean = x_train.mean(axis=0)
    x_std = x_train.std(axis=0)
    x_std[x_std < 1e-9] = 1.0
    y_mean = y_train.mean(axis=0)

    x_scaled = (x_train - x_mean) / x_std
    x_design = np.column_stack([np.ones(len(x_scaled)), x_scaled])

    penalty = np.eye(x_design.shape[1]) * ridge_alpha
    penalty[0, 0] = 0.0
    weights = np.linalg.solve(x_design.T @ x_design + penalty, x_design.T @ (y_train - y_mean))
    return weights, x_mean, x_std, y_mean


def predict_delta(
    x_values: np.ndarray, weights: np.ndarray, x_mean: np.ndarray, x_std: np.ndarray, y_mean: np.ndarray
) -> np.ndarray:
    x_scaled = (x_values - x_mean) / x_std
    x_design = np.column_stack([np.ones(len(x_scaled)), x_scaled])
    return x_design @ weights + y_mean


def metrics(y_true: np.ndarray, y_pred: np.ndarray, columns: list[str]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for index, column in enumerate(columns):
        error = y_pred[:, index] - y_true[:, index]
        mae = float(np.mean(np.abs(error)))
        rmse = float(np.sqrt(np.mean(error**2)))
        result[column] = {"mae_c": round(mae, 4), "rmse_c": round(rmse, 4)}
    return result


def artifact_dict(
    *,
    csv_paths: list[Path],
    source_summaries: list[dict[str, Any]],
    feature_names: list[str],
    target_names: list[str],
    weights: np.ndarray,
    x_mean: np.ndarray,
    x_std: np.ndarray,
    y_mean: np.ndarray,
    train_metrics: dict[str, dict[str, float]],
    test_metrics: dict[str, dict[str, float]],
    ridge_alpha: float,
) -> dict[str, Any]:
    return {
        "model_version": MODEL_VERSION,
        "model_type": "engineered_ridge_regression_delta_temperature",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "training_data": {
            "selected_paths": [str(path) for path in csv_paths],
            "sources": source_summaries,
        },
        "limits": {
            "min_setpoint_c": 20.0,
            "max_setpoint_c": 36.0,
            "max_heater_pwm_percent": 70,
            "vessel_soft_limit_c": 36.5,
            "vessel_emergency_limit_c": 38.0,
            "jacket_soft_limit_c": 45.0,
            "max_jacket_vessel_gap_c": 8.0,
        },
        "feature_names": feature_names,
        "raw_feature_columns": RAW_FEATURE_COLUMNS,
        "target_names": target_names,
        "ridge_alpha": ridge_alpha,
        "x_mean": x_mean.tolist(),
        "x_std": x_std.tolist(),
        "y_mean": y_mean.tolist(),
        "weights": weights.tolist(),
        "metrics": {
            "train": train_metrics,
            "chronological_holdout": test_metrics,
        },
    }


def print_metrics(title: str, values: dict[str, dict[str, float]]) -> None:
    print(title)
    for name, row in values.items():
        print(f"  {name}: MAE={row['mae_c']:.4f} C RMSE={row['rmse_c']:.4f} C")


def main() -> int:
    args = parse_args()
    csv_paths = selected_csvs(args)
    missing = [path for path in csv_paths if not path.exists()]
    if missing:
        print(f"Missing CSV path(s): {', '.join(str(path) for path in missing)}", file=sys.stderr)
        return 2

    horizons_s = tuple(int(value) for value in DEFAULT_HORIZONS_S)
    x_values, y_values, source_summaries = build_matrix(csv_paths, horizons_s)
    x_train, y_train, x_test, y_test = chronological_split(x_values, y_values, args.test_fraction)
    weights, x_mean, x_std, y_mean = fit_ridge(x_train, y_train, args.ridge_alpha)

    y_train_pred = predict_delta(x_train, weights, x_mean, x_std, y_mean)
    y_test_pred = predict_delta(x_test, weights, x_mean, x_std, y_mean)
    target_names = target_columns(horizons_s)
    train_metrics = metrics(y_train, y_train_pred, target_names)
    test_metrics = metrics(y_test, y_test_pred, target_names)

    artifact = artifact_dict(
        csv_paths=csv_paths,
        source_summaries=source_summaries,
        feature_names=engineered_feature_names(),
        target_names=target_names,
        weights=weights,
        x_mean=x_mean,
        x_std=x_std,
        y_mean=y_mean,
        train_metrics=train_metrics,
        test_metrics=test_metrics,
        ridge_alpha=args.ridge_alpha,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as file_obj:
        json.dump(artifact, file_obj, indent=2)
        file_obj.write("\n")

    print(f"Training rows: {len(x_train)}")
    print(f"Holdout rows: {len(x_test)}")
    print(f"Model written: {args.output}")
    print_metrics("Train metrics:", train_metrics)
    print_metrics("Chronological holdout metrics:", test_metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
