#!/bin/sh
# Citadel's stats script for Home Assistant OS (Probe-1): prints one JSON object in the same shape as the Linux
# citadel-stats, and exits.
#
# Home Assistant OS has no Python and no shell of its own: this runs inside Home Assistant's "Terminal & SSH" app (an
# Alpine container), which sees the Pi's own /proc/stat, /proc/meminfo, /proc/loadavg and /sys sensors, but only its
# own processes, so there are no top processes ("top" stays empty). The OS name and hostname come from the Supervisor
# (the app is given a token for it); without one they're just "Home Assistant OS" and "homeassistant".
#
# Installed as /data/citadel-stats-haos.sh (the app's own storage, kept across restarts and updates) by
# `tools/machines_setup.py homeassistant --haos`, and run only through the collector's key, whose line in the app's
# settings forces this command with `restrict`. Uses only BusyBox, awk and jq, all part of the app.
set -u

cpu_ticks() { awk '/^cpu /{print $5 + $6, $2 + $3 + $4 + $5 + $6 + $7 + $8 + $9; exit}' /proc/stat; }
A=$(cpu_ticks)
sleep 0.5
B=$(cpu_ticks)
CPU=$(echo "$A $B" | awk '{d = $4 - $2; if (d > 0) printf "%.1f", 100 * (1 - ($3 - $1) / d); else print "null"}')
CORES=$(grep -c '^cpu[0-9]' /proc/stat)

TEMP=null
for d in /sys/class/hwmon/hwmon*; do
  if [ "$(cat "$d/name" 2>/dev/null)" = cpu_thermal ] && [ -r "$d/temp1_input" ]; then
    TEMP=$(awk '{printf "%.1f", $1 / 1000}' "$d/temp1_input")
  fi
done

# rpi_volt's alarm is the firmware's "under-voltage now" flag (vcgencmd isn't available here, so nothing since boot)
LOWV=null
for d in /sys/class/hwmon/hwmon*; do
  if [ "$(cat "$d/name" 2>/dev/null)" = rpi_volt ] && [ -r "$d/in0_lcrit_alarm" ]; then
    LOWV=$([ "$(cat "$d/in0_lcrit_alarm")" = 1 ] && echo true || echo false)
  fi
done
PI=null
[ "$LOWV" != null ] && PI="{\"throttled\":null,\"under_voltage_now\":$LOWV,\"under_voltage_seen\":null}"

# %.0f, not %d: BusyBox awk caps %d at 2 GB
MEM=$(awk '{v[$1] = $2 * 1024} END {
  printf "{\"total\":%.0f,\"used\":%.0f,\"available\":%.0f,\"swap_total\":%.0f,\"swap_used\":%.0f,\"buffers\":%.0f,\"cached\":%.0f}",
    v["MemTotal:"], v["MemTotal:"] - v["MemAvailable:"], v["MemAvailable:"], v["SwapTotal:"], v["SwapTotal:"] - v["SwapFree:"],
    v["Buffers:"], v["Cached:"] }' /proc/meminfo)

# Home Assistant's data partition (config, backups, media), which /data sits on: the disk that can fill up
DISK=$(df -P -B1 /data 2>/dev/null | awk 'NR == 2 {printf "[{\"mount\":\"/\",\"total\":%.0f,\"free\":%.0f}]", $2, $4}')
[ -n "$DISK" ] || DISK='[]'

TOKEN=${SUPERVISOR_TOKEN:-}
[ -n "$TOKEN" ] || TOKEN=$(sed -n 's/^SUPERVISOR_TOKEN=//p' /data/.ssh/environment 2>/dev/null | tr -d '"')
INFO='{}'
if [ -n "$TOKEN" ]; then
  INFO=$(curl -s -m 3 -H "Authorization: Bearer $TOKEN" http://supervisor/host/info 2>/dev/null | jq -c '.data // {}' 2>/dev/null) || INFO='{}'
  [ -n "$INFO" ] || INFO='{}'
fi

MODEL=$(sed -n 's/^Model[[:space:]]*: //p' /proc/cpuinfo | head -n 1)
PART=$(sed -n 's/^CPU part[[:space:]]*: //p' /proc/cpuinfo | head -n 1)
MAXF=$(cat /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null || echo null)
PHYS=$(cat /sys/devices/system/cpu/cpu[0-9]*/topology/core_id 2>/dev/null | sort -u | wc -l)
[ "$PHYS" -gt 0 ] 2>/dev/null || PHYS=$CORES
read -r UP _ < /proc/uptime

jq -cn --argjson info "$INFO" --arg model "$MODEL" --arg part "$PART" --arg kernel "$(uname -r)" --arg arch "$(uname -m)" \
  --argjson cpu "$CPU" --argjson cores "$CORES" --argjson phys "$PHYS" --argjson temp "$TEMP" --argjson maxf "$MAXF" \
  --argjson mem "$MEM" --argjson disks "$DISK" --argjson pi "$PI" --arg up "$UP" --arg load "$(cut -d' ' -f1-3 /proc/loadavg)" '{
  v: 1,
  host: ($info.hostname // "homeassistant"),
  os: ($info.operating_system // "Home Assistant OS"),
  uptime_s: ($up | tonumber | floor),
  load: ($load | split(" ") | map(tonumber)),
  cpu: {percent: $cpu, cores: $cores, temp: $temp},
  mem: $mem,
  disks: $disks,
  gpu: null,
  battery: null,
  pi: $pi,
  top: [],
  spec: {model: (if $model == "" then null else $model end), cpu: null, cpu_part: (if $part == "" then null else $part end),
         cores: $phys, threads: $cores, max_mhz: (if $maxf == null then null else ($maxf / 1000 | round) end),
         gpus: [], kernel: ($info.kernel // $kernel), arch: $arch}
}'
