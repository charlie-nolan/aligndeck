# ADR-0003: Lug frame dimensions (hardware/lug_frame/)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `hardware/lug_frame/make_lug_frame.py`

- `arm`: Post center radius range -> bolt circles 56-204 mm.
- `post`: 12 x 3 mm neodymium disc, glued in.
- `post`: M6 nut trap (top, under the arm)
- `post`: M6 bolt clearance.
- `post`: 15.5 mm recess centers on the nut dome stud end.
- `hub`: 36 mm flange: two hubs fit side by side beside the arms on a 220 bed.
- `target_mount`: Pad along the mast x across; sized so both sets fit one 220 bed.
- `target_mount`: Teardrop bore so it prints without supports.
