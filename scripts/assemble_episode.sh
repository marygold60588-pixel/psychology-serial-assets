#!/usr/bin/env bash
set -euo pipefail

normalize_clip() {
  ffmpeg -y -nostdin -hide_banner -loglevel error \
    -i "$1" \
    -vf "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,format=yuv420p" \
    -an -c:v libx264 -pix_fmt yuv420p -r 30 -movflags +faststart \
    "$2"
}

concat_clips() {
  local list_dir
  list_dir="$(dirname "$1")"
  (
    cd "$list_dir"
    ffmpeg -y -nostdin -hide_banner -loglevel error \
      -f concat -safe 0 -i "$(basename "$1")" \
      -c copy -movflags +faststart \
      final-concat.mp4
  )
  mv "$list_dir/final-concat.mp4" "$2"
}

self_test() {
  command -v ffmpeg >/dev/null
  command -v ffprobe >/dev/null
  local tmp
  tmp="$(mktemp -d)"
  ffmpeg -y -nostdin -hide_banner -loglevel error \
    -f lavfi -i "testsrc=size=640x360:rate=24:duration=1" \
    -an -c:v libx264 -pix_fmt yuv420p "$tmp/wide.mp4"
  ffmpeg -y -nostdin -hide_banner -loglevel error \
    -f lavfi -i "testsrc=size=1080x1080:rate=30:duration=1" \
    -an -c:v libx264 -pix_fmt yuv420p "$tmp/square.mp4"
  normalize_clip "$tmp/wide.mp4" "$tmp/clip-01.mp4"
  normalize_clip "$tmp/square.mp4" "$tmp/clip-02.mp4"
  printf "file '%s'\nfile '%s'\n" "clip-01.mp4" "clip-02.mp4" > "$tmp/list.txt"
  concat_clips "$tmp/list.txt" "$tmp/final.mp4"
  local width height duration
  width="$(ffprobe -v error -select_streams v:0 -show_entries stream=width -of csv=p=0 "$tmp/final.mp4")"
  height="$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$tmp/final.mp4")"
  duration="$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$tmp/final.mp4")"
  rm -rf "$tmp"
  if [ "$width" != "1080" ] || [ "$height" != "1920" ]; then
    echo "expected 1080x1920, got ${width}x${height}" >&2
    exit 1
  fi
  echo "ffmpeg assemble ok ${width}x${height} ${duration}s"
}

if [ "${1:-}" = "--self-test" ]; then
  self_test
  exit 0
fi

fail() {
  local message="$1"
  echo "$message" >&2
  if [ -n "${EPISODE:-}" ] && [ -n "${REQUESTED:-}" ]; then
    mkdir -p "episodes/$EPISODE"
    jq -n \
      --arg episode_id "$EPISODE" \
      --arg requested_at "$REQUESTED" \
      --arg final_path "episodes/$EPISODE/final.mp4" \
      --arg error "$message" \
      '{ok: false, episode_id: $episode_id, requested_at: $requested_at, final_path: $final_path, error: $error}' \
      > "episodes/$EPISODE/assemble-result.json"
    exit 0
  fi
  exit 1
}

REQUEST_FILE="${1:-}"
if [ -z "$REQUEST_FILE" ] || [ ! -f "$REQUEST_FILE" ]; then
  echo "usage: assemble_episode.sh episodes/<episode_id>/assemble-request.json" >&2
  exit 1
fi

EPISODE="$(basename "$(dirname "$REQUEST_FILE")")"
if ! printf '%s' "$EPISODE" | grep -Eq '^[0-9a-fA-F-]{36}$'; then
  echo "bad episode id: $REQUEST_FILE" >&2
  exit 1
fi

REQUESTED="$(jq -r '.requested_at // empty' "$REQUEST_FILE")"
if [ -z "$REQUESTED" ]; then
  echo "assemble request has no requested_at" >&2
  exit 1
fi

WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

INDEX=0
while IFS= read -r SCENE; do
  INDEX=$((INDEX + 1))
  NUMBER="$(printf '%s' "$SCENE" | jq -r '.scene_number')"
  URL="$(printf '%s' "$SCENE" | jq -r '.video_url')"
  case "$URL" in
    https://*) ;;
    *) fail "scene $NUMBER has no https video_url" ;;
  esac
  PAD="$(printf '%02d' "$INDEX")"
  curl -fsSL --retry 3 --retry-delay 2 --output "$WORKDIR/source-$PAD.mp4" "$URL" || fail "download scene $NUMBER"
  normalize_clip "$WORKDIR/source-$PAD.mp4" "$WORKDIR/clip-$PAD.mp4" || fail "normalize scene $NUMBER"
  printf "file '%s'\n" "clip-$PAD.mp4" >> "$WORKDIR/list.txt"
done < <(jq -c '.scenes | sort_by(.scene_number) | .[]' "$REQUEST_FILE")

if [ "$INDEX" -lt 1 ]; then
  fail "assemble request has no scenes"
fi

mkdir -p "episodes/$EPISODE"
concat_clips "$WORKDIR/list.txt" "$WORKDIR/final.mp4" || fail "concat"
mv "$WORKDIR/final.mp4" "episodes/$EPISODE/final.mp4"
jq -n \
  --arg episode_id "$EPISODE" \
  --arg requested_at "$REQUESTED" \
  --arg final_path "episodes/$EPISODE/final.mp4" \
  '{ok: true, episode_id: $episode_id, requested_at: $requested_at, final_path: $final_path, error: ""}' \
  > "episodes/$EPISODE/assemble-result.json" || fail "could not write assemble result"
echo "assembled $EPISODE scenes=$INDEX"
