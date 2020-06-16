#!/bin/bash
BACKUP_TYPE=$1
HOURS_TO_RETAIN_BACKUPS=$2
HOSTNAME=$(hostname)
TODAY=$(date +"%Y_%m_%d_%H")
BACKUP_FILE="blobdb_${BACKUP_TYPE}_${TODAY}.tar.gz"

{% if not aws_versioning_enabled%}
UPLOAD_NAME="${BACKUP_FILE}"
{% else %}
UPLOAD_NAME="blobdb_${BACKUP_TYPE}_${HOSTNAME}.tar.gz"
{% endif %}


tar -Pzcf "{{ blobdb_backup_dir }}/${BACKUP_FILE}" "{{ blobdb_dir_path }}"

# Remove old backups of this backup type
find {{ blobdb_backup_dir }} -mmin "+${HOURS_TO_RETAIN_BACKUPS}" -name "blobdb_${BACKUP_TYPE}_*" -delete;

{% if blobdb_s3 %}
( cd {{ blobdb_backup_dir }} && /usr/local/sbin/backup_snapshots.py "${BACKUP_FILE}" "${UPLOAD_NAME}" {{ blobdb_snapshot_bucket }} {{aws_endpoint}} )
{% endif %}
