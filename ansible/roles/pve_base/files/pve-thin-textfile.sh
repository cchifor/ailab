#!/usr/bin/env bash
# pve-thin-textfile - publish the Proxmox host's LVM thin pool fill, and each thin volume's, for
# node_exporter's textfile collector. Deployed by the pve_base role (tag thin_textfile) as
# /usr/local/sbin/pve-thin-textfile; pve-thin-textfile.timer runs it every 2 min. It only READS (`lvs`).
#
# WHY. Freed-but-untrimmed blocks stay allocated in pve/data; on 2026-11-20 about 1.1 TiB of the three
# pools was such garbage, and nothing showed it: the pool fill was not exported, nor a volume's allocation
# against its guest's use. A full thin pool fails every guest's writes on that host.
#
# Writes <dir>/pve_thin.prom in one rename (a temp file in the same dir; node_exporter reads only *.prom):
#   pve_thin_pool_data_percent{pool}            data space used, % (lvs data_percent)
#   pve_thin_pool_metadata_percent{pool}        metadata space used, %
#   pve_thin_pool_size_bytes{pool}              the pool's data size
#   pve_thin_pool_metadata_size_bytes{pool}     the pool's metadata size
#   pve_thin_lv_size_bytes{pool,lv,vmid}        a thin volume's virtual size (vmid "" when the name has none)
#   pve_thin_lv_data_percent{pool,lv,vmid}      how much of it is allocated in the pool, %
#   pve_thin_lv_allocated_bytes{pool,lv,vmid}   size x data% / 100: what the volume holds in the pool
#   pve_thin_lv_open{pool,lv,vmid}              1 while the volume's device is open (lvs lv_device_open): QEMU
#                                               holds an attached disk open, and a container's mounted volume is
#                                               open; a source kept after a disk move or an unused disk is not.
#                                               The Local Drives dashboard needs it before it subtracts a VM's
#                                               filesystems from its allocation (the untrimmed gap)
#   pve_thin_textfile_read_errors               lvs failed, or a line or value did not parse (left out above);
#                                               each one is also said on stderr (the unit's journal), with lvs's
#                                               own message or the rejected line
#   pve_thin_textfile_timestamp_seconds         when this file was written (the staleness alert reads it)
#
# Environment: PVE_THIN_POOLS (default pve/data; space-separated VG/LV), PVE_THIN_TEXTFILE_DIR (default
# /var/lib/prometheus/node-exporter), PVE_THIN_NOW (test seam: the timestamp to write).
set -u

POOLS="${PVE_THIN_POOLS:-pve/data}"
DIR="${PVE_THIN_TEXTFILE_DIR:-/var/lib/prometheus/node-exporter}"
NOW="${PVE_THIN_NOW:-$(date +%s)}"
OUT="$DIR/pve_thin.prom"

errors=0
# Count a read error and say why on stderr, which the unit's journal keeps.
read_error() { errors=$((errors + 1)); echo "pve-thin-textfile: $*" >&2; }
errf="$(mktemp 2> /dev/null)" || errf=/dev/null
declare -A HELP=(
  [pve_thin_pool_data_percent]="LVM thin pool data space used, percent."
  [pve_thin_pool_metadata_percent]="LVM thin pool metadata space used, percent."
  [pve_thin_pool_size_bytes]="LVM thin pool data size in bytes."
  [pve_thin_pool_metadata_size_bytes]="LVM thin pool metadata size in bytes."
  [pve_thin_lv_size_bytes]="Thin volume virtual size in bytes."
  [pve_thin_lv_data_percent]="Share of the thin volume allocated in its pool, percent."
  [pve_thin_lv_allocated_bytes]="Bytes the thin volume holds in its pool (size x data percent)."
  [pve_thin_lv_open]="1 while the thin volume's device is open (an attached disk, a mounted volume)."
  [pve_thin_textfile_read_errors]="lvs calls or lines that failed or did not parse in the last run."
  [pve_thin_textfile_timestamp_seconds]="Unix time at which pve-thin-textfile last wrote this file."
)
FAMILIES=(pve_thin_pool_data_percent pve_thin_pool_metadata_percent pve_thin_pool_size_bytes
  pve_thin_pool_metadata_size_bytes pve_thin_lv_size_bytes pve_thin_lv_data_percent pve_thin_lv_allocated_bytes
  pve_thin_lv_open
  pve_thin_textfile_read_errors pve_thin_textfile_timestamp_seconds)
declare -A SAMPLES=()
sample() { SAMPLES[$1]+="$1$2 $3"$'\n'; } # $1 family, $2 labels ("" or {..}), $3 value

NUM='^[0-9]+$'
PCT='^[0-9]+(\.[0-9]+)?$'
NAME='^[A-Za-z0-9+_.-]+$'

for pool in $POOLS; do
  vg="${pool%%/*}"; plv="${pool#*/}"
  if ! [[ "$pool" == */* && "$vg" =~ $NAME && "$plv" =~ $NAME ]]; then
    read_error "'$pool' is not a VG/LV name"; continue
  fi
  # One call per pool: the pool itself and every volume in it. --units b --nosuffix: plain byte counts.
  if ! out="$(lvs --noheadings --units b --nosuffix --separator '|' \
    -o lv_name,pool_lv,segtype,lv_size,data_percent,metadata_percent,lv_metadata_size,lv_device_open "$vg" \
    2> "$errf")"; then
    read_error "lvs $vg failed: $(tr '\n' ' ' < "$errf" 2> /dev/null)"; continue
  fi
  pl="{pool=\"$pool\"}"
  seen_pool=0
  while IFS= read -r line; do
    IFS='|' read -r name lvpool segtype size data meta msize devopen <<< "$line"
    name="${name//[[:space:]]/}"; lvpool="${lvpool//[[:space:]]/}"; segtype="${segtype//[[:space:]]/}"
    size="${size//[[:space:]]/}"; data="${data//[[:space:]]/}"; meta="${meta//[[:space:]]/}"; msize="${msize//[[:space:]]/}"
    devopen="${devopen//[[:space:]]/}"
    [ -n "$name" ] || continue
    if [ "$name" = "$plv" ] && [ "$segtype" = thin-pool ]; then
      seen_pool=1
      if [[ "$data" =~ $PCT && "$meta" =~ $PCT && "$size" =~ $NUM && "$msize" =~ $NUM ]]; then
        sample pve_thin_pool_data_percent "$pl" "$data"
        sample pve_thin_pool_metadata_percent "$pl" "$meta"
        sample pve_thin_pool_size_bytes "$pl" "$size"
        sample pve_thin_pool_metadata_size_bytes "$pl" "$msize"
      else
        read_error "cannot parse the pool's row: $line"
      fi
    elif [ "$lvpool" = "$plv" ] && [ "$segtype" = thin ]; then
      # An inactive volume (a template's base-* volumes skip activation) has no readable allocation.
      [ -n "$data" ] || continue
      # lv_device_open prints "open", nothing (active, not open) or "unknown" (inactive; seen with real lvs in
      # the converge test). Only "open" is open.
      if [[ "$name" =~ $NAME && "$size" =~ $NUM && "$data" =~ $PCT && "$devopen" =~ ^(open|unknown)?$ ]]; then
        vmid=""
        [[ "$name" =~ ^(vm|base)-([0-9]+)- ]] && vmid="${BASH_REMATCH[2]}"
        l="{pool=\"$pool\",lv=\"$name\",vmid=\"$vmid\"}"
        sample pve_thin_lv_size_bytes "$l" "$size"
        sample pve_thin_lv_data_percent "$l" "$data"
        sample pve_thin_lv_allocated_bytes "$l" "$(awk -v s="$size" -v p="$data" 'BEGIN { printf "%.0f", s * p / 100 }')"
        sample pve_thin_lv_open "$l" "$([ "$devopen" = open ] && echo 1 || echo 0)"
      else
        read_error "cannot parse a volume's row: $line"
      fi
    fi
  done <<< "$out"
  [ "$seen_pool" -eq 1 ] || read_error "the pool $pool is not in the listing of VG $vg"
done
[ "$errf" = /dev/null ] || rm -f "$errf"
sample pve_thin_textfile_read_errors "" "$errors"
sample pve_thin_textfile_timestamp_seconds "" "$NOW"

tmp="$DIR/.pve_thin.prom.$$"
trap 'rm -f "$tmp"' EXIT
{
  for family in "${FAMILIES[@]}"; do
    [ -n "${SAMPLES[$family]:-}" ] || continue
    printf '# HELP %s %s\n# TYPE %s gauge\n%s' "$family" "${HELP[$family]}" "$family" "${SAMPLES[$family]}"
  done
} > "$tmp" || {
  echo "pve-thin-textfile: cannot write $tmp" >&2
  exit 1
}
chmod 0644 "$tmp"
mv -f "$tmp" "$OUT" || {
  echo "pve-thin-textfile: cannot rename $tmp to $OUT" >&2
  exit 1
}
