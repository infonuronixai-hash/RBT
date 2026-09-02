# Wiring and power

## Bill of materials

| Part | Notes |
|---|---|
| Arduino Uno | genuine or clone (clones need the CH340 driver) |
| 6 × servos | SG90 for a small arm, MG996R / DS3218 for anything with weight |
| 5–6 V supply | **2 A minimum**, 5 A if you use MG996R-class servos |
| Breadboard / shield | a servo shield or screw-terminal shield saves a lot of loose wires |
| 470–1000 µF capacitor | across the servo supply rails, near the servos |
| 6 × 10 kΩ potentiometers | optional, only for teach mode |

## Signal wiring

```
Arduino Uno                       Servos
    D3  ────────────────────────  base rotate   signal
    D5  ────────────────────────  shoulder      signal
    D6  ────────────────────────  elbow         signal
    D9  ────────────────────────  wrist pitch   signal
    D10 ────────────────────────  wrist roll    signal
    D11 ────────────────────────  gripper       signal
    GND ──────┬─────────────────  all servo grounds
              │
              └───────────────── external supply GND     <-- the common ground matters

External 5-6V (+) ──────┬──────  all servo V+
                        │
                     1000uF      (- leg to ground)
```

**Do not** power six servos from the Arduino's 5 V pin. The onboard regulator is good for
a few hundred milliamps; a single MG996R can pull well over an amp when it stalls. The
result is a board that resets mid-motion, or a regulator that cooks. Feed the servos from
their own supply and join only the grounds.

The Servo library takes over Timer1, so **pins 9 and 10 lose PWM** (`analogWrite`) while it
is running. That is fine here — they are used as servo outputs anyway.

## Optional teach pendant (A0–A5)

Each potentiometer is a plain voltage divider:

```
5V ──── pot outer leg 1
        pot wiper ──────── A0 .. A5
GND ─── pot outer leg 2
```

The firmware maps 0–1023 counts onto 0–180° and smooths the reading with an exponential
filter, so ADC noise doesn't show up as jitter in a recording. Six pots on a panel make a
teach pendant; pots geared to the joints themselves let you record by physically moving
the arm.

## First power-up checklist

1. Upload the firmware **before** connecting servo power, so nothing lurches on boot.
2. Support the arm by hand the first time — a servo whose horn is mounted at the wrong
   angle can slam into an end stop.
3. Connect from the app and jog **one joint at a time** by ±5°, checking direction.
4. Anything running backwards: tick **Invert** in Arm → Joint setup.
5. Walk each joint to its real mechanical limits, note the angles, and enter them as
   min/max in Joint setup. Then **Arm → Store settings in EEPROM**.
6. Only now load `sequences\demo_wave_hello.json` and press Play.

Keep the servo supply switch within reach while testing. Pulling USB does not stop the
servos — they keep holding the last commanded position.
