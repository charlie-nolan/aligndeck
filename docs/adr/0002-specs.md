# ADR-0002: Reading alignment specs from manual pages (aligndeck/align_specs.py)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `aligndeck/align_specs.py`

- A line naming one of these ends the camber/toe/caster context it was in.
- A cell that reads like an instruction or a rate, not a spec value.
- Beyond this a reading is not plausible.
- `_clean`: '(0.08±0.12 in)' repeats the mm value.
- `parse_specs`: Tolerance given before the value: 'Camber Tolerance : +/- 0 deg 30''
- `parse_specs`: 'Each Toe Angle : +/- 0 deg 09'' : a per-wheel angle whose center the line leaves out.
- `parse_specs`: Toe in mm is measured across both tires unless the line says each/per wheel before the value.
- `parse_specs`: A bare '± 15'' is a tolerance with no value of its own.
- `parse_specs`: A value with no tolerance is not a spec this screen can check.
- `parse_rates`: CHARM breaks lines inside sentences.
- `lookup`: CHARM stores the same page under several paths.
