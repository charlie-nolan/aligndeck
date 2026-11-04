# CLAUDE.md: aligndeck

Camera wheel alignment: printed ChArUco targets, any camera (gphoto2, webcam or photo files), camber and toe,
specs, and exact adjuster turns. It is a standalone app with its own web GUI. It has **no connection to the Open
Diagnostic Cyberdeck**, apart from optionally reading a spec DB in that layout via `--kb`/`--kb-dir`.

## Rules (the owner's, keep them)

- **Accuracy first.** Never invent a spec, thread pitch, rate or turn direction. A value no source gives is typed by
  the user and shown as "typed by you". When the direction is unknown, say so and ask for a test turn.
- A manual rate is used only after the user picks it for that adjuster, because it can belong to the other axle.
  Turns come mainly from the guided calibration (photos, turn one adjuster a known amount, photos again).
- The **vision model is a helper only**: notes labelled "may be wrong", and a tire size that goes in only when the
  user presses USE IT. It must never measure or change a reading or spec.
- **Full compute pauses only the programs the user ticks** (SIGSTOP), never kills anything, never pre-ticks
  browsers, and always resumes on quit or after a crash.
- The app never autostarts and only listens on localhost and Tailscale. The UI must stay keyboard-driven.
- The repo is public, so never commit service-manual data, photos, calibration, DBs, models, personal hostnames,
  IPs, names or emails. Commit as `charlie-nolan <122530184+charlie-nolan@users.noreply.github.com>`.
- Fix bugs at the general cause and test on several cars and cases, never just one car.

## Layout

| Path | What |
|---|---|
| `aligndeck/align_math.py` | Geometry and turns (stdlib only) |
| `aligndeck/align_vision.py` | Targets (ids: left 0-16, right 17-33, floor 50-66; 5x7 squares of 36 mm, DICT_5X5_100), calibration, pose, spin axis, `measure_axle`, cameras |
| `aligndeck/align_specs.py` | Spec, rate, direction and pitch extraction from a make DB |
| `aligndeck/session.py` | The job state: specs, readings, guided calibration (`calib_*`), rates, plan |
| `aligndeck/server.py`, `web/index.html` | Web API and screen |
| `aligndeck/liveview.py`, `focus.py`, `vlm.py` | Live view, full compute, photo helper |
| `tests/` | Synthetic tests (rendered photos with known angles): `python -m unittest discover -s tests -v` |
| `hardware/lug_frame/` | Lug frame STLs, `make_lug_frame.py`, `make_plates.py` (220 mm bed) |
| `targets/` | Printable targets |
| `tools/` | Qwen2.5-VL-3B build for an RK3588 NPU |

Data folder: `~/.local/share/aligndeck` (`$ALIGNDECK_HOME`).

## Status (2026-11-04)

The math, the app and 17 synthetic tests are done (within 0.03°). **Not yet run on a real car or camera**: the
first real run on the 2002 Forester (Nikon D750) is next. When real logs come in, fix against them and add a test
built from the failing case.
