# Temperature Control Progress And Next Steps

## Current Progress

The Arduino sketch was fixed, compiled, uploaded, and verified over USB serial.

The Arduino is communicating with the Raspberry Pi over:

```text
/dev/ttyACM0
baud 115200
```

Safe read-only serial commands were verified:

```text
i -> EPBR01
t -> vessel temperature
j -> jacket temperature
```

## Arduino Code Changes Completed

The current Arduino sketch is:

```text
/home/pi-epbr/projects/arduino_code.ino
```

Completed fixes:

- Moved air relay control from Arduino `D1` to `D2` to avoid USB Serial TX conflict.
- Added `v` as an immediate serial command.
- Fixed round LED state tracking in `setRLedPercent()`.
- Initialized round LED PWM to `0` during setup.
- Corrected comments for LED and round LED pin assignments.
- Removed stale function declarations.

Later hardware note:

- The air relay wire should be moved from Arduino `D1` to `D2`.
- Pi serial permissions may need to be fixed later by adding the user to `dialout`.

These are noted but not the current focus.

## Data Collection Plan Created

The AI/ML temperature-control data collection plan is saved at:

```text
/home/pi-epbr/projects/Agent/Temperature_control/ml_temperature_control_data_plan.md
```

The plan defines:

- Sensor logging format
- Heater PWM range
- Fan ON/OFF chilling tests
- Safety limits
- Heating step tests
- Cooling tests
- Mixed setpoint tests
- Proposed ML model
- Future model predictive control approach

## Smoke Test Program Created

The smoke test script is saved at:

```text
/home/pi-epbr/projects/Agent/Temperature_control/smoke_temperature_test.py
```

Default test profile:

```text
0-2 min    heater 20%, fan OFF
2-4 min    heater 40%, fan OFF
4-6 min    heater 60%, fan OFF
6-10 min   heater 0%, fan ON
```

Safety rules in the script:

```text
heater PWM <= 70%
heater OFF if vessel temperature >= 36.5 C
heater OFF if jacket temperature >= 45.0 C
safe shutdown on exit or interruption
```

## Smoke Test Run Completed

The first 10-minute smoke test was completed successfully.

CSV output:

```text
/home/pi-epbr/projects/Agent/Temperature_control/data/smoke_20260519_154953.csv
```

Run summary:

```text
Rows: 600
Start vessel temperature: 24.50 C
Start jacket temperature: 23.30 C
Final vessel temperature: 36.50 C
Final jacket temperature: 36.10 C
Maximum vessel temperature: 36.50 C
Maximum jacket temperature: 46.80 C
```

Safety cutoff:

```text
First triggered at 324.05 s
Phase: heat_pwm_60
Vessel temperature: 27.50 C
Jacket temperature: 45.00 C
Action: heater forced to 0%
```

Phase summary:

```text
20% heat, fan OFF:
  vessel 24.50 -> 24.60 C
  jacket 23.30 -> 28.40 C

40% heat, fan OFF:
  vessel 24.60 -> 25.60 C
  jacket 28.50 -> 37.80 C

60% heat, fan OFF:
  vessel 25.60 -> 28.80 C
  jacket 37.90 -> 46.70 C

Fan ON chilling:
  vessel 28.80 -> 36.50 C
  jacket 46.70 -> 36.10 C
```

## Important Observation

The jacket temperature rose much faster than the vessel temperature.

During the `60%` heater stage, the jacket reached the 45.0 C safety threshold while the vessel was only about 27.5 C.

After heater cutoff, the vessel temperature continued rising during the fan stage because heat continued transferring from the hot jacket into the vessel.

This means the controller must consider jacket temperature and thermal lag, not only vessel temperature.

## Immediate Next Steps

### 1. Improve Smoke Test Safety Behavior

Update the smoke test so that when the jacket safety limit is reached:

- heater PWM is forced to 0%
- fan is turned ON immediately
- the run enters a safety-cooling mode

Current behavior turns heater off but waits until the scheduled chilling phase to turn fan ON.

### 2. Add More Conservative Test Profiles

The next data collection runs should avoid heating the jacket too aggressively.

Recommended next profile:

```text
0-3 min    heater 10%, fan OFF
3-6 min    heater 20%, fan OFF
6-8 min    heater 30%, fan OFF
8-15 min   heater 0%, fan ON
```

This will provide safer low-PWM dynamics data.

### 3. Add Jacket-Based Control Logic

Future control logic should use both:

```text
vessel_temp_c
jacket_temp_c
```

Basic rule:

```text
If jacket temperature rises too far above vessel temperature, reduce heater PWM.
```

Example starting rule:

```text
if jacket_temp_c - vessel_temp_c > 8 C:
    reduce heater PWM or turn heater off
```

### 4. Collect Additional Datasets

Recommended next datasets:

- 10%, 20%, 30% heater step test
- 40% heater step test with earlier fan intervention
- Fan-only cooling test from warm jacket condition
- Natural cooling test with fan OFF

Each dataset should be saved as a separate CSV with a unique run ID.

### 5. Start Data Analysis

Create a small analysis script to calculate:

- vessel temperature rate in C/min
- jacket temperature rate in C/min
- thermal lag between jacket and vessel
- cooling rate with fan ON
- cooling rate with fan OFF
- time delay between heater PWM change and vessel response

These features will be useful for the first ML model.

## Proposed First ML Direction

The first model should predict future vessel temperature rather than directly control the heater.

Target:

```text
vessel_temp_c at t + 30 seconds
```

Inputs:

```text
current vessel temperature
current jacket temperature
heater PWM
fan state
temperature rate
jacket temperature rate
recent temperature history
```

Recommended first model:

```text
Gradient Boosted Trees
```

Candidate tools:

- Scikit-learn HistGradientBoostingRegressor
- XGBoost
- LightGBM

## Diary Update: 2026-05-20 Water Dynamics Collection

Work folder:

```text
/home/pi-epbr/projects/Agent/Temperature_control
```

### Goal

Check whether the water-loaded collection plan can produce useful thermal
dynamics data for ML learning, then run the first conservative water tests.

The main learning objective is still:

```text
Predict future vessel and jacket temperature from current temperature,
jacket-vessel lag, heater PWM, fan state, and recent history.
```

### Code State Before Runs

Main collection script:

```text
/home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py
```

The script was confirmed to support:

- 3000 planned rows at 1 Hz.
- Baseline water phase.
- Mixed heater PWM, fan, and coast phases.
- Target guard cooling at `34 C` for the conservative run.
- Vessel hard safety rules.
- Jacket-vessel gap logging.
- CSV output into `data/`.

The script compiled successfully before running.

### Run 1: Conservative Water Test With Original Jacket Safety Latch

Command used:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py --target-temp-c 34
```

CSV output:

```text
/home/pi-epbr/projects/Agent/Temperature_control/data/water_dynamics_20_36_r1_20260520_131404.csv
```

Summary:

```text
Rows collected: 1575
First timestamp: 2026-05-20T13:14:06
Last timestamp:  2026-05-20T13:40:20
First safety event: 800.21 s
Safety reason: jacket_soft_limit
Vessel at first safety event: 22.50 C
Jacket at first safety event: 45.00 C
Maximum vessel temperature: 26.5 C
Maximum jacket temperature: 45.2 C
Final vessel temperature: 26.50 C
Final jacket temperature: 27.10 C
```

Result:

- The plan collected useful baseline, heating, fan, and lag data.
- The jacket reached `45 C` while the vessel was still only about `22.5 C`.
- The original code latched permanently into `safety_cooling`.
- After the latch, the rest of the run would mostly be repeated cooling rows.
- The run was stopped after the system had cooled substantially.

Conclusion:

```text
The original plan is too aggressive if jacket_soft_limit permanently ends
mixed excitation. The jacket safety behavior prevents completing the planned
dynamics schedule.
```

### Code Change: Recoverable Jacket Guard

The user requested removing the jacket max temperature. Instead of removing
protection completely, the script was changed to make the jacket limit
recoverable.

New behavior:

```text
if jacket_temp_c >= jacket_soft_limit_c:
    heater_pwm_percent = 0
    fan_state = ON
    phase = jacket_guard_cooling

release jacket_guard_cooling after:
    jacket_temp_c <= jacket_soft_limit_c - jacket_guard_release_c
```

Default release margin:

```text
jacket_guard_release_c = 3.0 C
```

Therefore with the default jacket soft limit:

```text
enter jacket guard at 45.0 C
release jacket guard at 42.0 C
```

Important safety decision:

- Vessel soft and emergency limits still remain hard protection.
- Sensor validity checks still remain hard protection.
- Jacket overheating is no longer a permanent safety latch, but it still forces
  heater off and fan on until the jacket cools.

Modified script:

```text
/home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py
```

The modified script compiled successfully.

### Run 2: Recoverable Jacket Guard Water Test

Command used:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py --target-temp-c 34 --run-id water_dynamics_20_36_r2_recoverable_jacket_20260520
```

CSV output:

```text
/home/pi-epbr/projects/Agent/Temperature_control/data/water_dynamics_20_36_r2_recoverable_jacket_20260520.csv
```

Summary:

```text
Rows collected: 3000
First timestamp: 2026-05-20T13:44:32
Last timestamp:  2026-05-20T14:34:31
First elapsed: 2.21 s
Last elapsed: 3001.21 s
Hard safety rows: 0
```

Temperature summary:

```text
Minimum vessel temperature: 26.7 C
Maximum vessel temperature: 35.5 C
Minimum jacket temperature: 24.7 C
Maximum jacket temperature: 46.7 C
Final vessel temperature: 32.60 C
Final jacket temperature: 26.10 C
```

First jacket guard:

```text
Elapsed: 770.21 s
Vessel temperature: 28.30 C
Jacket temperature: 45.00 C
```

First target guard:

```text
Elapsed: 1620.21 s
Vessel temperature: 34.00 C
Jacket temperature: 41.90 C
```

Phase counts:

```text
baseline_water:          300
heat_pwm_30:              60
heat_pwm_55:             100
coast_fan_off:            60
heat_pwm_40:              60
heat_pwm_70:             114
fan_cool_pulse:           60
heat_pwm_20:              60
heat_pwm_60:              50
jacket_guard_cooling:    586
heat_pwm_45:              21
heat_pwm_65:              50
heat_pwm_50:              59
heat_pwm_35:              11
heat_pwm_25:               2
heat_pwm_15:              25
target_guard_cooling:   1255
fan_off_settle_water:    127
```

PWM values captured:

```text
0, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70
```

Fan states captured:

```text
0, 1
```

Result:

- The modified code completed the full 3000-row run.
- No hard safety event occurred.
- Recoverable `jacket_guard_cooling` worked.
- The run captured useful data for:
  - baseline drift
  - heater response
  - jacket-vessel lag
  - fan cooling
  - recoverable jacket guard behavior
  - target guard cooling
  - fan-off settling

### Main Observations

The water vessel responds much more slowly than the jacket.

The jacket can climb to `45 C` while the vessel is still below `29 C`. This is
important for model learning and future controller design.

The second run confirms that a useful controller cannot use only vessel
temperature. It must consider:

```text
vessel_temp_c
jacket_temp_c
jacket_temp_c - vessel_temp_c
heater_pwm_percent
fan_state
time since last output change
```

The current open-loop excitation plan is still too aggressive near the top
range. Even with recoverable jacket guard, the run spent many rows in:

```text
jacket_guard_cooling
target_guard_cooling
```

That data is useful, but the next collection runs should produce more balanced
low and medium PWM dynamics before hitting guards.

## Next Steps After 2026-05-20 Runs

### 1. Keep The Recoverable Jacket Guard

Keep the new recoverable jacket guard behavior in
`water_temperature_collection.py`.

Do not completely remove jacket protection. The system showed that the jacket
can heat much faster than the vessel, so removing protection would make the
vessel reading misleading.

### 2. Create A Data Validation And Feature Script

Create an analysis script, for example:

```text
/home/pi-epbr/projects/Agent/Temperature_control/analyze_temperature_dataset.py
```

It should calculate:

- Row count.
- Missing values.
- Sample interval statistics.
- Phase counts.
- PWM value counts.
- Fan state counts.
- Safety and guard event counts.
- Vessel and jacket min/max.
- Jacket-vessel gap min/max.
- Vessel rate in C/min.
- Jacket rate in C/min.
- Fan-on cooling rate.
- Fan-off cooling or rebound rate.

It should also create derived ML columns:

```text
vessel_rate_c_per_min
jacket_rate_c_per_min
previous_heater_pwm_percent
previous_fan_state
vessel_temp_lag_5s
vessel_temp_lag_15s
vessel_temp_lag_30s
jacket_temp_lag_5s
jacket_temp_lag_15s
jacket_temp_lag_30s
heater_pwm_percent_lag_5s
heater_pwm_percent_lag_15s
heater_pwm_percent_lag_30s
fan_state_lag_5s
fan_state_lag_15s
fan_state_lag_30s
vessel_temp_t_plus_15s
vessel_temp_t_plus_30s
vessel_temp_t_plus_60s
jacket_temp_t_plus_15s
jacket_temp_t_plus_30s
jacket_temp_t_plus_60s
```

Future temperature columns must only be used as labels, not input features.

### 3. Add A Lower-Aggression Collection Profile

Status on 2026-05-21: completed in
`/home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py`.

The data collector now supports:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py --plan-only --profile lower-aggression
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py --profile lower-aggression --target-temp-c 34
```

The original 3000-row mixed plan remains available as:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py --profile aggressive-mixed
```

The next collection profile should avoid early `55%`, `60%`, `65%`, and `70%`
heater blocks.

Recommended next profile:

```text
baseline:      300 s, heater 0%,  fan OFF
heat low:      180 s, heater 10%, fan OFF
coast:         120 s, heater 0%,  fan OFF
heat low-mid:  180 s, heater 20%, fan OFF
fan cool:      180 s, heater 0%,  fan ON
heat mid:      180 s, heater 30%, fan OFF
coast:         180 s, heater 0%,  fan OFF
heat mid-high: 120 s, heater 40%, fan OFF
fan cool:      300 s, heater 0%,  fan ON
fan off settle:300 s, heater 0%,  fan OFF
```

Purpose:

- Learn low and medium PWM dynamics cleanly.
- Reduce time spent in guard phases.
- Still capture fan cooling and jacket-vessel lag.

### 4. Collect Multiple Smaller Runs

Instead of one large aggressive mixed run, collect several focused datasets:

- Low PWM heating run: `10%, 20%, 30%`.
- Medium PWM heating run: `30%, 40%, 45%`.
- Fan-only cooling from warm jacket.
- Natural cooling with fan off.
- Near-setpoint fine-control run around `32 C` to `34 C`.

Each run should have a unique run ID and separate CSV.

### 5. Train First Predictive Model Only After Validation

Do not train a controller directly yet.

First train prediction models for:

```text
vessel_temp_c at t + 15 s
vessel_temp_c at t + 30 s
vessel_temp_c at t + 60 s
jacket_temp_c at t + 15 s
jacket_temp_c at t + 30 s
jacket_temp_c at t + 60 s
```

Recommended first model:

```text
scikit-learn HistGradientBoostingRegressor
```

The first controller should be model-predictive:

1. Read current vessel and jacket temperatures.
2. Generate candidate heater/fan actions.
3. Predict future vessel and jacket temperatures.
4. Reject unsafe or high-overshoot actions.
5. Choose the action that reaches the setpoint fastest without overshoot.

Hard safety and guard rules must remain outside the ML model.

## Diary Update: 2026-05-21 Code Maintenance

### Smoke Test Safety Cooling

Updated:

```text
/home/pi-epbr/projects/Agent/Temperature_control/smoke_temperature_test.py
```

Change:

- If vessel or jacket temperature reaches the smoke-test limit, the script now
  forces heater PWM to `0%`, turns the fan ON immediately, and labels following
  rows as `safety_cooling`.
- Added `safety_reason` to new smoke-test CSVs so vessel and jacket limit events
  are distinguishable.
- Once smoke-test safety cooling starts, later scheduled heater phases do not
  resume heating.

### Selectable Water Collection Profiles

Updated:

```text
/home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py
```

Change:

- Added `--profile aggressive-mixed`, preserving the existing 3000-row plan.
- Added `--profile lower-aggression`, a 2040-row plan intended for the next
  safer water dataset:

```text
baseline_water         300 rows, heater 0%,  fan OFF
heat_pwm_10            180 rows, heater 10%, fan OFF
coast_fan_off          120 rows, heater 0%,  fan OFF
heat_pwm_20            180 rows, heater 20%, fan OFF
fan_cool_pulse         180 rows, heater 0%,  fan ON
heat_pwm_30            180 rows, heater 30%, fan OFF
coast_fan_off          180 rows, heater 0%,  fan OFF
heat_pwm_40            120 rows, heater 40%, fan OFF
fan_cooling_water      300 rows, heater 0%,  fan ON
fan_off_settle_water   300 rows, heater 0%,  fan OFF
```

Validation performed:

```bash
python3 -m py_compile water_temperature_collection.py smoke_temperature_test.py
python3 water_temperature_collection.py --plan-only --profile lower-aggression
python3 water_temperature_collection.py --plan-only --profile aggressive-mixed
```

### Current Recommended Next Run

Use the lower-aggression profile before collecting more high-PWM data:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/water_temperature_collection.py \
  --profile lower-aggression \
  --target-temp-c 34 \
  --run-id water_dynamics_lower_aggression_20260521
```

## Controller Safety Requirements

The final controller must always enforce hard-coded safety rules outside the ML model:

```text
heater PWM <= 70%
heater OFF if vessel temperature >= 36.5 C
heater OFF if jacket temperature >= safety limit
fan ON if jacket temperature is too high
heater OFF if sensor reading is invalid
manual stop always available
```

The ML model should recommend actions, but it must not be the only safety layer.
