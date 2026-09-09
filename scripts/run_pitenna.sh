#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MAIN_FILE="${PROJECT_ROOT}/code/main.py"

FORCE=0
if [[ "${1:-}" == "--force" ]]; then
  FORCE=1
  shift
fi

prompt_kill() {
  local type="$1"; shift
  local -a pids=("$@")
  if (( ${#pids[@]} == 0 )); then
    return
  fi
  if (( FORCE )); then
    echo "Killing ${type}: ${pids[*]}"
    for pid in "${pids[@]}"; do
      kill "$pid" 2>/dev/null || true
    done
    sleep 1
    return
  fi
  if [[ -t 0 ]]; then
    echo "${type}: ${pids[*]}"
    read -rp "Terminate these processes? [y/N] " reply
    if [[ "$reply" =~ ^[Yy] ]]; then
      for pid in "${pids[@]}"; do
        kill "$pid" 2>/dev/null || true
      done
      sleep 1
      return
    fi
    echo "Aborting launch. Rerun with --force to kill automatically."
  else
    echo "${type}: ${pids[*]} (non-interactive shell)."
    echo "Rerun with --force to terminate them automatically."
  fi
  exit 1
}

wait_for_release() {
  local label="$1" node="$2"
  local retries=10
  while (( retries > 0 )); do
    if ! fuser "$node" >/dev/null 2>&1; then
      return 0
    fi
    echo "${label} still busy ($node); waiting..."
    sleep 0.5
    (( retries-- ))
  done
  echo "${label} still busy after retries ($node)."
  return 1
}

running_pids=()
while IFS= read -r pid; do
  [[ -n "$pid" ]] && running_pids+=("$pid")
done < <(pgrep -f "$MAIN_FILE" || true)

prompt_kill "PiTenna already running (PID/s)" "${running_pids[@]}"
wait_for_release "PiTenna process" /dev/media0 || true
wait_for_release "PiTenna process" /dev/media1 || true

camera_pids=()
if command -v fuser >/dev/null 2>&1; then
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && camera_pids+=("$pid")
  done < <(fuser /dev/media0 /dev/media1 2>/dev/null | tr ' ' '\n' | sort -u || true)
fi

prompt_kill "Camera devices busy (PID/s)" "${camera_pids[@]}"
wait_for_release "Camera device" /dev/media0 || true
wait_for_release "Camera device" /dev/media1 || true

# Audio plays out of the HDMI0 card through the softvol plugin defined in
# ~/.asoundrc. These must match the defaults in code/main.py.
: "${HDMI1_AUDIO_DEVICE:=hdmi0softvol}"
: "${HDMI1_AUDIO_DEVICE_FALLBACK:=plughw:CARD=vc4hdmi0,DEV=0}"
: "${HDMI1_VOLUME_CARD:=vc4hdmi0}"
: "${HDMI1_VOLUME_CONTROL:=HDMI0 Master}"
export HDMI1_AUDIO_DEVICE HDMI1_AUDIO_DEVICE_FALLBACK HDMI1_VOLUME_CARD HDMI1_VOLUME_CONTROL

# Start at a sane level if the softvol control exists.
if command -v amixer >/dev/null 2>&1; then
  amixer -c "$HDMI1_VOLUME_CARD" sset "$HDMI1_VOLUME_CONTROL" 70% >/dev/null 2>&1 || true
fi

# Busy-check the real hardware PCM, not the softvol alias (which has no CARD=).
audio_device="${HDMI1_AUDIO_DEVICE_FALLBACK}"
audio_hw_card="${HDMI1_AUDIO_HW_CARD:-}"
audio_hw_dev="${HDMI1_AUDIO_HW_DEV:-}"

extract_param() {
  local key="$1" str="$2"
  if [[ "$str" =~ ${key}=([^,]+) ]]; then
    echo "${BASH_REMATCH[1]}"
  fi
}

if [[ -z "$audio_hw_card" ]]; then
  audio_hw_card=$(extract_param "CARD" "$audio_device" || true)
fi
if [[ -z "$audio_hw_dev" ]]; then
  audio_hw_dev=$(extract_param "DEV" "$audio_device" || true)
fi

resolve_card_index() {
  local card="$1"
  if [[ -z "$card" ]]; then
    echo ""
    return
  fi
  if [[ "$card" =~ ^[0-9]+$ ]]; then
    echo "$card"
    return
  fi
  python3 - "$card" <<'PY'
import sys
name = sys.argv[1]
try:
    int(name)
except ValueError:
    pass
else:
    print(name)
    sys.exit()
idx = ""
try:
    with open("/proc/asound/cards") as f:
        for line in f:
            line = line.rstrip()
            if not line:
                continue
            parts = line.split()
            if not parts:
                continue
            if not parts[0].isdigit():
                continue
            if "[" + name in line:
                print(parts[0])
                break
except FileNotFoundError:
    pass
PY
}

card_index=$(resolve_card_index "${audio_hw_card:-}")
dev_index="${audio_hw_dev:-0}"

audio_pids=()
if [[ -n "$card_index" && -n "$dev_index" ]] && command -v fuser >/dev/null 2>&1; then
  pcm_nodes=(
    "/dev/snd/pcmC${card_index}D${dev_index}p"
    "/dev/snd/pcmC${card_index}D${dev_index}c"
  )
  for node in "${pcm_nodes[@]}"; do
    [[ -e "$node" ]] || continue
    while IFS= read -r pid; do
      [[ -n "$pid" ]] && audio_pids+=("$pid")
    done < <(fuser "$node" 2>/dev/null | tr ' ' '\n' || true)
  done
fi

if (( ${#audio_pids[@]} > 0 )); then
  unique_audio_pids=($(printf "%s\n" "${audio_pids[@]}" | sort -u))
  prompt_kill "HDMI1 audio device busy (PID/s)" "${unique_audio_pids[@]}"
fi
if [[ -n "$card_index" && -n "$dev_index" ]]; then
  wait_for_release "HDMI1 audio device" "/dev/snd/pcmC${card_index}D${dev_index}p" || true
fi

export PYTHONUNBUFFERED=1
cd "${PROJECT_ROOT}"
exec python3 "$MAIN_FILE" "$@"

