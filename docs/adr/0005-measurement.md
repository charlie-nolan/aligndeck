# ADR-0005: Measurement, camera and full-compute decisions (aligndeck/, tests/)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `aligndeck/align_vision.py`

- As designed; measure the print and enter it if distances matter.
- `_dict`: Front view: the car must roll at least this much of a wheel turn between photos.
- `_dict`: Fewer corners than this: the pose is not trusted.

### `aligndeck/focus.py`

- Programs known to eat CPU/GPU/NPU on desktops and RK3588 boards: ticked by default when they show up.
- `scan`: Browsers are never pre-ticked: one of them may be showing this screen.
- `Focus._load`: Left over from a crash: give those programs back first.

### `aligndeck/session.py`

- `Session.measure_side`: Other half of a flip pair: the average cancels the clamp's tilt.
- `Session._calib_step`: Too small to trust: turn the same amount again, same direction.

### `tests/test_alignment.py`

- `render`: Anti-alias: blur the sheet by about half its shrink factor (stands in for the lens and sensor)
- `MathTests.test_floor_not_level_and_fwd_not_level.rot`: The whole car and floor rolled 1 deg and pitched 0.5 deg: camber and toe must not change.
