#!/usr/bin/env python3
"""Water-loaded ePBR temperature dynamics data collection.

Collects 1 Hz water-loaded datasets using selectable heater, fan, and coast
excitation profiles. The output is intended for training a predictive dynamics
model that can later choose fast, safe actions for user setpoints from 20 to
36 C.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import serial


DEFAULT_PORT = "/dev/ttyACM0"
DEFAULT_BAUD = 115200
DEFAULT_OUTPUT_DIR = "/home/pi-epbr/projects/Agent/Temperature_control/data"
DEFAULT_SAMPLE_SECONDS = 1.0
DEFAULT_TARGET_TEMP_C = 36.0
DEFAULT_TARGET_GUARD_RELEASE_C = 1.0
DEFAULT_VESSEL_SOFT_LIMIT_C = 36.5
DEFAULT_VESSEL_EMERGENCY_LIMIT_C = 38.0
DEFAULT_JACKET_SOFT_LIMIT_C = 45.0
DEFAULT_JACKET_GUARD_RELEASE_C = 3.0
DEFAULT_MAX_JACKET_VESSEL_GAP_C = 8.0
MIN_VALID_TEMP_C = 0.0
MAX_VALID_TEMP_C = 60.0
MAX_HEATER_PWM = 80

THERMAL_EXCITATION_BLOCKS = [
    ("heat_pwm_30", 30, 0),
    ("heat_pwm_55", 55, 0),
    ("coast_fan_off", 0, 0),
    ("heat_pwm_40", 40, 0),
    ("heat_pwm_70", 70, 0),
    ("fan_cool_pulse", 0, 1),
    ("heat_pwm_20", 20, 0),
    ("heat_pwm_60", 60, 0),
    ("coast_fan_off", 0, 0),
    ("heat_pwm_45", 45, 0),
    ("heat_pwm_65", 65, 0),
    ("fan_cool_pulse", 0, 1),
    ("heat_pwm_10", 10, 0),
    ("heat_pwm_50", 50, 0),
    ("heat_pwm_35", 35, 0),
    ("coast_fan_off", 0, 0),
    ("heat_pwm_70", 70, 0),
    ("heat_pwm_25", 25, 0),
    ("fan_cool_pulse", 0, 1),
    ("heat_pwm_55", 55, 0),
    ("heat_pwm_15", 15, 0),
    ("heat_pwm_60", 60, 0),
    ("coast_fan_off", 0, 0),
    ("heat_pwm_40", 40, 0),
    ("fan_cool_pulse", 0, 1),
    ("heat_pwm_30", 30, 0),
    ("heat_pwm_65", 65, 0),
    ("heat_pwm_50", 50, 0),
    ("coast_fan_off", 0, 0),
    ("heat_pwm_70", 70, 0),
    ("fan_cool_pulse", 0, 1),
    ("heat_pwm_20", 20, 0),
    ("heat_pwm_45", 45, 0),
    ("heat_pwm_60", 60, 0),
    ("fan_cool_pulse", 0, 1),
]

VESSEL_ONLY_PWM_30_80_BLOCKS = [
    ("heat_pwm_30", 30, 0),
    ("heat_pwm_40", 40, 0),
    ("heat_pwm_50", 50, 0),
    ("coast_fan_off", 0, 0),
    ("heat_pwm_60", 60, 0),
    ("fan_cool_pulse", 0, 1),
    ("heat_pwm_40", 40, 0),
    ("heat_pwm_70", 70, 0),
    ("coast_fan_off", 0, 0),
    ("heat_pwm_50", 50, 0),
    ("heat_pwm_80", 80, 0),
    ("fan_cool_pulse", 0, 1),
    ("heat_pwm_30", 30, 0),
    ("heat_pwm_60", 60, 0),
    ("coast_fan_off", 0, 0),
    ("heat_pwm_70", 70, 0),
    ("heat_pwm_40", 40, 0),
    ("fan_cool_pulse", 0, 1),
    ("heat_pwm_50", 50, 0),
    ("heat_pwm_80", 80, 0),
    ("coast_fan_off", 0, 0),
    ("heat_pwm_30", 30, 0),
    ("heat_pwm_60", 60, 0),
    ("fan_cool_pulse", 0, 1),
    ("heat_pwm_70", 70, 0),
    ("heat_pwm_50", 50, 0),
    ("coast_fan_off", 0, 0),
    ("heat_pwm_80", 80, 0),
    ("heat_pwm_40", 40, 0),
    ("fan_cool_pulse", 0, 1),
]

VESSEL_ONLY_NEAR_TARGET_BLOCKS = [
    ("near_target_heat_pwm_30", 30, 0),
    ("near_target_coast_fan_off", 0, 0),
    ("near_target_heat_pwm_40", 40, 0),
    ("near_target_coast_fan_off", 0, 0),
    ("near_target_heat_pwm_50", 50, 0),
    ("near_target_fan_cool_pulse", 0, 1),
    ("near_target_heat_pwm_30", 30, 0),
    ("near_target_heat_pwm_40", 40, 0),
    ("near_target_coast_fan_off", 0, 0),
    ("near_target_fan_cool_pulse", 0, 1),
]


class ArduinoProtocolError(RuntimeError):
    pass


class SafetyAbort(RuntimeError):
    pass


@dataclass(frozen=True)
class Phase:
    name: str
    rows: int
    requested_pwm: int
    fan_state: int
    stage_index: int


COLLECTION_PROFILES = {
    "aggressive-mixed": [
        Phase("baseline_water", 300, 0, 0, 1),
        *[
            Phase(name, 60, pwm, fan_state, index)
            for index, (name, pwm, fan_state) in enumerate(THERMAL_EXCITATION_BLOCKS, start=2)
        ],
        Phase("fan_cooling_water", 420, 0, 1, len(THERMAL_EXCITATION_BLOCKS) + 2),
        Phase("fan_off_settle_water", 180, 0, 0, len(THERMAL_EXCITATION_BLOCKS) + 3),
    ],
    "lower-aggression": [
        Phase("baseline_water", 300, 0, 0, 1),
        Phase("heat_pwm_10", 180, 10, 0, 2),
        Phase("coast_fan_off", 120, 0, 0, 3),
        Phase("heat_pwm_20", 180, 20, 0, 4),
        Phase("fan_cool_pulse", 180, 0, 1, 5),
        Phase("heat_pwm_30", 180, 30, 0, 6),
        Phase("coast_fan_off", 180, 0, 0, 7),
        Phase("heat_pwm_40", 120, 40, 0, 8),
        Phase("fan_cooling_water", 300, 0, 1, 9),
        Phase("fan_off_settle_water", 300, 0, 0, 10),
    ],
    "vessel-only-pwm-30-80": [
        Phase("baseline_water", 300, 0, 0, 1),
        *[
            Phase(name, 60, pwm, fan_state, index)
            for index, (name, pwm, fan_state) in enumerate(
                VESSEL_ONLY_PWM_30_80_BLOCKS, start=2
            )
        ],
        *[
            Phase(name, 60, pwm, fan_state, index)
            for index, (name, pwm, fan_state) in enumerate(
                VESSEL_ONLY_NEAR_TARGET_BLOCKS,
                start=len(VESSEL_ONLY_PWM_30_80_BLOCKS) + 2,
            )
        ],
        Phase(
            "final_fan_cooling_water",
            180,
            0,
            1,
            len(VESSEL_ONLY_PWM_30_80_BLOCKS) + len(VESSEL_ONLY_NEAR_TARGET_BLOCKS) + 2,
        ),
        Phase(
            "final_fan_off_settle_water",
            120,
            0,
            0,
            len(VESSEL_ONLY_PWM_30_80_BLOCKS) + len(VESSEL_ONLY_NEAR_TARGET_BLOCKS) + 3,
        ),
    ],
}


@dataclass
class OutputState:
    requested_pwm: int = -1
    heater_pwm: int = -1
    fan_state: int = -1
    heater_reply: str = ""
    fan_reply: str = ""
    phase_name: str = ""
    stage_index: int = 0
    pwm_step_start_elapsed_s: float = 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Collect a water-loaded temperature dynamics dataset using "
            "selectable heater, fan, and coast excitation profiles."
        )
    )
    parser.add_argument("--port", default=DEFAULT_PORT, help="Arduino serial port")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="Serial baud rate")
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Directory where the CSV log will be written.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Optional run ID. Defaults to water_dynamics_20_36_r1_YYYYmmdd_HHMMSS.",
    )
    parser.add_argument(
        "--sample-seconds",
        type=float,
        default=DEFAULT_SAMPLE_SECONDS,
        help="Logging interval in seconds. The planned dataset assumes 1.0.",
    )
    parser.add_argument(
        "--target-temp-c",
        type=float,
        default=DEFAULT_TARGET_TEMP_C,
        help="Upper target for this dynamics run. Heating is guarded at this value.",
    )
    parser.add_argument(
        "--profile",
        choices=sorted(COLLECTION_PROFILES),
        default="aggressive-mixed",
        help=(
            "Collection profile to run. lower-aggression avoids early high PWM "
            "blocks. vessel-only-pwm-30-80 ignores jacket temperature for guard "
            "logic and collects a 3000-row 30..80% PWM dataset."
        ),
    )
    parser.add_argument(
        "--target-guard-release-c",
        type=float,
        default=DEFAULT_TARGET_GUARD_RELEASE_C,
        help=(
            "When target guard cooling starts, release it after vessel temperature "
            "falls this many degrees below --target-temp-c."
        ),
    )
    parser.add_argument(
        "--vessel-soft-limit-c",
        type=float,
        default=DEFAULT_VESSEL_SOFT_LIMIT_C,
        help="Enter safety cooling if vessel temperature reaches this value.",
    )
    parser.add_argument(
        "--vessel-emergency-limit-c",
        type=float,
        default=DEFAULT_VESSEL_EMERGENCY_LIMIT_C,
        help="Abort the run if vessel temperature reaches this value.",
    )
    parser.add_argument(
        "--jacket-soft-limit-c",
        type=float,
        default=DEFAULT_JACKET_SOFT_LIMIT_C,
        help=(
            "Enter recoverable jacket guard cooling if jacket temperature reaches "
            "this value. Vessel limits still use safety cooling."
        ),
    )
    parser.add_argument(
        "--jacket-guard-release-c",
        type=float,
        default=DEFAULT_JACKET_GUARD_RELEASE_C,
        help=(
            "When jacket guard cooling starts, release it after jacket temperature "
            "falls this many degrees below --jacket-soft-limit-c."
        ),
    )
    parser.add_argument(
        "--max-jacket-vessel-gap-c",
        type=float,
        default=DEFAULT_MAX_JACKET_VESSEL_GAP_C,
        help="Log a jacket-vessel gap warning above this value; this is not a safety latch.",
    )
    parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Print the planned phases and exit without opening serial.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.sample_seconds <= 0:
        raise ValueError("--sample-seconds must be positive")
    if args.vessel_soft_limit_c >= args.vessel_emergency_limit_c:
        raise ValueError("--vessel-soft-limit-c must be below --vessel-emergency-limit-c")
    if args.jacket_soft_limit_c <= 0:
        raise ValueError("--jacket-soft-limit-c must be positive")
    if args.jacket_guard_release_c <= 0:
        raise ValueError("--jacket-guard-release-c must be positive")
    if args.target_temp_c < 20.0 or args.target_temp_c > args.vessel_soft_limit_c:
        raise ValueError("--target-temp-c must be between 20 C and the vessel soft limit")
    if args.target_guard_release_c <= 0:
        raise ValueError("--target-guard-release-c must be positive")
    phases = build_phase_plan(args.profile)
    for phase in phases:
        if phase.requested_pwm < 0 or phase.requested_pwm > MAX_HEATER_PWM:
            raise ValueError(f"{phase.name} PWM is outside 0..{MAX_HEATER_PWM}")
        if phase.fan_state not in (0, 1):
            raise ValueError(f"{phase.name} fan_state must be 0 or 1")
        if phase.rows <= 0:
            raise ValueError(f"{phase.name} rows must be positive")


def build_phase_plan(profile: str) -> list[Phase]:
    return list(COLLECTION_PROFILES[profile])


def uses_jacket_feedback(profile: str) -> bool:
    return profile != "vessel-only-pwm-30-80"


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


def set_outputs(
    ser: serial.Serial,
    state: OutputState,
    *,
    requested_pwm: int,
    actual_pwm: int,
    fan_state: int,
    phase_name: str,
    stage_index: int,
    elapsed_s: float,
) -> None:
    if (
        state.heater_pwm == actual_pwm
        and state.fan_state == fan_state
        and state.phase_name == phase_name
    ):
        return

    state.requested_pwm = requested_pwm
    state.heater_pwm = actual_pwm
    state.fan_state = fan_state
    state.phase_name = phase_name
    state.stage_index = stage_index
    state.pwm_step_start_elapsed_s = elapsed_s

    state.heater_reply = set_heater_pwm(ser, actual_pwm)
    state.fan_reply = set_fan(ser, bool(fan_state))


def force_safety_cooling(ser: serial.Serial, state: OutputState, elapsed_s: float) -> None:
    state.requested_pwm = 0
    state.heater_pwm = 0
    state.fan_state = 1
    state.phase_name = "safety_cooling"
    state.stage_index = 999
    state.pwm_step_start_elapsed_s = elapsed_s
    state.heater_reply = set_heater_pwm(ser, 0)
    state.fan_reply = set_fan(ser, True)


def shutdown_outputs(ser: serial.Serial, *, fan_on: bool) -> None:
    for command in ("a00", "d" if fan_on else "k"):
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


def open_writer(output_dir: Path, run_id: str):
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{run_id}.csv"
    file_obj = output_path.open("w", newline="", encoding="utf-8")
    fieldnames = [
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
    writer = csv.DictWriter(file_obj, fieldnames=fieldnames)
    writer.writeheader()
    return output_path, file_obj, writer


def validate_temperature(name: str, value: float) -> None:
    if value < MIN_VALID_TEMP_C or value > MAX_VALID_TEMP_C:
        raise SafetyAbort(f"{name} temperature {value:.2f} C is outside valid range")


def safety_reason(args: argparse.Namespace, vessel_temp_c: float, jacket_temp_c: float) -> str:
    if vessel_temp_c >= args.vessel_emergency_limit_c:
        return "emergency_vessel_limit"
    if vessel_temp_c >= args.vessel_soft_limit_c:
        return "vessel_soft_limit"
    return ""


def planned_phase_for_row(phases: list[Phase], row_index: int) -> Phase:
    offset = 0
    for phase in phases:
        offset += phase.rows
        if row_index < offset:
            return phase
    raise IndexError(f"row index {row_index} outside planned phase range")


def write_row(
    writer: csv.DictWriter,
    *,
    run_id: str,
    args: argparse.Namespace,
    state: OutputState,
    elapsed_s: float,
    vessel_temp_c: float,
    jacket_temp_c: float,
    ambient_temp_c: float,
    safety_limited: bool,
    reason: str,
) -> None:
    writer.writerow(
        {
            "timestamp_iso": datetime.now().isoformat(timespec="seconds"),
            "elapsed_s": f"{elapsed_s:.2f}",
            "run_id": run_id,
            "controller_type": "open_loop_data",
            "phase": state.phase_name,
            "stage_index": state.stage_index,
            "target_temp_c": f"{args.target_temp_c:.2f}",
            "vessel_temp_c": f"{vessel_temp_c:.2f}",
            "jacket_temp_c": f"{jacket_temp_c:.2f}",
            "jacket_minus_vessel_c": f"{jacket_temp_c - vessel_temp_c:.2f}",
            "heater_pwm_percent": state.heater_pwm,
            "requested_heater_pwm_percent": state.requested_pwm,
            "fan_state": state.fan_state,
            "jacket_vessel_gap_warning": int(
                jacket_temp_c - vessel_temp_c > args.max_jacket_vessel_gap_c
            ),
            "safety_limited": int(safety_limited),
            "safety_reason": reason,
            "ambient_temp_c": f"{ambient_temp_c:.2f}",
            "pwm_step_start_elapsed_s": f"{state.pwm_step_start_elapsed_s:.2f}",
            "seconds_since_pwm_change": f"{elapsed_s - state.pwm_step_start_elapsed_s:.2f}",
            "arduino_heater_reply": state.heater_reply,
            "arduino_fan_reply": state.fan_reply,
        }
    )


def print_plan(phases: list[Phase], sample_seconds: float) -> None:
    total_rows = sum(phase.rows for phase in phases)
    print(f"Planned rows: {total_rows}")
    print(f"Planned duration: {total_rows * sample_seconds:.0f} seconds")
    for phase in phases:
        print(
            f"{phase.stage_index:02d} {phase.name:22} rows={phase.rows:4d} "
            f"pwm={phase.requested_pwm:02d}% fan={phase.fan_state}"
        )


def run_collection(args: argparse.Namespace) -> Path:
    phases = build_phase_plan(args.profile)
    total_rows = sum(phase.rows for phase in phases)
    run_id = args.run_id or datetime.now().strftime("water_dynamics_20_36_r1_%Y%m%d_%H%M%S")
    output_path, file_obj, writer = open_writer(Path(args.output_dir), run_id)

    completed = False
    safety_limited = False
    reason = ""
    target_guard_active = False
    jacket_guard_active = False
    ambient_temp_c: float | None = None
    state = OutputState()
    start_monotonic = time.monotonic()

    with file_obj:
        with serial.Serial(args.port, args.baud, timeout=2) as ser:
            startup_lines = drain_startup_lines(ser)
            if startup_lines:
                print("Startup:", " | ".join(startup_lines))

            identity = send_command(ser, "i")
            print(f"Connected to Arduino device: {identity}")
            print(f"Run ID: {run_id}")
            print(f"Writing CSV: {output_path}")

            try:
                next_sample = time.monotonic()
                for row_index in range(total_rows):
                    now = time.monotonic()
                    if now < next_sample:
                        time.sleep(max(0.0, next_sample - now))

                    elapsed_s = time.monotonic() - start_monotonic
                    planned_phase = planned_phase_for_row(phases, row_index)
                    if not safety_limited:
                        requested_pwm = planned_phase.requested_pwm
                        actual_pwm = planned_phase.requested_pwm
                        fan_state = planned_phase.fan_state
                        phase_name = planned_phase.name
                        stage_index = planned_phase.stage_index
                        if jacket_guard_active:
                            actual_pwm = 0
                            fan_state = 1
                            phase_name = "jacket_guard_cooling"
                            stage_index = 997
                        elif target_guard_active:
                            actual_pwm = 0
                            fan_state = 1
                            phase_name = "target_guard_cooling"
                            stage_index = 998
                        set_outputs(
                            ser,
                            state,
                            requested_pwm=requested_pwm,
                            actual_pwm=actual_pwm,
                            fan_state=fan_state,
                            phase_name=phase_name,
                            stage_index=stage_index,
                            elapsed_s=elapsed_s,
                        )

                    vessel_temp_c = read_temperature(ser, "t")
                    jacket_temp_c = read_temperature(ser, "j")
                    validate_temperature("vessel", vessel_temp_c)
                    if uses_jacket_feedback(args.profile):
                        validate_temperature("jacket", jacket_temp_c)

                    if ambient_temp_c is None:
                        ambient_temp_c = vessel_temp_c

                    detected_reason = safety_reason(args, vessel_temp_c, jacket_temp_c)
                    if detected_reason == "emergency_vessel_limit":
                        force_safety_cooling(ser, state, elapsed_s)
                        write_row(
                            writer,
                            run_id=run_id,
                            args=args,
                            state=state,
                            elapsed_s=elapsed_s,
                            vessel_temp_c=vessel_temp_c,
                            jacket_temp_c=jacket_temp_c,
                            ambient_temp_c=ambient_temp_c,
                            safety_limited=True,
                            reason=detected_reason,
                        )
                        file_obj.flush()
                        raise SafetyAbort(
                            f"emergency vessel limit reached: {vessel_temp_c:.2f} C"
                        )

                    if detected_reason and not safety_limited:
                        safety_limited = True
                        reason = detected_reason
                        force_safety_cooling(ser, state, elapsed_s)
                        print(
                            "Safety cooling started. "
                            f"reason={reason}, vessel={vessel_temp_c:.2f} C, "
                            f"jacket={jacket_temp_c:.2f} C"
                        )

                    if not safety_limited and uses_jacket_feedback(args.profile):
                        if jacket_temp_c >= args.jacket_soft_limit_c:
                            jacket_guard_active = True
                            set_outputs(
                                ser,
                                state,
                                requested_pwm=planned_phase.requested_pwm,
                                actual_pwm=0,
                                fan_state=1,
                                phase_name="jacket_guard_cooling",
                                stage_index=997,
                                elapsed_s=elapsed_s,
                            )
                        elif (
                            jacket_guard_active
                            and jacket_temp_c
                            <= args.jacket_soft_limit_c - args.jacket_guard_release_c
                        ):
                            jacket_guard_active = False

                    if not safety_limited:
                        if not jacket_guard_active and vessel_temp_c >= args.target_temp_c:
                            target_guard_active = True
                            set_outputs(
                                ser,
                                state,
                                requested_pwm=planned_phase.requested_pwm,
                                actual_pwm=0,
                                fan_state=1,
                                phase_name="target_guard_cooling",
                                stage_index=998,
                                elapsed_s=elapsed_s,
                            )
                        elif (
                            target_guard_active
                            and vessel_temp_c <= args.target_temp_c - args.target_guard_release_c
                        ):
                            target_guard_active = False

                    write_row(
                        writer,
                        run_id=run_id,
                        args=args,
                        state=state,
                        elapsed_s=elapsed_s,
                        vessel_temp_c=vessel_temp_c,
                        jacket_temp_c=jacket_temp_c,
                        ambient_temp_c=ambient_temp_c,
                        safety_limited=safety_limited,
                        reason=reason,
                    )
                    file_obj.flush()

                    print(
                        f"{row_index + 1:4d}/{total_rows} {elapsed_s:7.1f}s "
                        f"{state.phase_name:22} T={vessel_temp_c:5.2f} C "
                        f"J={jacket_temp_c:5.2f} C PWM={state.heater_pwm:02d}% "
                        f"fan={state.fan_state} safety={int(safety_limited)} "
                        f"jacket_guard={int(jacket_guard_active)}"
                    )
                    next_sample += args.sample_seconds

                completed = True
            finally:
                shutdown_outputs(ser, fan_on=not completed)

    return output_path


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        phases = build_phase_plan(args.profile)
        if args.plan_only:
            print_plan(phases, args.sample_seconds)
            return 0
        output_path = run_collection(args)
    except KeyboardInterrupt:
        print("Interrupted by user. Heater off and fan-on shutdown attempted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI should show concise failure
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Water data collection complete. Data written to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
