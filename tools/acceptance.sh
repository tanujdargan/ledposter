#!/bin/bash
# On-device acceptance checks for docs/ACCEPTANCE.md. Run on the installed Pi as root:
#   sudo /data/app/current/tools/acceptance.sh [boot|refresh|limiter|transcode|preview|ha|panels|health|all]
# Needs the admin password for the API (LEDPOSTER_PASSWORD or a prompt). Prints a PASS/FAIL
# table; the eyes-on criteria (flicker, phone latency, Wi-Fi setup time) are prompted, not judged.
set -euo pipefail

DATA="${LEDPOSTER_DATA:-/data}"
URL="${LEDPOSTER_URL:-http://127.0.0.1}"
PY="$DATA/app/current/web/.venv/bin/python"
[ -x "$PY" ] || PY=python3
SOCK="$(sed -n 's/^  socket: *//p' "$DATA/config.yaml" 2> /dev/null | head -n 1)"
SOCK="${SOCK:-/run/ledposter/engine.sock}"
PSOCK="$(sed -n 's/^  preview_socket: *//p' "$DATA/config.yaml" 2> /dev/null | head -n 1)"
PSOCK="${PSOCK:-/run/ledposter/frames.sock}"
COOKIES="$(mktemp)"
RESULTS=()
trap 'rm -f "$COOKIES"' EXIT

say() { printf '\n== %s\n' "$*"; }
result() { RESULTS+=("$1|$2|$3"); printf '   %s: %s\n' "$1" "$3"; }  # name PASS|FAIL|INFO text

# engine control socket: ctl '{"cmd":"status"}'
ctl() { echo "$1" | "$PY" -c '
import json, socket, sys
s = socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect(sys.argv[1])
s.sendall(sys.stdin.read().strip().encode() + b"\n"); print(s.makefile().readline().strip())' "$SOCK"; }

login() {
  [ -s "$COOKIES" ] && return
  local pw="${LEDPOSTER_PASSWORD:-}"
  if [ -z "$pw" ]; then read -r -s -p "Admin password: " pw; echo; fi
  curl -fsS -c "$COOKIES" -H 'Content-Type: application/json' -d "{\"password\":\"$pw\"}" "$URL/api/auth/login" > /dev/null || { echo "login failed"; exit 1; }
}
api() { curl -fsS -b "$COOKIES" "$@"; }

check_boot() {
  say "1. Cold boot to first frame"
  local line secs
  line="$(journalctl -b -u ledposter-engine -o short-monotonic 2> /dev/null | grep -m1 'first frame' || true)"
  if [ -z "$line" ]; then result boot FAIL "no 'first frame' in this boot's engine journal (is the service running?)"; return; fi
  secs="$(echo "$line" | sed -n 's/^\[ *\([0-9.]*\)\].*/\1/p')"
  systemd-analyze 2> /dev/null | head -n 1 | sed 's/^/   /'
  if awk "BEGIN{exit !($secs < 26)}"; then result boot PASS "first frame at ${secs}s after the kernel started (+~3s firmware; budget 30s)"
  else result boot FAIL "first frame at ${secs}s after the kernel started (budget 30s incl. ~3s firmware)"; fi
}

check_refresh() {
  say "2. Refresh rate at default settings (stops the engine for ~10s)"
  local tmp hz
  tmp="$(mktemp -d)"
  sed 's/show_refresh_rate: *false/show_refresh_rate: true/' "$DATA/config.yaml" > "$tmp/config.yaml"
  grep -q 'show_refresh_rate: true' "$tmp/config.yaml" || echo 'matrix: {show_refresh_rate: true}' >> "$tmp/config.yaml"
  systemctl stop ledposter-engine.service
  # the library prints the refresh rate to the terminal; the engine's own log goes to stderr too
  timeout 8 env LEDPOSTER_DATA="$DATA" "$DATA/app/current/engine/ledengine" --config "$tmp/config.yaml" --pattern panel-id > "$tmp/out" 2>&1 || true
  systemctl start ledposter-engine.service
  hz="$(tr '\r' '\n' < "$tmp/out" | sed -n 's/.*\([0-9][0-9][0-9]*\.[0-9]*\)Hz.*/\1/p' | tail -n 20 | sort -n | awk '{a[NR]=$1} END{print a[int((NR+1)/2)]}')"
  rm -rf "$tmp"
  if [ -z "$hz" ]; then result refresh FAIL "could not read a refresh rate (output had no 'Hz')"; return; fi
  if awk "BEGIN{exit !($hz >= 150)}"; then result refresh PASS "${hz}Hz median (>= 150Hz). Now look at the wall: no flicker by eye?"
  else result refresh FAIL "${hz}Hz (< 150Hz): lower pwm_bits to 8 in Settings > Matrix"; fi
}

check_limiter() {
  say "3. Limiter on full white"
  login
  api -H 'Content-Type: application/json' -d '{"enabled":false}' "$URL/api/auto_brightness" > /dev/null
  api -H 'Content-Type: application/json' -d '{"value":100}' "$URL/api/brightness" > /dev/null
  api -H 'Content-Type: application/json' -d '{"mode":"pattern","pattern":"solid","color":"#ffffff"}' "$URL/api/mode" > /dev/null
  sleep 1
  local max=0 ceil="" bad=0 unl=""
  for _ in $(seq 1 30); do
    local st amps lim
    st="$(ctl '{"cmd":"status"}')"
    amps="$(echo "$st" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["amps"])')"
    ceil="$(echo "$st" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["ceiling_amps"])')"
    lim="$(echo "$st" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["limited"])')"
    unl="$(echo "$st" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["amps_unlimited"])')"
    awk "BEGIN{exit !($amps > $ceil)}" && bad=$((bad + 1))
    awk "BEGIN{exit !($amps > $max)}" && max="$amps"
    [ "$lim" = True ] || bad=$((bad + 1))
    sleep 0.2
  done
  api -H 'Content-Type: application/json' -d '{"enabled":true}' "$URL/api/auto_brightness" > /dev/null
  if [ "$bad" = 0 ]; then result limiter PASS "max ${max}A over 30 samples, ceiling ${ceil}A, unlimited ${unl}A, limiter active"
  else result limiter FAIL "$bad samples over the ceiling or not limited (max ${max}A, ceiling ${ceil}A)"; fi
}

check_transcode() {
  say "4. 1080p 30s MP4: upload, transcode, play"
  login
  local clip t0 id st t1 mode
  clip="$(mktemp -d)/hd30.mp4"
  ffmpeg -v error -y -f lavfi -i "testsrc2=s=1920x1080:r=30:d=30" -c:v libx264 -preset veryfast -pix_fmt yuv420p "$clip"
  t0=$(date +%s)
  id="$(api -F "file=@$clip" -F name=acceptance-1080p "$URL/api/media" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
  while :; do
    st="$(api "$URL/api/media/$id" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
    case "$st" in ready | failed) break ;; esac
    [ $(($(date +%s) - t0)) -gt 300 ] && break
    sleep 1
  done
  api -H 'Content-Type: application/json' -d "{\"mode\":\"media\",\"media\":\"$id\"}" "$URL/api/mode" > /dev/null || true
  sleep 1
  mode="$(ctl '{"cmd":"status"}' | "$PY" -c 'import json,sys; s=json.load(sys.stdin); print(s["mode"], s["mode_name"])')"
  t1=$(($(date +%s) - t0))
  rm -rf "$(dirname "$clip")"
  if [ "$st" = ready ] && [ "$mode" = "media $id" ] && [ "$t1" -le 120 ]; then result transcode PASS "ready and playing in ${t1}s (budget 120s); item $id left in the library"
  else result transcode FAIL "status $st, engine '$mode', ${t1}s"; fi
}

check_preview() {
  say "5. Preview latency: engine preview socket vs the app's WebSocket (server side)"
  login
  api -H 'Content-Type: application/json' -d '{"mode":"pattern","pattern":"cycle"}' "$URL/api/mode" > /dev/null
  local out
  out="$("$PY" - "$PSOCK" "$URL" "$COOKIES" <<'EOF' || true
import asyncio, json, socket, sys, time, threading
psock, url, cookies = sys.argv[1:4]
cookie = next((l.split()[6] for l in open(cookies) if "session" in l), "")
frames = {}  # frame bytes hash -> engine time
lock = threading.Lock()
def engine():
    s = socket.socket(socket.AF_UNIX); s.connect(psock); f = s.makefile("rb")
    while True:
        st = json.loads(f.readline()); fr = f.read(st["w"] * st["h"] * 3)
        with lock: frames.setdefault(hash(fr), time.monotonic())
threading.Thread(target=engine, daemon=True).start()
async def ws():
    import websockets
    lat = []; seen = set()
    uri = url.replace("http", "ws", 1) + "/api/preview"
    async with websockets.connect(uri, additional_headers={"Cookie": f"session={cookie}"}) as w:
        t_end = time.monotonic() + 24
        while time.monotonic() < t_end and len(lat) < 10:
            m = await w.recv()
            if isinstance(m, str): continue
            h = hash(bytes(m)); now = time.monotonic()
            with lock: t = frames.get(h)
            if t is not None and h not in seen:
                seen.add(h); lat.append((now - t) * 1000)
                if len(lat) % 2 == 0: await asyncio.sleep(1.8)  # wait for the next colour
    lat.sort()
    print(f"{lat[len(lat)//2]:.0f} {lat[-1]:.0f} {len(lat)}" if lat else "nan nan 0")
asyncio.run(ws())
EOF
)"
  local med="" mx="" n=0
  read -r med mx n <<< "$out" || true
  if [ "${n:-0}" -ge 5 ] && awk "BEGIN{exit !($mx < 200)}"; then result preview PASS "relay latency median ${med}ms, max ${mx}ms over $n frames (< 200ms). Check the phone next to the wall too"
  else result preview FAIL "could not measure or over budget: median ${med:-?}ms max ${mx:-?}ms over ${n:-0} frames"; fi
}

check_ha() {
  say "8. Home Assistant discovery topics (MQTT, milestone 7)"
  local host port user pass out
  host="$(sed -n 's/^  host: *//p' "$DATA/config.yaml" | head -n 1 | tr -d "'\"")"
  port="$(sed -n 's/^  port: *//p' "$DATA/config.yaml" | sed -n 2p)"
  user="$(sed -n 's/^  username: *//p' "$DATA/config.yaml" | head -n 1 | tr -d "'\"")"
  pass="$(sed -n 's/^  password: *//p' "$DATA/config.yaml" | head -n 1 | tr -d "'\"")"
  if ! command -v mosquitto_sub > /dev/null; then result ha INFO "mosquitto_sub not installed (sudo apt install mosquitto-clients); check HA by hand"; return; fi
  if [ -z "$host" ]; then result ha INFO "MQTT not configured in Settings > Integrations"; return; fi
  out="$(timeout 6 mosquitto_sub -h "$host" -p "${port:-1883}" ${user:+-u "$user"} ${pass:+-P "$pass"} -v -t 'homeassistant/#' 2> /dev/null | grep -i 'ledposter\|led poster' || true)"
  if echo "$out" | grep -q 'homeassistant/light/' && echo "$out" | grep -q 'homeassistant/sensor/' && echo "$out" | grep -q 'homeassistant/select/'; then
    result ha PASS "light, sensor and select discovery topics retained on $host. Now toggle, dim and pick an effect from HA"
  else result ha FAIL "discovery topics not all found on $host (got: $(echo "$out" | cut -d' ' -f1 | tr '\n' ' '))"; fi
}

check_panels() {
  say "9. Panel ID"
  login
  api -H 'Content-Type: application/json' -d '{"mode":"pattern","pattern":"panel-id"}' "$URL/api/mode" > /dev/null
  echo "   The wall should show, upright with up-arrows:  [1][2] / [4][3] / [5][6]"
  result panels INFO "visual: record a photo (fix matrix.panels in Settings > Matrix if not)"
}

check_health() {
  say "6. System health after power pulls"
  local ok=1 why=""
  for u in ledposter-engine.service ledposter-web.service; do systemctl is-active --quiet "$u" || { ok=0; why="$why $u not active;"; }; done
  grep -Eq 'overlayroot=tmpfs|boot=overlay' /proc/cmdline || { ok=0; why="$why overlay not active;"; }
  findmnt -no OPTIONS "$DATA" 2> /dev/null | grep -q noatime || { ok=0; why="$why $DATA not mounted noatime;"; }
  grep -qE "^[^#].* $DATA .*nofail" /etc/fstab 2> /dev/null || { ok=0; why="$why $DATA lacks nofail in fstab;"; }
  [ -f "$(readlink -f "$DATA/app/current")/.ready" ] || { ok=0; why="$why current release has no .ready;"; }
  # the ownership split that keeps the web app out of root (docs/ARCHITECTURE.md)
  [ "$(stat -c '%U:%G %a' "$DATA")" = "root:ledwall 1775" ] || { ok=0; why="$why $DATA is not root:ledwall 1775;"; }
  for d in app state logs; do
    [ "$(stat -c %U "$DATA/$d")" = root ] || { ok=0; why="$why $DATA/$d not owned by root;"; }
    if runuser -u ledwall -- test -w "$DATA/$d" 2> /dev/null; then ok=0; why="$why $DATA/$d writable by ledwall;"; fi
  done
  runuser -u ledwall -- test -x "$DATA/app/current/engine/ledengine" || { ok=0; why="$why ledwall cannot run the current release;"; }
  "$PY" -c "import yaml,sys; yaml.safe_load(open('$DATA/config.yaml'))" 2> /dev/null || { ok=0; why="$why config.yaml does not parse;"; }
  if find "$DATA" -name '*.tmp' -o -name '*.part' 2> /dev/null | grep -q .; then ok=0; why="$why temp files left under $DATA;"; fi
  local bad
  bad="$("$PY" - "$DATA/media" <<'EOF'
import json, os, sys
bad = 0
root = sys.argv[1]
for mid in os.listdir(root) if os.path.isdir(root) else []:
    d = os.path.join(root, mid)
    try: item = json.load(open(os.path.join(d, "item.json")))
    except Exception: continue
    if item.get("status") != "ready": continue
    try:
        meta = json.load(open(os.path.join(d, "meta.json")))
        want = len(meta["durations_ms"]) * meta["w"] * meta["h"] * 3
        if os.path.getsize(os.path.join(d, "frames.rgb")) != want: bad += 1
    except Exception: bad += 1
print(bad)
EOF
)"
  [ "$bad" = 0 ] || { ok=0; why="$why $bad ready media items with an inconsistent cache;"; }
  if [ "$ok" = 1 ]; then result health PASS "services active, overlay on, $DATA clean, consistent and owned as designed"; else result health FAIL "$why"; fi
}

what="${1:-all}"
case "$what" in
  boot) check_boot ;; refresh) check_refresh ;; limiter) check_limiter ;; transcode) check_transcode ;;
  preview) check_preview ;; ha) check_ha ;; panels) check_panels ;; health) check_health ;;
  all) check_boot; check_health; check_limiter; check_transcode; check_preview; check_ha; check_refresh; check_panels ;;
  *) echo "usage: $0 [boot|refresh|limiter|transcode|preview|ha|panels|health|all]"; exit 2 ;;
esac

printf '\n%-10s %-5s %s\n' CHECK RESULT DETAIL
pass=0 auto=0
for r in "${RESULTS[@]}"; do
  IFS='|' read -r n s t <<< "$r"
  printf '%-10s %-5s %s\n' "$n" "$s" "$t"
  [ "$s" = INFO ] || auto=$((auto + 1))
  [ "$s" = PASS ] && pass=$((pass + 1))
done
echo "ACCEPTANCE: $pass/$auto automated checks passed ($(git -C "$DATA/app/current" describe --tags --always 2> /dev/null || echo '?'))"
