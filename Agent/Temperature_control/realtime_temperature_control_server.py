#!/usr/bin/env python3
"""Real-time model-predictive temperature control server for the ePBR vessel.

The Flask UI is intentionally self-contained so it can run on the Raspberry Pi
without a frontend build step. The server starts in monitor/idle mode. A user
must submit a setpoint and press Start before heater commands are sent.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import serial
from flask import Flask, jsonify, request

from temperature_model_controller import (
    DEFAULT_MODEL_PATH,
    TemperatureDynamicsModel,
    TemperatureMPCController,
    TemperatureState,
)
from water_temperature_collection import (
    DEFAULT_BAUD,
    DEFAULT_PORT,
    MAX_HEATER_PWM,
    MAX_VALID_TEMP_C,
    MIN_VALID_TEMP_C,
    drain_startup_lines,
    read_temperature,
    send_command,
    set_fan,
    set_heater_pwm,
)


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_DIR = SCRIPT_DIR / "control_runs"
CONTROL_RUN_COLUMNS = [
    "timestamp_iso",
    "elapsed_s",
    "run_id",
    "controller_type",
    "enabled",
    "phase",
    "target_temp_c",
    "vessel_temp_c",
    "jacket_temp_c",
    "jacket_minus_vessel_c",
    "heater_pwm_percent",
    "fan_state",
    "safety_limited",
    "safety_reason",
    "ambient_temp_c",
    "seconds_since_pwm_change",
    "predicted_vessel_t_plus_15s",
    "predicted_jacket_t_plus_15s",
    "predicted_vessel_t_plus_30s",
    "predicted_jacket_t_plus_30s",
    "predicted_vessel_t_plus_60s",
    "predicted_jacket_t_plus_60s",
    "vessel_prediction_error_15s",
    "jacket_prediction_error_15s",
    "vessel_prediction_error_30s",
    "jacket_prediction_error_30s",
    "vessel_prediction_error_60s",
    "jacket_prediction_error_60s",
    "arduino_heater_reply",
    "arduino_fan_reply",
]


@dataclass
class ControlMetrics:
    start_temp_c: float | None = None
    started_at_elapsed_s: float | None = None
    first_reached_elapsed_s: float | None = None
    response_time_s: float | None = None
    max_vessel_temp_c: float | None = None
    max_overshoot_c: float = 0.0
    samples: int = 0
    mean_abs_prediction_error_15s_c: float | None = None
    mean_abs_prediction_error_30s_c: float | None = None
    mean_abs_prediction_error_60s_c: float | None = None


class TemperatureIO:
    def read(self) -> tuple[float, float]:
        raise NotImplementedError

    def set_outputs(self, heater_pwm: int, fan_state: int) -> tuple[str, str]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SerialTemperatureIO(TemperatureIO):
    def __init__(self, port: str, baud: int) -> None:
        self.ser = serial.Serial(port, baud, timeout=2)
        drain_startup_lines(self.ser)
        self.identity = send_command(self.ser, "i")

    def read(self) -> tuple[float, float]:
        return read_temperature(self.ser, "t"), read_temperature(self.ser, "j")

    def set_outputs(self, heater_pwm: int, fan_state: int) -> tuple[str, str]:
        heater_reply = set_heater_pwm(self.ser, max(0, min(MAX_HEATER_PWM, heater_pwm)))
        fan_reply = set_fan(self.ser, bool(fan_state))
        return heater_reply, fan_reply

    def close(self) -> None:
        try:
            self.set_outputs(0, 0)
        finally:
            self.ser.close()


class SimulatedTemperatureIO(TemperatureIO):
    """Simple deterministic-ish thermal simulation for UI and control testing."""

    def __init__(self) -> None:
        self.vessel = 26.7
        self.jacket = 26.2
        self.ambient = 26.5
        self.heater_pwm = 0
        self.fan_state = 0
        self.last_time = time.monotonic()

    def read(self) -> tuple[float, float]:
        now = time.monotonic()
        dt = max(0.1, min(2.0, now - self.last_time))
        self.last_time = now
        heater_drive = self.heater_pwm / 70.0
        jacket_gain = 0.11 * heater_drive
        fan_cooling = 0.045 if self.fan_state else 0.0
        self.jacket += (
            jacket_gain
            - 0.018 * (self.jacket - self.ambient)
            - fan_cooling * max(0.0, self.jacket - self.ambient)
        ) * dt
        self.vessel += (
            0.012 * (self.jacket - self.vessel)
            - 0.004 * (self.vessel - self.ambient)
            - 0.01 * self.fan_state * max(0.0, self.vessel - self.ambient)
        ) * dt
        return (
            round(self.vessel + random.uniform(-0.02, 0.02), 2),
            round(self.jacket + random.uniform(-0.02, 0.02), 2),
        )

    def set_outputs(self, heater_pwm: int, fan_state: int) -> tuple[str, str]:
        self.heater_pwm = max(0, min(MAX_HEATER_PWM, heater_pwm))
        self.fan_state = 1 if fan_state else 0
        return f"SIM:HEATER={self.heater_pwm}", f"SIM:FAN={self.fan_state}"


class ControlLoop:
    def __init__(
        self,
        *,
        model_path: Path,
        log_dir: Path,
        sample_seconds: float,
        port: str,
        baud: int,
        simulate: bool,
        tolerance_c: float,
    ) -> None:
        self.model = TemperatureDynamicsModel.load(model_path)
        self.controller = TemperatureMPCController(self.model)
        self.log_dir = log_dir
        self.sample_seconds = sample_seconds
        self.port = port
        self.baud = baud
        self.simulate = simulate
        self.tolerance_c = tolerance_c
        self.io: TemperatureIO | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.enabled = False
        self.connected = False
        self.target_temp_c = 34.0
        self.run_id = ""
        self.run_path: Path | None = None
        self.run_file: Any = None
        self.writer: csv.DictWriter | None = None
        self.started_monotonic = time.monotonic()
        self.last_pwm_change_elapsed_s = 0.0
        self.heater_pwm = 0
        self.fan_state = 0
        self.heater_reply = ""
        self.fan_reply = ""
        self.ambient_temp_c: float | None = None
        self.vessel_history: deque[float] = deque(maxlen=180)
        self.jacket_history: deque[float] = deque(maxlen=180)
        self.heater_history: deque[int] = deque(maxlen=180)
        self.fan_history: deque[int] = deque(maxlen=180)
        self.pending_predictions: list[dict[str, Any]] = []
        self.prediction_abs_errors = {15: [], 30: [], 60: []}
        self.metrics = ControlMetrics()
        self.status: dict[str, Any] = {
            "mode": "simulate" if simulate else "serial",
            "enabled": False,
            "connected": False,
            "phase": "starting",
            "message": "Server starting",
        }

    def start_background(self) -> None:
        self.thread = threading.Thread(target=self._run, name="temperature-control-loop", daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.set_enabled(False)
        if self.thread:
            self.thread.join(timeout=5)
        self._close_run_file()

    def set_target(self, setpoint_c: float) -> None:
        if not math.isfinite(setpoint_c):
            raise ValueError("setpoint must be finite")
        limits = self.model.limits
        if setpoint_c < limits["min_setpoint_c"] or setpoint_c > limits["max_setpoint_c"]:
            raise ValueError(
                f"setpoint must be between {limits['min_setpoint_c']:.1f} and "
                f"{limits['max_setpoint_c']:.1f} C"
            )
        with self.lock:
            self.target_temp_c = setpoint_c

    def set_enabled(self, enabled: bool) -> None:
        with self.lock:
            if enabled and not self.enabled:
                self._open_new_run_locked()
                if self.vessel_history:
                    self.metrics.start_temp_c = self.vessel_history[-1]
                self.metrics.started_at_elapsed_s = self._elapsed_s()
            self.enabled = enabled
            if not enabled:
                self.heater_pwm = 0
                self.fan_state = 0

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            status = dict(self.status)
            status.update(
                {
                    "enabled": self.enabled,
                    "connected": self.connected,
                    "target_temp_c": self.target_temp_c,
                    "run_id": self.run_id,
                    "run_path": str(self.run_path) if self.run_path else "",
                    "heater_pwm_percent": self.heater_pwm,
                    "fan_state": self.fan_state,
                    "metrics": asdict(self.metrics),
                    "history": {
                        "vessel": list(self.vessel_history)[-180:],
                        "jacket": list(self.jacket_history)[-180:],
                        "heater": list(self.heater_history)[-180:],
                        "fan": list(self.fan_history)[-180:],
                    },
                }
            )
            return status

    def _run(self) -> None:
        try:
            self.io = SimulatedTemperatureIO() if self.simulate else SerialTemperatureIO(self.port, self.baud)
            with self.lock:
                self.connected = True
                self.status.update({"connected": True, "phase": "idle", "message": "Connected"})
            next_sample = time.monotonic()
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now < next_sample:
                    time.sleep(min(0.1, next_sample - now))
                    continue
                next_sample += self.sample_seconds
                self._sample_once()
        except Exception as exc:  # noqa: BLE001 - expose hardware/server faults to UI
            with self.lock:
                self.connected = False
                self.enabled = False
                self.status.update(
                    {
                        "connected": False,
                        "enabled": False,
                        "phase": "fault",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
        finally:
            if self.io:
                self.io.close()
            self._close_run_file()

    def _sample_once(self) -> None:
        assert self.io is not None
        elapsed_s = self._elapsed_s()
        vessel_temp_c, jacket_temp_c = self.io.read()
        valid = self._valid_temperature(vessel_temp_c) and self._valid_temperature(jacket_temp_c)

        with self.lock:
            if self.ambient_temp_c is None and valid:
                self.ambient_temp_c = vessel_temp_c
            target = self.target_temp_c
            enabled = self.enabled
            safety_reason = self._safety_reason(vessel_temp_c, jacket_temp_c, target, valid)

            if safety_reason:
                next_pwm, next_fan = 0, 1
                enabled = False
                self.enabled = False
                phase = "safety_cooling"
            elif enabled:
                action = self.controller.choose_action(
                    TemperatureState(
                        vessel_temp_c=vessel_temp_c,
                        jacket_temp_c=jacket_temp_c,
                        setpoint_c=target,
                        ambient_temp_c=self.ambient_temp_c,
                        elapsed_s=elapsed_s,
                        seconds_since_pwm_change=elapsed_s - self.last_pwm_change_elapsed_s,
                        previous_heater_pwm_percent=self.heater_pwm,
                        previous_fan_state=self.fan_state,
                        vessel_history_c=list(self.vessel_history),
                        jacket_history_c=list(self.jacket_history),
                        heater_history_percent=list(self.heater_history),
                        fan_history=list(self.fan_history),
                    )
                )
                next_pwm = int(action["heater_pwm_percent"])
                next_fan = int(action["fan_state"])
                phase = "model_control"
            else:
                next_pwm, next_fan = 0, 0
                phase = "idle_monitor"

            if next_pwm != self.heater_pwm or next_fan != self.fan_state:
                self.last_pwm_change_elapsed_s = elapsed_s
            self.heater_pwm = next_pwm
            self.fan_state = next_fan

        heater_reply, fan_reply = self.io.set_outputs(next_pwm, next_fan)

        with self.lock:
            self.heater_reply = heater_reply
            self.fan_reply = fan_reply
            self.vessel_history.append(vessel_temp_c)
            self.jacket_history.append(jacket_temp_c)
            self.heater_history.append(next_pwm)
            self.fan_history.append(next_fan)
            prediction = self._prediction_for_current_locked(
                elapsed_s, vessel_temp_c, jacket_temp_c, target
            )
            errors = self._consume_prediction_errors_locked(elapsed_s, vessel_temp_c, jacket_temp_c)
            self._update_metrics_locked(elapsed_s, target, vessel_temp_c, errors)
            self._write_row_locked(
                elapsed_s=elapsed_s,
                phase=phase,
                target=target,
                vessel_temp_c=vessel_temp_c,
                jacket_temp_c=jacket_temp_c,
                enabled=enabled,
                safety_reason=safety_reason,
                prediction=prediction,
                errors=errors,
            )
            self.status.update(
                {
                    "phase": phase,
                    "message": safety_reason or "Running" if enabled else safety_reason or "Idle",
                    "elapsed_s": round(elapsed_s, 2),
                    "vessel_temp_c": vessel_temp_c,
                    "jacket_temp_c": jacket_temp_c,
                    "jacket_minus_vessel_c": round(jacket_temp_c - vessel_temp_c, 2),
                    "prediction": prediction,
                    "safety_reason": safety_reason,
                }
            )

    def _prediction_for_current_locked(
        self, elapsed_s: float, vessel_temp_c: float, jacket_temp_c: float, target: float
    ) -> dict[str, float]:
        state = TemperatureState(
            vessel_temp_c=vessel_temp_c,
            jacket_temp_c=jacket_temp_c,
            setpoint_c=target,
            ambient_temp_c=self.ambient_temp_c,
            elapsed_s=elapsed_s,
            seconds_since_pwm_change=elapsed_s - self.last_pwm_change_elapsed_s,
            previous_heater_pwm_percent=self.heater_pwm,
            previous_fan_state=self.fan_state,
            vessel_history_c=list(self.vessel_history),
            jacket_history_c=list(self.jacket_history),
            heater_history_percent=list(self.heater_history),
            fan_history=list(self.fan_history),
        )
        row = self.controller._row_for_action(state, target, self.heater_pwm, self.fan_state)
        prediction = self.model.predict_temperatures(row)
        self.pending_predictions.append(
            {
                "elapsed_s": elapsed_s,
                "vessel": vessel_temp_c,
                "jacket": jacket_temp_c,
                "prediction": prediction,
            }
        )
        self.pending_predictions = self.pending_predictions[-120:]
        return prediction

    def _consume_prediction_errors_locked(
        self, elapsed_s: float, vessel_temp_c: float, jacket_temp_c: float
    ) -> dict[int, tuple[float, float]]:
        errors: dict[int, tuple[float, float]] = {}
        remaining: list[dict[str, Any]] = []
        for item in self.pending_predictions:
            keep = True
            for horizon in (15, 30, 60):
                key = f"checked_{horizon}"
                if not item.get(key) and elapsed_s - item["elapsed_s"] >= horizon:
                    pred = item["prediction"]
                    vessel_error = vessel_temp_c - pred[f"vessel_temp_t_plus_{horizon}s"]
                    jacket_error = jacket_temp_c - pred[f"jacket_temp_t_plus_{horizon}s"]
                    errors[horizon] = (vessel_error, jacket_error)
                    self.prediction_abs_errors[horizon].append(abs(vessel_error))
                    self.prediction_abs_errors[horizon] = self.prediction_abs_errors[horizon][-500:]
                    item[key] = True
                if horizon == 60 and item.get(key):
                    keep = False
            if keep:
                remaining.append(item)
        self.pending_predictions = remaining
        return errors

    def _update_metrics_locked(
        self,
        elapsed_s: float,
        target: float,
        vessel_temp_c: float,
        errors: dict[int, tuple[float, float]],
    ) -> None:
        self.metrics.samples += 1
        if self.metrics.max_vessel_temp_c is None:
            self.metrics.max_vessel_temp_c = vessel_temp_c
        else:
            self.metrics.max_vessel_temp_c = max(self.metrics.max_vessel_temp_c, vessel_temp_c)
        self.metrics.max_overshoot_c = max(0.0, self.metrics.max_vessel_temp_c - target)
        if (
            self.enabled
            and self.metrics.started_at_elapsed_s is not None
            and self.metrics.first_reached_elapsed_s is None
            and abs(vessel_temp_c - target) <= self.tolerance_c
        ):
            self.metrics.first_reached_elapsed_s = elapsed_s
            self.metrics.response_time_s = elapsed_s - self.metrics.started_at_elapsed_s
        for horizon in errors:
            values = self.prediction_abs_errors[horizon]
            mean_error = sum(values) / len(values) if values else None
            if horizon == 15:
                self.metrics.mean_abs_prediction_error_15s_c = mean_error
            elif horizon == 30:
                self.metrics.mean_abs_prediction_error_30s_c = mean_error
            elif horizon == 60:
                self.metrics.mean_abs_prediction_error_60s_c = mean_error

    def _write_row_locked(
        self,
        *,
        elapsed_s: float,
        phase: str,
        target: float,
        vessel_temp_c: float,
        jacket_temp_c: float,
        enabled: bool,
        safety_reason: str,
        prediction: dict[str, float],
        errors: dict[int, tuple[float, float]],
    ) -> None:
        if self.writer is None:
            return
        row = {
            "timestamp_iso": datetime.now().isoformat(timespec="seconds"),
            "elapsed_s": f"{elapsed_s:.2f}",
            "run_id": self.run_id,
            "controller_type": "model_predictive_control",
            "enabled": int(enabled),
            "phase": phase,
            "target_temp_c": f"{target:.2f}",
            "vessel_temp_c": f"{vessel_temp_c:.2f}",
            "jacket_temp_c": f"{jacket_temp_c:.2f}",
            "jacket_minus_vessel_c": f"{jacket_temp_c - vessel_temp_c:.2f}",
            "heater_pwm_percent": self.heater_pwm,
            "fan_state": self.fan_state,
            "safety_limited": int(bool(safety_reason)),
            "safety_reason": safety_reason,
            "ambient_temp_c": "" if self.ambient_temp_c is None else f"{self.ambient_temp_c:.2f}",
            "seconds_since_pwm_change": f"{elapsed_s - self.last_pwm_change_elapsed_s:.2f}",
            "arduino_heater_reply": self.heater_reply,
            "arduino_fan_reply": self.fan_reply,
        }
        for horizon in (15, 30, 60):
            row[f"predicted_vessel_t_plus_{horizon}s"] = self._fmt_prediction(
                prediction, f"vessel_temp_t_plus_{horizon}s"
            )
            row[f"predicted_jacket_t_plus_{horizon}s"] = self._fmt_prediction(
                prediction, f"jacket_temp_t_plus_{horizon}s"
            )
            vessel_error, jacket_error = errors.get(horizon, (None, None))
            row[f"vessel_prediction_error_{horizon}s"] = self._fmt_float(vessel_error)
            row[f"jacket_prediction_error_{horizon}s"] = self._fmt_float(jacket_error)
        self.writer.writerow(row)
        self.run_file.flush()

    def _open_new_run_locked(self) -> None:
        self._close_run_file()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = datetime.now().strftime("model_control_%Y%m%d_%H%M%S")
        self.run_path = self.log_dir / f"{self.run_id}.csv"
        self.run_file = self.run_path.open("w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.run_file, fieldnames=CONTROL_RUN_COLUMNS)
        self.writer.writeheader()
        self.metrics = ControlMetrics()
        self.pending_predictions = []
        self.prediction_abs_errors = {15: [], 30: [], 60: []}

    def _close_run_file(self) -> None:
        if self.run_file:
            self.run_file.close()
        self.run_file = None
        self.writer = None

    def _safety_reason(
        self, vessel_temp_c: float, jacket_temp_c: float, target_temp_c: float, valid: bool
    ) -> str:
        limits = self.model.limits
        if not valid:
            return "invalid_sensor_temperature"
        if vessel_temp_c >= limits["vessel_emergency_limit_c"]:
            return "vessel_emergency_limit"
        if vessel_temp_c >= limits["vessel_soft_limit_c"]:
            return "vessel_soft_limit"
        model_handoff_error_c = target_temp_c * 0.02
        vessel_inside_model_band = target_temp_c - vessel_temp_c <= model_handoff_error_c
        if vessel_inside_model_band and jacket_temp_c >= limits["jacket_soft_limit_c"]:
            return "jacket_soft_limit"
        return ""

    def _valid_temperature(self, value: float) -> bool:
        return math.isfinite(value) and MIN_VALID_TEMP_C <= value <= MAX_VALID_TEMP_C

    def _elapsed_s(self) -> float:
        return time.monotonic() - self.started_monotonic

    @staticmethod
    def _fmt_float(value: float | None) -> str:
        return "" if value is None else f"{value:.4f}"

    @staticmethod
    def _fmt_prediction(prediction: dict[str, float], key: str) -> str:
        value = prediction.get(key)
        return "" if value is None else f"{value:.4f}"


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ePBR Temperature Control</title>
  <style>
    :root { color-scheme: light; font-family: Arial, sans-serif; }
    body { margin: 0; background: #f6f7f9; color: #1d2430; }
    header { background: #263238; color: white; padding: 16px 24px; }
    main { max-width: 1120px; margin: 0 auto; padding: 20px; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; margin-bottom: 18px; }
    label { display: grid; gap: 6px; font-size: 13px; color: #40505f; }
    input { width: 120px; padding: 9px 10px; border: 1px solid #b8c2cc; border-radius: 6px; font-size: 16px; }
    button { border: 0; border-radius: 6px; padding: 10px 14px; font-size: 15px; cursor: pointer; }
    button.primary { background: #0b6bcb; color: white; }
    button.stop { background: #b42318; color: white; }
    button.secondary { background: #d9e2ec; color: #1d2430; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .panel { background: white; border: 1px solid #d9e2ec; border-radius: 8px; padding: 14px; }
    .metric { font-size: 30px; font-weight: 700; margin-top: 6px; }
    .subtle { color: #607080; font-size: 13px; }
    .wide { grid-column: 1 / -1; }
    canvas { width: 100%; height: 280px; border: 1px solid #d9e2ec; border-radius: 8px; background: white; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; }
    td { padding: 7px 0; border-bottom: 1px solid #edf1f5; }
    td:last-child { text-align: right; font-weight: 600; }
    .status { display: inline-block; padding: 4px 8px; border-radius: 999px; background: #e8eef5; color: #263238; }
    .status.on { background: #d1fadf; color: #05603a; }
    .status.fault { background: #fee4e2; color: #912018; }
    @media (max-width: 850px) { .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
    @media (max-width: 520px) { .grid { grid-template-columns: 1fr; } input { width: 100%; } }
  </style>
</head>
<body>
  <header><h1>ePBR Temperature Control</h1></header>
  <main>
    <section class="toolbar">
      <label>Setpoint C <input id="setpoint" type="number" step="0.1" min="20" max="36" value="34.0"></label>
      <button class="primary" onclick="startRun()">Start</button>
      <button class="stop" onclick="stopRun()">Stop</button>
      <button class="secondary" onclick="setOnly()">Set Only</button>
      <span id="statusBadge" class="status">starting</span>
    </section>
    <section class="grid">
      <div class="panel"><div class="subtle">Vessel</div><div id="vessel" class="metric">--</div></div>
      <div class="panel"><div class="subtle">Jacket</div><div id="jacket" class="metric">--</div></div>
      <div class="panel"><div class="subtle">Heater</div><div id="heater" class="metric">--</div></div>
      <div class="panel"><div class="subtle">Fan</div><div id="fan" class="metric">--</div></div>
      <div class="wide"><canvas id="chart" width="1000" height="280"></canvas></div>
      <div class="panel wide">
        <table>
          <tbody>
            <tr><td>Phase</td><td id="phase">--</td></tr>
            <tr><td>Run log</td><td id="runPath">--</td></tr>
            <tr><td>Response time</td><td id="response">--</td></tr>
            <tr><td>Max overshoot</td><td id="overshoot">--</td></tr>
            <tr><td>Mean abs prediction error, 60s</td><td id="mae60">--</td></tr>
            <tr><td>Message</td><td id="message">--</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    const fmt = (value, suffix = '') => value === null || value === undefined || value === '' ? '--' : `${Number(value).toFixed(2)}${suffix}`;
    async function api(path, body) {
      const res = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body || {})});
      return res.json();
    }
    async function setOnly() { await api('/api/setpoint', {setpoint_c: Number(document.getElementById('setpoint').value)}); }
    async function startRun() { await api('/api/start', {setpoint_c: Number(document.getElementById('setpoint').value)}); }
    async function stopRun() { await api('/api/stop', {}); }
    function draw(history, target) {
      const canvas = document.getElementById('chart');
      const ctx = canvas.getContext('2d');
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      const values = [...(history.vessel || []), ...(history.jacket || []), target].filter(v => v !== null && v !== undefined);
      const min = Math.min(20, ...values) - 1;
      const max = Math.max(45, ...values) + 1;
      const y = v => h - 24 - ((v - min) / (max - min)) * (h - 44);
      const line = (arr, color) => {
        ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
        arr.forEach((v, i) => { const x = 20 + i * ((w - 40) / Math.max(1, arr.length - 1)); i ? ctx.lineTo(x, y(v)) : ctx.moveTo(x, y(v)); });
        ctx.stroke();
      };
      ctx.strokeStyle = '#c8d3df'; ctx.lineWidth = 1;
      [20, 25, 30, 35, 40, 45].forEach(t => { ctx.beginPath(); ctx.moveTo(20, y(t)); ctx.lineTo(w - 20, y(t)); ctx.stroke(); ctx.fillText(`${t}C`, 4, y(t) + 4); });
      ctx.setLineDash([6, 4]); line(Array(Math.max(2, (history.vessel || []).length)).fill(target), '#0b6bcb'); ctx.setLineDash([]);
      line(history.jacket || [], '#b54708'); line(history.vessel || [], '#05603a');
    }
    async function refresh() {
      try {
        const s = await fetch('/api/status').then(r => r.json());
        document.getElementById('vessel').textContent = fmt(s.vessel_temp_c, ' C');
        document.getElementById('jacket').textContent = fmt(s.jacket_temp_c, ' C');
        document.getElementById('heater').textContent = `${s.heater_pwm_percent ?? 0}%`;
        document.getElementById('fan').textContent = s.fan_state ? 'ON' : 'OFF';
        document.getElementById('phase').textContent = s.phase || '--';
        document.getElementById('runPath').textContent = s.run_path || '--';
        document.getElementById('message').textContent = s.message || '--';
        document.getElementById('response').textContent = fmt(s.metrics?.response_time_s, ' s');
        document.getElementById('overshoot').textContent = fmt(s.metrics?.max_overshoot_c, ' C');
        document.getElementById('mae60').textContent = fmt(s.metrics?.mean_abs_prediction_error_60s_c, ' C');
        const badge = document.getElementById('statusBadge');
        badge.textContent = s.enabled ? 'control on' : (s.phase || 'idle');
        badge.className = `status ${s.enabled ? 'on' : s.phase === 'fault' ? 'fault' : ''}`;
        draw(s.history || {}, s.target_temp_c || 34);
      } catch (err) {
        document.getElementById('message').textContent = err.toString();
      }
    }
    setInterval(refresh, 1000);
    refresh();
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the ePBR real-time temperature control UI.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--serial-port", default=DEFAULT_PORT)
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL_PATH)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    parser.add_argument("--sample-seconds", type=float, default=1.0)
    parser.add_argument("--tolerance-c", type=float, default=0.3)
    parser.add_argument("--simulate", action="store_true", help="Run without Arduino hardware.")
    return parser.parse_args()


def create_app(control_loop: ControlLoop) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        return INDEX_HTML

    @app.get("/api/status")
    def status():
        return jsonify(control_loop.snapshot())

    @app.post("/api/setpoint")
    def setpoint():
        payload = request.get_json(force=True) or {}
        control_loop.set_target(float(payload["setpoint_c"]))
        return jsonify({"ok": True, "status": control_loop.snapshot()})

    @app.post("/api/start")
    def start():
        payload = request.get_json(force=True) or {}
        if "setpoint_c" in payload:
            control_loop.set_target(float(payload["setpoint_c"]))
        control_loop.set_enabled(True)
        return jsonify({"ok": True, "status": control_loop.snapshot()})

    @app.post("/api/stop")
    def stop():
        control_loop.set_enabled(False)
        return jsonify({"ok": True, "status": control_loop.snapshot()})

    @app.post("/api/shutdown")
    def shutdown():
        control_loop.shutdown()
        return jsonify({"ok": True})

    return app


def main() -> int:
    args = parse_args()
    control_loop = ControlLoop(
        model_path=args.model,
        log_dir=args.log_dir,
        sample_seconds=args.sample_seconds,
        port=args.serial_port,
        baud=args.baud,
        simulate=args.simulate,
        tolerance_c=args.tolerance_c,
    )
    control_loop.start_background()
    app = create_app(control_loop)
    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        control_loop.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
