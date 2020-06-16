#!/bin/bash
BACKUP_TYPE=$1
HOURS_TO_RETAIN_BACKUPS=$2
HOSTNAME=$(hostname)
TODAY=$(date +"%Y_%m_%d_%H")
BACKUP_FILE="couchdb_${BACKUP_TYPE}_${TODAY}.tar.gz"
tar -Pzcf "{{ couch_backup_dir }}/${BACKUP_FILE}" "{{ couch_data_dir }}"


{% if not aws_versioning_enabled%}
UPLOAD_NAME="${BACKUP_FILE}"
{% else %}
UPLOAD_NAME="couchdb_${BACKUP_TYPE}_${HOSTNAME}.tar.gz"
{% endif %}


# Remove old backups of this backup type
find {{ couch_backup_dir }} -mmin "+${HOURS_TO_RETAIN_BACKUPS}" -name "couchdb_${BACKUP_TYPE}_*" -delete

{% if remote_couch_backup %}
rsync -avH --delete --exclude="commcarehq__synclogs.*.couch" {{ couch_backup_dir }}/ {{ remote_couch_backup }}:{{ couch_backup_dir }}
{% endif %}

{% if couch_s3 %}
( cd {{ couch_backup_dir }} && /usr/local/sbin/backup_snapshots.py "${BACKUP_FILE}" "${UPLOAD_NAME}" {{ couchdb_snapshot_bucket }} {{aws_endpoint}} )
{% endif %}
