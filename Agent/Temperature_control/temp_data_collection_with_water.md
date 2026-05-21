# Temperature Data Collection With Water

## Goal

Collect water-loaded thermal dynamics data for the ePBR system so an ML model
can predict how vessel and jacket temperatures respond to heater PWM, fan
state, stored jacket heat, and cooling delay.

The trained model will be used by a controller that accepts a user setpoint in
the range:

```text
20 C to 36 C
```

The controller objective is:

```text
reach the requested setpoint as fast as possible without overshoot or unsafe jacket heating
```

This is a dynamics-learning problem first. The data must include heating,
coasting, fan-assisted cooling, and thermal-lag behavior. A simple monotonic
heater sweep is not enough.

## Required Setup

Before running:

1. Fill the vessel with the intended normal-operation water volume.
2. Confirm the vessel temperature sensor on Arduino `A0` is submerged or correctly coupled.
3. Confirm the jacket temperature sensor on Arduino `A1` is installed correctly.
4. Confirm the fan/chilling hardware can turn ON with serial command `d`.
5. Confirm heater PWM can be set with serial command `aXX`.
6. Confirm the Arduino is available at:

```text
/dev/ttyACM0
baud 115200
```

## Safety Rules

Keep hard safety outside the ML model:

```text
maximum heater PWM: 70%
vessel soft limit: 36.5 C
jacket soft limit: 45.0 C
emergency vessel limit: 38.0 C
valid sensor range: 0 C to 60 C
sample interval: 1 second
safe shutdown on exit or interruption
```

Safety behavior:

```text
if vessel_temp_c >= 36.5:
    heater_pwm_percent = 0
    fan_state = ON
    phase = safety_cooling

if jacket_temp_c >= 45.0:
    heater_pwm_percent = 0
    fan_state = ON
    phase = safety_cooling

if vessel_temp_c >= 38.0:
    heater_pwm_percent = 0
    fan_state = ON
    abort the run
```

The jacket-vessel gap is not a hard safety latch for this dynamics dataset.
It is logged as `jacket_vessel_gap_warning` when the gap exceeds `8 C`, because
that state is useful for learning jacket-to-vessel heat transfer. The hard
jacket safety limit remains `45 C`.

Important Arduino behavior:

```text
The Arduino aXX heater command currently turns the fan OFF internally.
Therefore fan-on states must send commands in this order:

1. aXX or a00
2. d
```

## Operating-Range Guard

The data is intended for setpoints from `20 C` to `36 C`. During collection,
the default target guard is:

```text
target_temp_c = 36.0 C
if vessel_temp_c >= target_temp_c:
    heater_pwm_percent = 0
    fan_state = ON
    phase = target_guard_cooling

release target guard after vessel_temp_c <= target_temp_c - 1.0 C
```

This is not marked as a safety event. It keeps the dataset inside the useful
training range while still collecting recovery and cooling behavior near the
upper operating limit.

## Dataset Size Target

Target rows:

```text
3000 rows at 1 Hz
3000 seconds = 50 minutes
```

## CSV Columns

Use these columns:

```csv
timestamp_iso,elapsed_s,run_id,controller_type,phase,stage_index,target_temp_c,vessel_temp_c,jacket_temp_c,jacket_minus_vessel_c,heater_pwm_percent,requested_heater_pwm_percent,fan_state,jacket_vessel_gap_warning,safety_limited,safety_reason,ambient_temp_c,pwm_step_start_elapsed_s,seconds_since_pwm_change,arduino_heater_reply,arduino_fan_reply
```

Derived columns for training should be added after collection:

```csv
vessel_rate_c_per_min,jacket_rate_c_per_min,previous_heater_pwm_percent,previous_fan_state,vessel_temp_t_plus_15s,vessel_temp_t_plus_30s,vessel_temp_t_plus_60s,jacket_temp_t_plus_15s,jacket_temp_t_plus_30s,jacket_temp_t_plus_60s,vessel_temp_lag_5s,vessel_temp_lag_15s,vessel_temp_lag_30s,jacket_temp_lag_5s,jacket_temp_lag_15s,jacket_temp_lag_30s
```

Future temperature columns are supervised-learning labels only. Do not use
future values as input features.

## Collection Script

Executable script:

```text
/home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py
```

Preview the run:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py --plan-only
```

Run collection:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py
```

Default run ID format:

```text
water_dynamics_20_36_r1_YYYYmmdd_HHMMSS
```

## Real Data Collection Schedule

This schedule produces exactly `3000` planned samples at `1 Hz`.

### Phase 1: Baseline

```text
duration: 300 seconds
rows: 300
heater PWM: 0%
fan: OFF
phase: baseline_water
```

Purpose:

- estimate ambient water temperature
- measure sensor noise
- confirm stable readings before heating

### Phase 2: Mixed Thermal Excitation

```text
duration: 2100 seconds
rows: 2100
block duration: 60 seconds
heater PWM values: 0%, 10%, 15%, 20%, 25%, 30%, 35%, 40%, 45%, 50%, 55%, 60%, 65%, 70%
fan states: OFF and ON
```

The plan deliberately mixes:

- high-power heat-up blocks for fastest-rise behavior
- low and medium PWM blocks for controllability near setpoint
- heater-off coast blocks for stored jacket heat transfer
- fan-on cooling pulses for recovery and overshoot data

Sequence:

```text
30, 55, coast, 40, 70, fan,
20, 60, coast, 45, 65, fan,
10, 50, 35, coast, 70, 25, fan,
55, 15, 60, coast, 40, fan,
30, 65, 50, coast, 70, fan,
20, 45, 60, fan
```

### Phase 3: Fan Cooling

```text
duration: 420 seconds
rows: 420
heater PWM: 0%
fan: ON
phase: fan_cooling_water
```

Purpose:

- learn forced-cooling rate
- capture delayed vessel response after jacket heating
- provide recovery data for overshoot correction

### Phase 4: Fan-Off Settling

```text
duration: 180 seconds
rows: 180
heater PWM: 0%
fan: OFF
phase: fan_off_settle_water
```

Purpose:

- observe rebound after fan cooling
- measure natural thermal drift

## Planned Row Count

```text
baseline_water:          300 rows
mixed thermal excitation: 2100 rows
fan_cooling_water:       420 rows
fan_off_settle_water:    180 rows
----------------------------------
total planned:          3000 rows
```

## Data Quality Requirements

Before training:

```text
no missing vessel_temp_c values
no missing jacket_temp_c values
no missing heater_pwm_percent values
no missing fan_state values
all heater_pwm_percent values are 0..70
all fan_state values are 0 or 1
elapsed_s is strictly increasing
sample spacing is close to 1 second
both fan OFF and fan ON rows are present
both heating and heater-off coast rows are present
at least 8 distinct heater PWM values are present
future label columns do not wrap or repeat at the end of the file
```

## First Model

Train a predictive dynamics model, not a direct black-box controller.

Primary labels:

```text
vessel_temp_c at t + 15 seconds
vessel_temp_c at t + 30 seconds
vessel_temp_c at t + 60 seconds
```

Secondary labels:

```text
jacket_temp_c at t + 15 seconds
jacket_temp_c at t + 30 seconds
jacket_temp_c at t + 60 seconds
```

Recommended first model:

```text
scikit-learn HistGradientBoostingRegressor
```

## First ML Controller

Use the predictive model inside a model-predictive control loop.

At each control decision:

1. Read current vessel and jacket temperature.
2. Accept the user setpoint, clamped to `20 C` to `36 C`.
3. Generate candidate actions:

```text
heater_pwm_percent = 0, 10, 20, 30, 40, 50, 60, 70
fan_state = OFF or ON
```

4. Reject unsafe actions using hard-coded safety rules.
5. Predict future vessel and jacket temperature for each candidate.
6. Choose the action with the fastest predicted setpoint approach and no overshoot.

Example score:

```text
score =
    time_to_setpoint_penalty
  + overshoot_penalty
  + jacket_soft_limit_penalty
  + jacket_vessel_gap_penalty
  + heater_usage_penalty
  + fan_usage_penalty
```

Hard overrides always remain active:

```text
if vessel_temp_c >= 36.5: heater = 0, fan = ON
if jacket_temp_c >= 45.0: heater = 0, fan = ON
if vessel_temp_c >= 38.0: heater = 0, fan = ON, abort
if sensor invalid: heater = 0, fan = ON
```

## Collection Profiles

The water collection script now has selectable plans:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py --plan-only --profile aggressive-mixed
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py --plan-only --profile lower-aggression
```

Use `lower-aggression` for the next balanced water run. It avoids early
`55%`, `60%`, `65%`, and `70%` heater blocks while still collecting low/medium
PWM, fan cooling, coast, and settle data.
