#!/bin/bash
# render.sh — render a Manim scene to MP4 and copy to dashboard output dir
# Usage: ./render.sh <scene_file.py> <SceneClassName> [extra manimgl flags]
#
# Output lands in ~/Axon/manim/output/<SceneClassName>.mp4
# The dashboard file viewer picks it up automatically.

set -e

MANIMGL="/home/lynnkse/.pyenv/versions/3.12.9/bin/manimgl"
OUTPUT_DIR="/home/lynnkse/Axon/manim/output"
SCENE_FILE="$1"
SCENE_NAME="$2"
shift 2 || true

if [[ -z "$SCENE_FILE" || -z "$SCENE_NAME" ]]; then
  echo "Usage: $0 <scene_file.py> <SceneClassName> [flags]"
  exit 1
fi

export DISPLAY=:1

echo "Rendering $SCENE_NAME from $SCENE_FILE ..."
"$MANIMGL" "$SCENE_FILE" "$SCENE_NAME" \
  -w \
  --video_dir "$OUTPUT_DIR" \
  "$@"

# manimgl may write to a subdirectory; flatten to output root
FOUND=$(find "$OUTPUT_DIR" -name "*.mp4" -newer /tmp 2>/dev/null | grep -v "^$OUTPUT_DIR/[^/]*$" | head -1)
if [[ -n "$FOUND" ]]; then
  mv "$FOUND" "$OUTPUT_DIR/$SCENE_NAME.mp4"
fi

echo "Done → $OUTPUT_DIR/$SCENE_NAME.mp4"
