#!/usr/bin/env python3
"""10-minute ePBR temperature smoke test.

Runs three heater PWM stages followed by one fan chilling stage, while logging
vessel and jacket temperature from the Arduino USB serial interface.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

import serial


DEFAULT_PWM_STAGES = [20, 40, 60]
MAX_HEATER_PWM = 70
DEFAULT_HEAT_STAGE_SECONDS = 120
DEFAULT_CHILL_SECONDS = 240
DEFAULT_SAMPLE_SECONDS = 1.0
DEFAULT_MAX_TEMP_C = 36.5
DEFAULT_MAX_JACKET_TEMP_C = 45.0
DEFAULT_PORT = "/dev/ttyACM0"
BAUD = 115200


class ArduinoProtocolError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a 10-minute temperature smoke test: three heater PWM stages "
            "and one fan chilling stage."
        )
    )
    parser.add_argument("--port", default=DEFAULT_PORT, help="Arduino serial port")
    parser.add_argument("--baud", type=int, default=BAUD, help="Serial baud rate")
    parser.add_argument(
        "--pwm",
        nargs=3,
        type=int,
        default=DEFAULT_PWM_STAGES,
        metavar=("PWM1", "PWM2", "PWM3"),
        help="Three heater PWM percentages. Each must be 0..70.",
    )
    parser.add_argument(
        "--heat-stage-seconds",
        type=int,
        default=DEFAULT_HEAT_STAGE_SECONDS,
        help="Seconds for each heater PWM stage.",
    )
    parser.add_argument(
        "--chill-seconds",
        type=int,
        default=DEFAULT_CHILL_SECONDS,
        help="Seconds for the fan chilling stage.",
    )
    parser.add_argument(
        "--sample-seconds",
        type=float,
        default=DEFAULT_SAMPLE_SECONDS,
        help="Temperature logging interval in seconds.",
    )
    parser.add_argument(
        "--max-temp-c",
        type=float,
        default=DEFAULT_MAX_TEMP_C,
        help="Turn heater off if vessel temperature reaches this value.",
    )
    parser.add_argument(
        "--max-jacket-temp-c",
        type=float,
        default=DEFAULT_MAX_JACKET_TEMP_C,
        help="Turn heater off if jacket temperature reaches this value.",
    )
    parser.add_argument(
        "--output-dir",
        default="/home/pi-epbr/projects/Agent/Temperature_control/data",
        help="Directory where the CSV log will be written.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional run ID. Defaults to smoke_YYYYmmdd_HHMMSS.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for pwm in args.pwm:
        if pwm < 0 or pwm > MAX_HEATER_PWM:
            raise ValueError(f"PWM value {pwm} is outside the allowed 0..{MAX_HEATER_PWM}% range")
    if args.heat_stage_seconds <= 0:
        raise ValueError("--heat-stage-seconds must be positive")
    if args.chill_seconds <= 0:
        raise ValueError("--chill-seconds must be positive")
    if args.sample_seconds <= 0:
        raise ValueError("--sample-seconds must be positive")


def read_line(ser: serial.Serial) -> str:
    line = ser.readline().decode("utf-8", errors="replace").strip()
    if not line:
        raise ArduinoProtocolError("Timed out waiting for Arduino reply")
    return line


def send_command(ser: serial.Serial, command: str) -> str:
    ser.write((command + "\n").encode("ascii"))
    ser.flush()
    return read_line(ser)


def read_temperature(ser: serial.Serial, command: str) -> float:
    reply = send_command(ser, command)
    try:
        return float(reply)
    except ValueError as exc:
        raise ArduinoProtocolError(f"Expected numeric reply for {command!r}, got {reply!r}") from exc


def set_heater_pwm(ser: serial.Serial, pwm_percent: int) -> str:
    pwm_percent = max(0, min(MAX_HEATER_PWM, pwm_percent))
    return send_command(ser, f"a{pwm_percent:02d}")


def set_fan(ser: serial.Serial, enabled: bool) -> str:
    return send_command(ser, "d" if enabled else "k")


def safe_shutdown(ser: serial.Serial) -> None:
    for command in ("a00", "k"):
        try:
            send_command(ser, command)
        except Exception as exc:  # noqa: BLE001 - best effort shutdown
            print(f"WARNING: failed to send {command}: {exc}", file=sys.stderr)


def drain_startup_lines(ser: serial.Serial) -> list[str]:
    time.sleep(2.2)
    lines = []
    while ser.in_waiting:
        lines.append(ser.readline().decode("utf-8", errors="replace").strip())
    return lines


def phase_plan(args: argparse.Namespace) -> list[dict[str, object]]:
    phases: list[dict[str, object]] = []
    for index, pwm in enumerate(args.pwm, start=1):
        phases.append(
            {
                "phase": f"heat_pwm_{pwm:02d}",
                "duration_s": args.heat_stage_seconds,
                "heater_pwm_percent": pwm,
                "fan_state": 0,
                "stage_index": index,
            }
        )
    phases.append(
        {
            "phase": "chill_fan_on",
            "duration_s": args.chill_seconds,
            "heater_pwm_percent": 0,
            "fan_state": 1,
            "stage_index": 4,
        }
    )
    return phases


def open_writer(output_dir: Path, run_id: str):
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{run_id}.csv"
    file_obj = output_path.open("w", newline="", encoding="utf-8")
    fieldnames = [
        "timestamp_iso",
        "elapsed_s",
        "run_id",
        "phase",
        "stage_index",
        "vessel_temp_c",
        "jacket_temp_c",
        "heater_pwm_percent",
        "fan_state",
        "safety_limited",
        "safety_reason",
        "arduino_heater_reply",
        "arduino_fan_reply",
    ]
    writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
    writer.writeheader()
    return output_path, file_obj, writer


def run_test(args: argparse.Namespace) -> Path:
    run_id = args.run_id or datetime.now().strftime("smoke_%Y%m%d_%H%M%S")
    output_path, file_obj, writer = open_writer(Path(args.output_dir), run_id)
    start_monotonic = time.monotonic()
    safety_limited = False
    safety_reason = ""

    with file_obj:
        with serial.Serial(args.port, args.baud, timeout=2) as ser:
            startup_lines = drain_startup_lines(ser)
            if startup_lines:
                print("Startup:", " | ".join(startup_lines))

            identity = send_command(ser, "i")
            print(f"Connected to Arduino device: {identity}")

            try:
                for phase in phase_plan(args):
                    if safety_limited:
                        requested_pwm = 0
                        fan_state = 1
                    else:
                        requested_pwm = int(phase["heater_pwm_percent"])
                        fan_state = int(phase["fan_state"])
                    heater_reply = set_heater_pwm(ser, requested_pwm)
                    fan_reply = set_fan(ser, bool(fan_state))
                    phase_start = time.monotonic()
                    next_sample = phase_start

                    print(
                        f"Phase {phase['stage_index']}: "
                        f"{'safety_cooling' if safety_limited else phase['phase']} "
                        f"for {phase['duration_s']}s, heater={requested_pwm}%, fan={fan_state}"
                    )

                    while time.monotonic() - phase_start < int(phase["duration_s"]):
                        now = time.monotonic()
                        if now < next_sample:
                            time.sleep(min(0.1, next_sample - now))
                            continue

                        vessel_temp_c = read_temperature(ser, "t")
                        jacket_temp_c = read_temperature(ser, "j")

                        if (
                            requested_pwm > 0
                            and (
                                vessel_temp_c >= args.max_temp_c
                                or jacket_temp_c >= args.max_jacket_temp_c
                            )
                        ):
                            safety_limited = True
                            if vessel_temp_c >= args.max_temp_c:
                                safety_reason = "vessel_temperature_limit"
                            else:
                                safety_reason = "jacket_temperature_limit"
                            requested_pwm = 0
                            heater_reply = set_heater_pwm(ser, 0)
                            fan_state = 1
                            fan_reply = set_fan(ser, True)
                            print(
                                "Safety limit reached. Heater forced to 0%, fan forced ON. "
                                f"vessel={vessel_temp_c:.2f} C, jacket={jacket_temp_c:.2f} C"
                            )

                        elapsed_s = time.monotonic() - start_monotonic
                        row_phase = "safety_cooling" if safety_limited else phase["phase"]
                        writer.writerow(
                            {
                                "timestamp_iso": datetime.now().isoformat(timespec="seconds"),
                                "elapsed_s": f"{elapsed_s:.2f}",
                                "run_id": run_id,
                                "phase": row_phase,
                                "stage_index": phase["stage_index"],
                                "vessel_temp_c": f"{vessel_temp_c:.2f}",
                                "jacket_temp_c": f"{jacket_temp_c:.2f}",
                                "heater_pwm_percent": requested_pwm,
                                "fan_state": fan_state,
                                "safety_limited": int(safety_limited),
                                "safety_reason": safety_reason,
                                "arduino_heater_reply": heater_reply,
                                "arduino_fan_reply": fan_reply,
                            }
                        )
                        file_obj.flush()

                        print(
                            f"{elapsed_s:7.1f}s {row_phase:>14} "
                            f"T={vessel_temp_c:5.2f} C J={jacket_temp_c:5.2f} C "
                            f"PWM={requested_pwm:02d}% fan={fan_state}"
                        )
                        next_sample += args.sample_seconds
            finally:
                safe_shutdown(ser)

    return output_path


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        output_path = run_test(args)
    except KeyboardInterrupt:
        print("Interrupted by user. Heater and fan shutdown attempted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI should show concise failure
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Smoke test complete. Data written to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
