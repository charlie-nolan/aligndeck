#!/usr/bin/env bash
# Build Rockchip's multimodal runner on the RK3588 board (native, a few minutes) and put it next to the model files.
#   bash tools/build_vlm_runner_rk3588.sh
# Needs: $ALIGNDECK_HOME/models/qwen2.5-vl-3b/ (default ~/.local/share/aligndeck) with the .rknn and .rkllm from export_qwen25vl_rkllm.sh,
#        sudo apt install cmake g++ (once)
set -euo pipefail
OUT=${ALIGNDECK_HOME:-$HOME/.local/share/aligndeck}/models/qwen2.5-vl-3b
RL=${RKNN_LLM:-$HOME/rknn-llm}
if [ ! -d "$RL/examples/multimodal_model_demo/deploy" ]; then
  echo "== getting Rockchip's rknn-llm (examples + runtime)"
  git clone --depth 1 https://github.com/airockchip/rknn-llm "$RL"
fi
ls "$OUT"/*.rknn "$OUT"/*.rkllm >/dev/null || { echo "model files missing in $OUT: run tools/export_qwen25vl_rkllm.sh on a PC first"; exit 1; }
cd "$RL/examples/multimodal_model_demo/deploy"
rm -rf build && mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=aarch64
make -j"$(nproc)"
make install
INST=$(dirname "$(find .. -path '*install/demo_Linux_aarch64/demo' -type f | head -1)")
cp "$INST/demo" "$OUT/demo"
rm -rf "$OUT/lib" && cp -r "$INST/lib" "$OUT/lib"
echo "== quick test (the first run loads the model, ~20 s)"
cd "$OUT"
echo -e "<image>What is in the image?\nexit" | LD_LIBRARY_PATH="$OUT/lib" ./demo "$RL/examples/multimodal_model_demo/data/demo.jpg" \
  "$(ls "$OUT"/*.rknn | head -1)" "" "" "$(ls "$OUT"/*.rkllm | head -1)" 64 2048 3 rk3588 \
  "<|vision_start|>" "<|vision_end|>" "<|image_pad|>" | tail -3
echo "Done. The alignment app finds it by itself (FULL COMPUTE tab shows which vision model it uses)."
