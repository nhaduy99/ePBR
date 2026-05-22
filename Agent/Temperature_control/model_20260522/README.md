# Model 20260522

This folder preserves the latest vessel-only PWM `30%` to `80%` dataset and
the model trained from it.

Files:

- `vessel_only_pwm_30_80_20260522.csv`: raw collection data.
- `vessel_only_pwm_30_80_20260522_features.csv`: feature-enriched training data.
- `temperature_dynamics_model_latest.json`: trained model artifact.

Training source:

```text
data/vessel_only_pwm_30_80_20260522.csv
```

Training summary:

```text
Training rows: 1447
Holdout rows: 482
15 s vessel MAE: 0.0884 C
30 s vessel MAE: 0.1412 C
60 s vessel MAE: 0.2630 C
```

The active UI model also remains at:

```text
models/temperature_dynamics_model_latest.json
```
