# AI/ML Temperature Control Data Collection Plan

## Goal

Collect time-series data from the ePBR temperature-control system so an ML
model can learn water-loaded thermal dynamics and support fast temperature
control for user setpoints from `20 C` to `36 C`.

The ML model should predict future vessel and jacket temperature. The controller
will use those predictions to choose heater PWM and fan state. Hard safety rules
must remain outside the ML model.

## Control Objective

Given a user setpoint:

```text
20 C <= setpoint <= 36 C
```

choose actions that:

```text
reach the setpoint as fast as possible
avoid overshoot
avoid unsafe jacket temperature
avoid excessive jacket-vessel thermal lag
```

## System Outputs

Controllable outputs:

```text
heater_pwm_percent: 0% to 70%
fan_state: OFF or ON
```

The heater PWM must never exceed `70%`.

## Sensors To Log

Minimum readings:

```text
vessel_temp_c from Arduino A0
jacket_temp_c from Arduino A1
```

If no ambient sensor exists, use the initial vessel temperature before heating
as the ambient estimate for the run.

## Data Logging Rate

Log one row every `1 second`.

Required raw columns:

```csv
timestamp_iso,elapsed_s,run_id,controller_type,phase,stage_index,target_temp_c,vessel_temp_c,jacket_temp_c,jacket_minus_vessel_c,heater_pwm_percent,requested_heater_pwm_percent,fan_state,jacket_vessel_gap_warning,safety_limited,safety_reason,ambient_temp_c,pwm_step_start_elapsed_s,seconds_since_pwm_change,arduino_heater_reply,arduino_fan_reply
```

Derived training columns:

```csv
vessel_rate_c_per_min,jacket_rate_c_per_min,previous_heater_pwm_percent,previous_fan_state,vessel_temp_lag_5s,vessel_temp_lag_15s,vessel_temp_lag_30s,jacket_temp_lag_5s,jacket_temp_lag_15s,jacket_temp_lag_30s,heater_pwm_percent_lag_5s,heater_pwm_percent_lag_15s,heater_pwm_percent_lag_30s,fan_state_lag_5s,fan_state_lag_15s,fan_state_lag_30s,vessel_temp_t_plus_15s,vessel_temp_t_plus_30s,vessel_temp_t_plus_60s,jacket_temp_t_plus_15s,jacket_temp_t_plus_30s,jacket_temp_t_plus_60s
```

Use only current and past values as model features. Use future shifted values
only as labels.

## Safety Limits

Collection software must enforce:

```text
heater PWM clamped to 0%..70%
heater OFF and fan ON if vessel_temp_c >= 36.5 C
heater OFF and fan ON if jacket_temp_c >= 45.0 C
abort if vessel_temp_c >= 38.0 C
heater OFF and fan ON if sensor data is invalid
heater OFF on manual stop
```

Valid sensor range:

```text
0 C <= vessel_temp_c <= 60 C
0 C <= jacket_temp_c <= 60 C
```

The jacket-vessel gap should be logged, not used as a permanent safety latch.
Large gaps are useful training data because they teach the model about stored
jacket heat and delayed vessel response.

## First Dynamics Run

The implemented first run is:

```text
/home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py
```

It collects:

```text
300 rows baseline
2100 rows mixed heater/fan/coast excitation
420 rows fan cooling
180 rows fan-off settling
3000 rows total
```

This run is designed to produce useful state-transition data, not to hold a
single setpoint.

## Experiment Types For Full Dataset

### 1. Mixed Dynamics Runs

Purpose: learn general response across the operating range.

Actions:

```text
heater PWM: 0, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70
fan state: OFF and ON
```

Collect multiple runs from different starting temperatures.

### 2. Fast Heat-Up Runs

Purpose: learn the fastest safe trajectory to setpoints.

Setpoints:

```text
24 C, 28 C, 32 C, 36 C
```

Procedure:

1. Start near ambient.
2. Use high PWM, usually `60%` to `70%`, while jacket remains safe.
3. Reduce or stop heat near the setpoint.
4. Record overshoot, settling, and jacket lag.

### 3. Near-Setpoint Fine Control Runs

Purpose: learn small corrections close to target.

Setpoints:

```text
24 C, 28 C, 32 C, 36 C
```

Actions:

```text
heater PWM: 0, 10, 20, 30, 40
fan state: OFF and ON
```

These runs teach the controller how to avoid overshoot after fast approach.

### 4. Cooling And Recovery Runs

Purpose: learn fan-assisted cooling and natural settling.

Procedure:

1. Heat vessel/jacket to a warm state below safety limits.
2. Set heater to `0%`.
3. Run fan ON and fan OFF recovery profiles.
4. Log until temperatures stabilize.

## Minimum Dataset Before Training

Recommended first training size:

```text
mixed dynamics runs: 3 to 5
fast heat-up runs: 4 setpoints x 2 repeats
near-setpoint runs: 4 setpoints x 2 repeats
cooling/recovery runs: 6
```

Expected size:

```text
50,000 to 100,000 rows
```

## First Model

Train predictive dynamics models:

```text
vessel_temp_c at t + 15 seconds
vessel_temp_c at t + 30 seconds
vessel_temp_c at t + 60 seconds
jacket_temp_c at t + 15 seconds
jacket_temp_c at t + 30 seconds
jacket_temp_c at t + 60 seconds
```

Recommended first implementation:

```text
scikit-learn HistGradientBoostingRegressor
```

Good inputs:

```text
current_vessel_temp_c
current_jacket_temp_c
jacket_minus_vessel_c
heater_pwm_percent
fan_state
ambient_temp_c
elapsed_s
vessel_rate_c_per_min
jacket_rate_c_per_min
previous_heater_pwm_percent
previous_fan_state
recent vessel/jacket/heater/fan lag values
```

## First Controller

Use model-predictive control rather than direct ML action output.

At each control step:

1. Read vessel and jacket temperatures.
2. Clamp user setpoint to `20 C`..`36 C`.
3. Generate candidate actions:

```text
heater_pwm_percent = 0, 10, 20, 30, 40, 50, 60, 70
fan_state = OFF or ON
```

4. Reject actions that violate hard safety rules.
5. Predict future vessel and jacket temperatures.
6. Pick the action with the best score.

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

Hard overrides:

```text
if vessel_temp_c >= 36.5: heater = 0, fan = ON
if jacket_temp_c >= 45.0: heater = 0, fan = ON
if vessel_temp_c >= 38.0: heater = 0, fan = ON, abort
if sensor reading is invalid: heater = 0, fan = ON
```

## Validation Before Training

Check:

```text
no missing temperatures
no missing actions
elapsed_s strictly increasing
sample spacing near 1 second
both fan states present
multiple PWM values present
heating, coasting, and cooling rows present
safety rows clearly labeled
future labels shifted correctly without wrapping
train/validation/test split by time or by whole run ID
```
