#!/usr/bin/env bash
# Build Qwen2.5-VL-3B for an RK3588 NPU (run on an x86 Linux PC, about an hour, ~40 GB RAM, ~25 GB disk).
# Needs a venv with Rockchip's rkllm-toolkit 1.3.x ($RKLLM_ENV, default ~/rkllm-env).
#   bash tools/export_qwen25vl_rkllm.sh
# Output: models/qwen2.5-vl-3b/ in the data folder ($ALIGNDECK_HOME, default ~/.local/share/aligndeck)
# Then copy that folder to the board (same path, e.g. rsync) and run tools/build_vlm_runner_rk3588.sh there.
# Uses Rockchip's own multimodal example scripts from ~/rknn-llm ($RKNN_LLM) (RKLLM 1.3.1), unchanged except
# where noted.
set -euo pipefail
ENV=${RKLLM_ENV:-$HOME/rkllm-env}
HF=${QWEN_VL_DIR:-$HOME/models/Qwen2.5-VL-3B-Instruct}
SRC=${RKNN_LLM:-$HOME/rknn-llm}/examples/multimodal_model_demo
OUT=${ALIGNDECK_HOME:-$HOME/.local/share/aligndeck}/models/qwen2.5-vl-3b
WORK=${ALIGNDECK_HOME:-$HOME/.local/share/aligndeck}/models/qwen2.5-vl-3b-build

RL=${RKNN_LLM:-$HOME/rknn-llm}
[ -d "$RL/examples/multimodal_model_demo" ] || git clone --depth 1 https://github.com/airockchip/rknn-llm "$RL"
source "$ENV/bin/activate"
python -c "import rkllm" || { echo "rkllm-toolkit is not in $ENV (see rknn-llm/rkllm-toolkit/packages)"; exit 1; }
pip install -q "onnx==1.18.0"
RKNN_ENV=${RKNN_ENV:-$HOME/rknn-env}
if [ ! -x "$RKNN_ENV/bin/python" ]; then
  python -m venv "$RKNN_ENV"
  "$RKNN_ENV/bin/pip" install -q "rknn-toolkit2>=2.3.2"
fi

if [ ! -f "$HF/config.json" ]; then
  echo "== downloading Qwen/Qwen2.5-VL-3B-Instruct to $HF (~7.5 GB)"
  python -c "from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen2.5-VL-3B-Instruct', local_dir='$HF')"
fi
python - "$HF/config.json" <<'PY'
import json, sys
p = sys.argv[1]; c = json.load(open(p))
if c.get("use_flash_attn") is not False:
    c["use_flash_attn"] = False; json.dump(c, open(p, "w"), indent=2); print("use_flash_attn -> false")
PY

mkdir -p "$WORK" "$OUT"
cp -r "$SRC/export" "$SRC/data" "$WORK/"
cd "$WORK"

echo "== 1/4 vision tower -> ONNX"
python export/export_vision.py --path "$HF" --model_name qwen2_5-vl-3b --height 448 --width 448
echo "== 2/4 ONNX -> RKNN (rk3588)"
"$RKNN_ENV/bin/python" export/export_vision_rknn.py --path ./onnx/qwen2_5-vl-3b_vision.onnx --model_name qwen2_5-vl-3b \
       --target-platform rk3588 --height 448 --width 448
echo "== 3/4 calibration inputs for the language model"
python data/make_input_embeds_for_quantize.py --path "$HF" --model_type qwen2.5vl
echo "== 4/4 language model -> RKLLM w8a8"
sed -e "s#dataset = 'data/inputs.json'#dataset = 'data/llm_inputs.json'#" export/export_rkllm.py > export/export_rkllm_qwen25.py
python export/export_rkllm_qwen25.py --path "$HF" --target-platform rk3588 --num_npu_core 3 --quantized_dtype w8a8 --device cpu

cp rknn/qwen2_5-vl-3b_vision_rk3588.rknn "$OUT/"
cp rkllm/*.rkllm "$OUT/"
ls -lh "$OUT"
echo "Done. Copy $OUT to the board (same path), then there: bash tools/build_vlm_runner_rk3588.sh"
