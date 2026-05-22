# Vessel-Only PWM 30-80 Data Collection Plan

## Goal

Collect a 3000-row water-loaded dataset using heater PWM values from `30%` to
`80%`, with only vessel temperature used for feedback and guard decisions.

The dataset should teach the model how vessel temperature responds to:

- heater PWM from `30%` to `80%`
- fan-assisted cooling
- fan-off coasting
- near-target holding and recovery

Jacket temperature may still be logged as passive telemetry, but it must not be
used for PWM decisions, guard cooling, or abort logic in this run.

## Assumptions

```text
sample interval: 1 second
total rows: 3000
total duration: 3000 seconds = 50 minutes
feedback signal: vessel_temp_c only
PWM values: 30, 40, 50, 60, 70, 80
```

## Vessel-Only Guard Rules

Use only vessel temperature for active decisions:

```text
if vessel_temp_c >= 36.0:
    heater_pwm_percent = 0
    fan_state = ON
    phase = target_guard_cooling

release target_guard_cooling after:
    vessel_temp_c <= 34.5
```

Hard vessel protection:

```text
if vessel_temp_c >= 36.5:
    heater_pwm_percent = 0
    fan_state = ON
    phase = safety_cooling

if vessel_temp_c >= 38.0:
    heater_pwm_percent = 0
    fan_state = ON
    abort run
```

Jacket temperature is ignored by guard logic for this plan.

## 3000-Row Schedule

### Phase 1: Baseline

```text
rows: 300
duration: 300 seconds
heater PWM: 0%
fan: OFF
phase: baseline_water
```

Purpose:

- measure starting vessel drift
- estimate initial ambient water temperature
- capture sensor noise before heating

### Phase 2: PWM Step Excitation

```text
rows: 1800
duration: 1800 seconds
block count: 30
block duration: 60 seconds
```

Block sequence:

```text
30, 40, 50, coast, 60, fan,
40, 70, coast, 50, 80, fan,
30, 60, coast, 70, 40, fan,
50, 80, coast, 30, 60, fan,
70, 50, coast, 80, 40, fan
```

Where:

```text
coast = heater 0%, fan OFF
fan   = heater 0%, fan ON
```

### Phase 3: Near-Target Holding And Recovery

```text
rows: 600
duration: 600 seconds
block count: 10
block duration: 60 seconds
```

Block sequence:

```text
30, coast, 40, coast, 50, fan, 30, 40, coast, fan
```

Purpose:

- capture lower PWM behavior when the vessel is already warm
- improve setpoint-holding data
- add recovery data after fan cooling

### Phase 4: Final Cooling And Settling

```text
rows: 300
duration: 300 seconds
heater PWM: 0%
fan ON: 180 seconds
fan OFF: 120 seconds
```

Purpose:

- capture fan-assisted cooling
- capture post-fan settling behavior

## Expected Row Counts

```text
baseline:        300
PWM excitation: 1800
near target:     600
final cooling:   300
total:          3000
```

## Recommended Command

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py \
  --profile vessel-only-pwm-30-80 \
  --target-temp-c 36 \
  --target-guard-release-c 1.5 \
  --run-id vessel_only_pwm_30_80_YYYYMMDD_HHMMSS
```

Preview the plan without opening serial:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py \
  --profile vessel-only-pwm-30-80 \
  --target-guard-release-c 1.5 \
  --plan-only
```

## CSV Notes

The existing CSV format can be reused. For this plan:

- `vessel_temp_c` is the only feedback signal.
- `target_guard_cooling` is based only on vessel temperature.
- `safety_cooling` is based only on vessel temperature.
- `jacket_temp_c` and `jacket_minus_vessel_c` are passive telemetry only.
- `jacket_vessel_gap_warning` is informational only.

## Run Attempt: 2026-05-22

Command:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py \
  --profile vessel-only-pwm-30-80 \
  --target-temp-c 36 \
  --target-guard-release-c 1.5 \
  --run-id vessel_only_pwm_30_80_20260522
```

Output:

```text
/home/pi-epbr/projects/Agent/Temperature_control/data/vessel_only_pwm_30_80_20260522.csv
```

Result:

```text
Rows collected: 2019
Requested rows: 3000
Status: aborted by vessel emergency limit
First target guard: vessel_temp_c = 36.0 C
Soft safety cooling: vessel_temp_c = 36.5 C
Emergency abort: vessel_temp_c = 38.0 C
Maximum jacket telemetry: 59.7 C
```

PWM coverage before abort:

```text
0, 30, 40, 50, 60, 70, 80
```

Conclusion:

The vessel-only plan successfully collected baseline, coast, fan cooling, and
all requested PWM levels from `30%` to `80%`, but the run did not complete 3000
rows. Stored heat caused the vessel to continue rising after target guard and
soft safety cooling had already turned the heater off and fan on.

For a successful full 3000-row vessel-only run, the target guard should start
earlier, for example:

```text
target guard enter: 34.5 C
target guard release: 33.0 C
```

This still uses only vessel temperature as feedback while giving more room for
thermal lag after heater shutdown.
