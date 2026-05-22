#!/usr/bin/env python3
"""Runtime helper for model-predictive ePBR temperature control decisions."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from train_temperature_model import RAW_FEATURE_COLUMNS, feature_vector


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL_PATH = SCRIPT_DIR / "models" / "temperature_dynamics_model_latest.json"
DEFAULT_ACTIONS = [(pwm, 0) for pwm in (0, 10, 20, 30, 40, 50, 60, 70, 80)] + [(0, 1)]
MODEL_HANDOFF_FRACTION = 0.02


@dataclass
class TemperatureState:
    vessel_temp_c: float
    jacket_temp_c: float
    setpoint_c: float
    ambient_temp_c: float | None = None
    elapsed_s: float = 0.0
    seconds_since_pwm_change: float = 0.0
    previous_heater_pwm_percent: int = 0
    previous_fan_state: int = 0
    vessel_history_c: list[float] = field(default_factory=list)
    jacket_history_c: list[float] = field(default_factory=list)
    heater_history_percent: list[int] = field(default_factory=list)
    fan_history: list[int] = field(default_factory=list)


class TemperatureDynamicsModel:
    def __init__(self, artifact: dict[str, Any]) -> None:
        self.artifact = artifact
        self.feature_names = artifact["feature_names"]
        self.target_names = artifact["target_names"]
        self.limits = artifact["limits"]
        self.x_mean = np.array(artifact["x_mean"], dtype=float)
        self.x_std = np.array(artifact["x_std"], dtype=float)
        self.y_mean = np.array(artifact["y_mean"], dtype=float)
        self.weights = np.array(artifact["weights"], dtype=float)

    @classmethod
    def load(cls, path: Path) -> "TemperatureDynamicsModel":
        with path.open("r", encoding="utf-8") as file_obj:
            return cls(json.load(file_obj))

    def predict_deltas(self, row: dict[str, str]) -> dict[str, float]:
        values = feature_vector(row)
        if values is None:
            missing = [column for column in RAW_FEATURE_COLUMNS if not row.get(column)]
            raise ValueError(f"Cannot predict with incomplete feature row. Missing: {missing}")
        x_values = np.array(values, dtype=float)
        x_scaled = (x_values - self.x_mean) / self.x_std
        x_design = np.concatenate(([1.0], x_scaled))
        prediction = x_design @ self.weights + self.y_mean
        return {
            name: float(value)
            for name, value in zip(self.target_names, prediction.tolist(), strict=True)
        }

    def predict_temperatures(self, row: dict[str, str]) -> dict[str, float]:
        vessel = float(row["vessel_temp_c"])
        jacket = float(row["jacket_temp_c"])
        deltas = self.predict_deltas(row)
        result: dict[str, float] = {}
        for name, delta in deltas.items():
            if name.startswith("vessel_"):
                result[name.replace("_delta", "")] = vessel + delta
            elif name.startswith("jacket_"):
                result[name.replace("_delta", "")] = jacket + delta
        return result


class TemperatureMPCController:
    def __init__(self, model: TemperatureDynamicsModel) -> None:
        self.model = model

    def choose_action(self, state: TemperatureState) -> dict[str, Any]:
        setpoint = min(
            self.model.limits["max_setpoint_c"],
            max(self.model.limits["min_setpoint_c"], state.setpoint_c),
        )
        approach_action = self._fast_approach_action(state, setpoint)
        if approach_action is not None:
            return approach_action

        candidates: list[dict[str, Any]] = []
        for heater_pwm, fan_state in DEFAULT_ACTIONS:
            row = self._row_for_action(state, setpoint, heater_pwm, fan_state)
            hard_rejection = self._hard_rejection(state, heater_pwm, fan_state)
            if hard_rejection:
                candidates.append(
                    {
                        "heater_pwm_percent": 0,
                        "fan_state": 1,
                        "score": math.inf,
                        "rejected": hard_rejection,
                    }
                )
                continue
            prediction = self.model.predict_temperatures(row)
            score = self._score(state, setpoint, heater_pwm, fan_state, prediction)
            candidates.append(
                {
                    "heater_pwm_percent": heater_pwm,
                    "fan_state": fan_state,
                    "score": score,
                    "prediction": prediction,
                    "rejected": "",
                }
            )

        valid = [candidate for candidate in candidates if not candidate["rejected"]]
        if not valid:
            return {
                "heater_pwm_percent": 0,
                "fan_state": 1,
                "reason": "hard safety fallback",
                "candidates": candidates,
            }
        if state.vessel_temp_c >= setpoint:
            no_heat_candidates = [
                candidate for candidate in valid if candidate["heater_pwm_percent"] == 0
            ]
            if no_heat_candidates:
                valid = no_heat_candidates
        if not self._fan_allowed(state, setpoint):
            non_fan_candidates = [candidate for candidate in valid if candidate["fan_state"] == 0]
            if non_fan_candidates:
                valid = non_fan_candidates
        warmup_candidates = self._warmup_candidates(state, setpoint, valid)
        if warmup_candidates:
            best = min(warmup_candidates, key=lambda candidate: candidate["score"])
            best["reason"] = "bounded fast warmup"
            best["candidates"] = sorted(valid, key=lambda candidate: candidate["score"])
            return best
        best = min(valid, key=lambda candidate: candidate["score"])
        best["reason"] = "lowest model-predictive score"
        best["candidates"] = sorted(valid, key=lambda candidate: candidate["score"])
        return best

    def _model_handoff_error_c(self, setpoint: float) -> float:
        return setpoint * MODEL_HANDOFF_FRACTION

    def _fast_approach_action(
        self, state: TemperatureState, setpoint: float
    ) -> dict[str, Any] | None:
        """Use explicit fast heating until the vessel is close enough for MPC.

        The model is useful near target, but the initial warmup has long thermal
        lag. A short-horizon model can otherwise coast too early while the
        vessel is still far below the requested temperature.
        """
        error_c = setpoint - state.vessel_temp_c
        if error_c <= self._model_handoff_error_c(setpoint):
            return None

        limits = self.model.limits
        if state.vessel_temp_c >= limits["vessel_soft_limit_c"]:
            return self._predicted_action(state, setpoint, 0, 1, "vessel safety cooling")

        if error_c >= 10.0:
            heater_pwm = 80
        elif error_c >= 6.0:
            heater_pwm = 70
        else:
            heater_pwm = 60
        return self._predicted_action(state, setpoint, heater_pwm, 0, "fast approach heating")

    def _predicted_action(
        self,
        state: TemperatureState,
        setpoint: float,
        heater_pwm: int,
        fan_state: int,
        reason: str,
    ) -> dict[str, Any]:
        row = self._row_for_action(state, setpoint, heater_pwm, fan_state)
        prediction = self.model.predict_temperatures(row)
        return {
            "heater_pwm_percent": heater_pwm,
            "fan_state": fan_state,
            "score": self._score(state, setpoint, heater_pwm, fan_state, prediction),
            "prediction": prediction,
            "rejected": "",
            "reason": reason,
            "candidates": [],
        }

    def _fan_allowed(self, state: TemperatureState, setpoint: float) -> bool:
        limits = self.model.limits
        if state.vessel_temp_c >= limits["vessel_soft_limit_c"]:
            return True
        if state.vessel_temp_c >= setpoint + 1.0:
            return True
        if state.previous_fan_state and state.vessel_temp_c >= setpoint + 0.5:
            return True
        return False

    def _warmup_candidates(
        self, state: TemperatureState, setpoint: float, valid: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Force useful heat while far below target and still inside jacket limits.

        The learned model predicts only 15/30/60 seconds ahead. On the real
        vessel, heat can sit in the jacket for longer than that before the
        vessel visibly responds, so pure short-horizon scoring can incorrectly
        prefer coasting during initial warmup. This bounded override only
        applies when there is clear thermal headroom.
        """
        error_c = setpoint - state.vessel_temp_c
        if error_c <= self._model_handoff_error_c(setpoint):
            return []

        if error_c >= 8.0:
            min_pwm = 50
        elif error_c >= 5.0:
            min_pwm = 40
        else:
            min_pwm = 20
        return [
            candidate
            for candidate in valid
            if candidate["fan_state"] == 0 and candidate["heater_pwm_percent"] >= min_pwm
        ]

    def _row_for_action(
        self, state: TemperatureState, setpoint: float, heater_pwm: int, fan_state: int
    ) -> dict[str, str]:
        ambient = state.ambient_temp_c
        if ambient is None:
            ambient = state.vessel_history_c[0] if state.vessel_history_c else state.vessel_temp_c

        vessel_history = state.vessel_history_c or [state.vessel_temp_c]
        jacket_history = state.jacket_history_c or [state.jacket_temp_c]
        heater_history = state.heater_history_percent or [state.previous_heater_pwm_percent]
        fan_history = state.fan_history or [state.previous_fan_state]

        def lag(values: list[float] | list[int], seconds: int, fallback: float | int) -> float | int:
            if len(values) > seconds:
                return values[-seconds - 1]
            return fallback

        vessel_1s_ago = lag(vessel_history, 1, state.vessel_temp_c)
        jacket_1s_ago = lag(jacket_history, 1, state.jacket_temp_c)
        vessel_rate = (state.vessel_temp_c - float(vessel_1s_ago)) * 60.0
        jacket_rate = (state.jacket_temp_c - float(jacket_1s_ago)) * 60.0
        gap = state.jacket_temp_c - state.vessel_temp_c

        values = {
            "elapsed_s": state.elapsed_s,
            "target_temp_c": setpoint,
            "vessel_temp_c": state.vessel_temp_c,
            "jacket_temp_c": state.jacket_temp_c,
            "jacket_minus_vessel_c": gap,
            "heater_pwm_percent": heater_pwm,
            "fan_state": fan_state,
            "ambient_temp_c": ambient,
            "seconds_since_pwm_change": state.seconds_since_pwm_change,
            "vessel_rate_c_per_min": vessel_rate,
            "jacket_rate_c_per_min": jacket_rate,
            "previous_heater_pwm_percent": state.previous_heater_pwm_percent,
            "previous_fan_state": state.previous_fan_state,
        }
        for seconds in (5, 15, 30):
            values[f"vessel_temp_lag_{seconds}s"] = lag(vessel_history, seconds, state.vessel_temp_c)
            values[f"jacket_temp_lag_{seconds}s"] = lag(jacket_history, seconds, state.jacket_temp_c)
            values[f"heater_pwm_percent_lag_{seconds}s"] = lag(heater_history, seconds, state.previous_heater_pwm_percent)
            values[f"fan_state_lag_{seconds}s"] = lag(fan_history, seconds, state.previous_fan_state)
        return {key: f"{value:.6f}" for key, value in values.items()}

    def _hard_rejection(self, state: TemperatureState, heater_pwm: int, fan_state: int) -> str:
        limits = self.model.limits
        if state.vessel_temp_c >= limits["vessel_soft_limit_c"]:
            return "vessel soft limit requires heater off and fan on"
        if heater_pwm > limits["max_heater_pwm_percent"]:
            return "heater PWM above model limit"
        if fan_state not in (0, 1):
            return "invalid fan state"
        return ""

    def _score(
        self,
        state: TemperatureState,
        setpoint: float,
        heater_pwm: int,
        fan_state: int,
        prediction: dict[str, float],
    ) -> float:
        vessel_15 = prediction["vessel_temp_t_plus_15s"]
        vessel_30 = prediction["vessel_temp_t_plus_30s"]
        vessel_60 = prediction["vessel_temp_t_plus_60s"]
        current_error = setpoint - state.vessel_temp_c
        future_error = abs(setpoint - vessel_60)
        short_error = abs(setpoint - vessel_30)
        progress_penalty = max(0.0, current_error - (vessel_30 - state.vessel_temp_c))
        overshoot_penalty = max(0.0, vessel_15 - setpoint, vessel_30 - setpoint, vessel_60 - setpoint)

        return (
            future_error * 5.0
            + short_error * 1.5
            + progress_penalty * 0.8
            + overshoot_penalty * 30.0
            + heater_pwm * 0.01
            + fan_state * 1.5
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recommend one heater/fan action from the saved model.")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--vessel", type=float, required=True, help="Current vessel temperature C")
    parser.add_argument("--jacket", type=float, required=True, help="Current jacket temperature C")
    parser.add_argument("--setpoint", type=float, required=True, help="Requested vessel setpoint C")
    parser.add_argument("--ambient", type=float, default=None, help="Ambient/initial temperature C")
    parser.add_argument("--previous-heater", type=int, default=0)
    parser.add_argument("--previous-fan", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    model = TemperatureDynamicsModel.load(args.model)
    controller = TemperatureMPCController(model)
    state = TemperatureState(
        vessel_temp_c=args.vessel,
        jacket_temp_c=args.jacket,
        setpoint_c=args.setpoint,
        ambient_temp_c=args.ambient,
        previous_heater_pwm_percent=args.previous_heater,
        previous_fan_state=args.previous_fan,
    )
    action = controller.choose_action(state)
    printable = {
        "heater_pwm_percent": action["heater_pwm_percent"],
        "fan_state": action["fan_state"],
        "score": None if math.isinf(action.get("score", math.inf)) else round(action["score"], 4),
        "reason": action["reason"],
        "prediction": {
            key: round(value, 3)
            for key, value in action.get("prediction", {}).items()
        },
    }
    print(json.dumps(printable, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
