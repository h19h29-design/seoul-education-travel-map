#!/bin/sh
set -eu

blocked() {
  printf '%s\n' "$1" >&2
  exit 2
}

physical_regular_file() {
  target=$1
  [ -f "$target" ] && [ ! -L "$target" ] || return 1
  parent=${target%/*}
  base=${target##*/}
  [ -n "$base" ] || return 1
  [ -n "$parent" ] || parent=/
  physical_parent=$(CDPATH= cd -- "$parent" 2>/dev/null && pwd -P 2>/dev/null) \
    || return 1
  if [ "$physical_parent" = / ]; then
    canonical=/$base
  else
    canonical=$physical_parent/$base
  fi
  [ "$target" = "$canonical" ]
}

physical_directory() {
  target=$1
  [ -d "$target" ] && [ ! -L "$target" ] || return 1
  physical=$(CDPATH= cd -- "$target" 2>/dev/null && pwd -P 2>/dev/null) \
    || return 1
  [ "$target" = "$physical" ]
}

[ "$#" -eq 4 ] || blocked BLOCKED_INVALID_ARGUMENTS
[ "$1" = --job-config ] || blocked BLOCKED_INVALID_ARGUMENTS
[ "$3" = --backup-root ] || blocked BLOCKED_INVALID_ARGUMENTS
job_config=$2
backup_root=$4

case "$job_config" in
  /*) ;;
  *) blocked BLOCKED_INVALID_JOB_CONFIG_PATH ;;
esac
case "$backup_root" in
  /) blocked BLOCKED_INVALID_BACKUP_ROOT ;;
  /*) ;;
  *) blocked BLOCKED_INVALID_BACKUP_ROOT ;;
esac
physical_regular_file "$job_config" || blocked BLOCKED_INVALID_JOB_CONFIG_PATH
physical_directory "$backup_root" || blocked BLOCKED_INVALID_BACKUP_ROOT

script_path=$0
case "$script_path" in
  /*) ;;
  *) script_path=$(pwd -P 2>/dev/null)/$script_path ;;
esac
script_directory=${script_path%/*}
[ -d "$script_directory" ] || blocked BLOCKED_INVALID_EXCLUSION_FILE
script_directory=$(CDPATH= cd -- "$script_directory" 2>/dev/null && pwd -P 2>/dev/null) \
  || blocked BLOCKED_INVALID_EXCLUSION_FILE
exclusion_file=$script_directory/backup-excludes.txt
physical_regular_file "$exclusion_file" || blocked BLOCKED_INVALID_EXCLUSION_FILE

expected_excludes='/volume2/docker-1/seoul-education-travel-map/data/
*.sqlite3
*.sqlite3-wal
*.sqlite3-shm'
actual_excludes=$(cat "$exclusion_file" 2>/dev/null) \
  || blocked BLOCKED_INVALID_EXCLUSION_FILE
line_count=$(LC_ALL=C wc -l < "$exclusion_file" 2>/dev/null | tr -d ' ') \
  || blocked BLOCKED_INVALID_EXCLUSION_FILE
[ "$actual_excludes" = "$expected_excludes" ] && [ "$line_count" = 4 ] \
  || blocked BLOCKED_INVALID_EXCLUSION_FILE

active_reference_count=$(grep -Fxc -- "ACTIVE_EXCLUSION_FILE=$exclusion_file" \
  "$job_config" 2>/dev/null || true)
active_field_count=$(grep -Ec '^ACTIVE_EXCLUSION_FILE=' "$job_config" 2>/dev/null || true)
[ "$active_reference_count" = 1 ] && [ "$active_field_count" = 1 ] \
  || blocked BLOCKED_MISSING_ACTIVE_EXCLUSION_REFERENCE

if backup_artifact=$(find "$backup_root" -xdev \
  \( -type f -o -type l \) \
  \( -name '*.sqlite3' -o -name '*.sqlite3-wal' -o -name '*.sqlite3-shm' \) \
  -print -quit 2>/dev/null); then
  :
else
  blocked BLOCKED_BACKUP_SCAN_FAILED
fi
[ -z "$backup_artifact" ] || blocked BLOCKED_DATABASE_ARTIFACT_IN_BACKUP

printf '%s\n' BACKUP_EXCLUSION_OK
