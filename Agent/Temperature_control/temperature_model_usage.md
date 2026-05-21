# Temperature Prediction And Control Model

## Generated Model

The current model artifact is:

```text
/home/pi-epbr/projects/Agent/Temperature_control/models/temperature_dynamics_model_latest.json
```

It was trained from the latest raw collection file:

```text
/home/pi-epbr/projects/Agent/Temperature_control/data/water_dynamics_20_36_r2_recoverable_jacket_20260520.csv
```

The model predicts vessel and jacket temperature deltas at:

```text
15 seconds
30 seconds
60 seconds
```

Hard safety limits are not learned by the model. They remain explicit controller
rules.

## Retrain

Train from the latest raw CSV in `data/`:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/train_temperature_model.py
```

Train from all raw CSVs in `data/`:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/train_temperature_model.py --all-raw
```

Train from a specific file:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/train_temperature_model.py /path/to/run.csv
```

## Recommend One Action

Example:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/temperature_model_controller.py \
  --vessel 28.0 \
  --jacket 28.0 \
  --setpoint 34.0 \
  --ambient 26.7
```

Output is JSON with:

```text
heater_pwm_percent
fan_state
predicted vessel/jacket temperatures
reason
```

## Current Limitations

This is the first usable model, not a final controller.

- The training set is one main 3000-row run, so use conservative supervision
  before connecting closed-loop control to hardware.
- The run contains many target/jacket guard rows, which is useful for safety but
  biases the model toward cautious heating.
- More data is still needed around near-setpoint holding and controlled cooling.
- The controller intentionally keeps hard limits outside the model:
  vessel soft limit `36.5 C`, jacket soft limit `45.0 C`, heater max `70%`.

## Real-Time Server UI

Start the browser control server in simulation mode:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/realtime_temperature_control_server.py --simulate --port 8080
```

Start it against the Arduino on `/dev/ttyACM0`:

```bash
python3 /home/pi-epbr/projects/Agent/Temperature_control/realtime_temperature_control_server.py --port 8080
```

Then open:

```text
http://<raspberry-pi-ip>:8080/
```

The page starts in idle monitoring mode. Heater commands are sent only after a
setpoint is entered and `Start` is pressed. `Stop` commands heater `0%` and fan
OFF.

Control behavior:

- While the vessel is more than `2%` of the setpoint below target, the
  controller uses explicit fast heating based on vessel temperature error.
- During fast heating, jacket temperature and jacket-vessel gap are ignored for
  normal modulation.
- Jacket soft-limit cooling is ignored until the vessel is inside the `2%`
  model handoff band. Vessel emergency and invalid-sensor protections remain
  active.
- Once the vessel is within `2%` of the setpoint, control is handed to the
  saved predictive model. For a `30 C` setpoint, model handoff starts at `29.4 C`.
- If vessel temperature is at or above setpoint, heater output is blocked.
- The fan is gated with hysteresis: it is used for safety, hot jacket, or larger
  overshoot, not for small near-setpoint corrections.

Each run writes a CSV log under:

```text
/home/pi-epbr/projects/Agent/Temperature_control/control_runs/
```

The run log includes measured vessel/jacket temperatures, selected heater/fan
commands, model predictions, delayed prediction errors, response time, and
overshoot metrics.
