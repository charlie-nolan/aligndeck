# ADR-0004: Qwen2.5-VL export for the RK3588 NPU (tools/)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `tools/export_qwen25vl_rkllm.sh`

- Rkllm-env keeps its pinned torch/transformers; it only gains onnx (Rockchip README pins 1.18.0)
- Rknn-toolkit2 (vision tower conversion) gets its own venv so it can't change rkllm-env's packages.
- RKNN only takes float32 vision weights: flash attention off (Rockchip README step 3.2)
