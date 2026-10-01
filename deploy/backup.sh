#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
if [[ "${DB_PROVIDER:-postgres}" != postgres ]]; then
  echo 'This backup job targets native PostgreSQL. Configure Supabase backups separately.' >&2
  exit 1
fi
: "${RESTIC_REPOSITORY:?An external backup repository is required}"
: "${RESTIC_PASSWORD:?Set the backup encryption secret}"
[[ "$RESTIC_PASSWORD" != REPLACE_* && ${#RESTIC_PASSWORD} -ge 16 ]] || {
  echo 'Generate a backup encryption password of at least 16 characters' >&2; exit 1;
}
case "$RESTIC_REPOSITORY" in
  s3:https://*|sftp:*|b2:*|azure:*|gs:*|rest:https://*) ;;
  *) echo 'Refusing a local or unencrypted-transport backup repository' >&2; exit 1 ;;
esac
: "${PGDATABASE:?Set PGDATABASE}"
mkdir -p /work
backup_dir="$(mktemp -d /work/backup.XXXXXXXX)"
trap 'rm -rf -- "$backup_dir"' EXIT
stamp="$(date -u +%Y-%m-%d-%H%M%S)-${backup_dir##*.}"
dump="$backup_dir/production-$stamp.dump"
pg_dump --format=custom --no-owner --no-privileges --file="$dump"
pg_restore --list "$dump" >/dev/null
(cd "$backup_dir" && sha256sum ./*.dump > SHA256SUMS)
# Stable path, unique file names, stable host/tag for predictable retention.
restic backup --host ive-production --tag postgres "$backup_dir"
restic forget --host ive-production --tag postgres --group-by host,tags \
  --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune
printf 'External PostgreSQL backup completed at %s\n' "$stamp"
