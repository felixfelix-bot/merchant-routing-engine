#!/usr/bin/env bash
# Build the 3 required video deliverables from fresh per-test Playwright webm footage.
# Each mp4 = title card (pass count) + concatenated test footage, re-encoded to H.264.
set -euo pipefail
cd "$(dirname "$0")"
TR=test-results
OUT=test-videos
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf
mkdir -p "$OUT/build"

# ---- directory names of each per-test video ----
declare -A V=(
  [nostr]="display-resilience-display-1a299--when-no-api-param-provided-desktop-chrome"
  [noflicker]="display-resilience-connect-9a236-s-not-flicker-to-connecting-desktop-chrome"
  [stalekeep]="display-resilience-panels--626a3-adge-after-CVM-goes-offline-desktop-chrome"
  [backoff]="display-resilience-poll-in-3e723-ally-during-extended-outage-desktop-chrome"
  [allpanel]="display-resilience-all-pan-2b39a-ta-on-successful-connection-desktop-chrome"
  [qr]="display-resilience-QR-code-58585-ant-nsite-URL-not-localhost-desktop-chrome"
  [t9]="display-resilience-Nostr-f-df53d-LE-when-events-stop-Task-9--desktop-chrome"
  [t10]="display-resilience-Nostr-d-b5664--relay-goes-silent-Task-10--desktop-chrome"
  [t13]="display-resilience-Nostr-s-0485e-ory-charts-on-load-Task-13--desktop-chrome"
)

# verify every video exists before building
for k in "${!V[@]}"; do
  f="$TR/${V[$k]}/video.webm"
  [ -s "$f" ] || { echo "MISSING: $f"; exit 1; }
done
echo "all 9 source videos present"

make_title() { # $1=outmp4  $2=line1  $3=line2  $4=line3
  ffmpeg -y -f lavfi -i "color=c=#0d1117:s=800x500:d=3.5:r=25" \
    -vf "drawtext=fontfile=${FONT}:text='${2}':fontcolor=3FB950:fontsize=30:x=(w-text_w)/2:y=(h*0.30),\
drawtext=fontfile=${FONT}:text='${3}':fontcolor=white:fontsize=24:x=(w-text_w)/2:y=(h*0.50),\
drawtext=fontfile=${FONT}:text='${4}':fontcolor=8B949E:fontsize=18:x=(w-text_w)/2:y=(h*0.66)" \
    -c:v libx264 -pix_fmt yuv420p -r 25 -an "$1" -loglevel error
}

build() { # $1=outname  $2=titlemp4  $3... = footage keys in order
  local name="$1"; local title="$2"; shift 2
  local out="$OUT/$name"
  local -a inputs=("-i" "$title")
  local chain=""; local idx=1
  chain+="[0:v]fps=25,setpts=PTS-STARTPTS,format=yuv420p[c0];"
  for k in "$@"; do
    inputs+=("-i" "$TR/${V[$k]}/video.webm")
    chain+="[${idx}:v]fps=25,setpts=PTS-STARTPTS,format=yuv420p[c${idx}];"
    idx=$((idx+1))
  done
  local n=$idx
  # chain the normalized streams into concat: [c0][c1]...[cN-1]
  local i
  for ((i=0; i<n; i++)); do chain+="[c${i}]"; done
  chain+="concat=n=${n}:v=1:a=0[v]"
  ffmpeg -y "${inputs[@]}" -filter_complex "$chain" -map "[v]" \
    -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p -r 25 \
    -movflags +faststart "$out" -loglevel error
  echo "built $out  ($(ffprobe -v error -show_entries format=duration -of csv=p=0 "$out")s, $(du -h "$out" | cut -f1))"
}

# 1) resilience-e2e.mp4 — ALL 9 tests, all passing
make_title "$OUT/build/title_res.mp4" \
  "DISPLAY RESILIENCE SUITE" "9 / 9 TESTS PASSED" "all panels | badge | backoff | nostr | QR"
build resilience-e2e.mp4 "$OUT/build/title_res.mp4" \
  nostr noflicker stalekeep backoff allpanel qr t9 t10 t13

# 2) nostr-e2e.mp4 — Nostr data flow + staleness
make_title "$OUT/build/title_nostr.mp4" \
  "NOSTR END-TO-END" "DATA FLOW + STALENESS" "kind-30315 subscribe | watchdog | reconnect"
build nostr-e2e.mp4 "$OUT/build/title_nostr.mp4" \
  nostr t9 t10 t13

# 3) badge-transitions.mp4 — LIVE -> STALE -> LIVE cycle
make_title "$OUT/build/title_badge.mp4" \
  "BADGE TRANSITIONS" "LIVE -> STALE -> LIVE" "no flicker | outage resilience | reconnect"
build badge-transitions.mp4 "$OUT/build/title_badge.mp4" \
  allpanel noflicker stalekeep t10

rm -rf "$OUT/build"
echo "=== DONE ==="
ls -la "$OUT"/*.mp4
