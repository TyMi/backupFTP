#!/bin/sh
#
# Wrapper for the daily backupFTP run via cron.daily.
#
# Installation (as root/with sudo):
#   1. Adjust BACKUP_USER and BACKUP_DIR below for the target environment.
#   2. Copy this file to /etc/cron.daily/backupFTP (no dots in the name,
#      otherwise run-parts skips the file):
#        sudo cp backupFTP-cron-daily.sh /etc/cron.daily/backupFTP
#        sudo chown root:root /etc/cron.daily/backupFTP
#        sudo chmod 755 /etc/cron.daily/backupFTP
#   3. Check that run-parts picks it up (does NOT execute it):
#        sudo run-parts --test /etc/cron.daily
#
# Started by cron as root; switches here to the given system user so
# backup and config file ownership stays unchanged.
#
# cron itself only provides a minimal environment (no custom variables),
# so password_env/smtp_password_env (as recommended in backupFTP.conf.example)
# won't be set unless loaded here. If ENV_FILE exists, it is sourced before
# the switch to BACKUP_USER; create it as needed, e.g.:
#   sudo install -m 600 -o root -g root /dev/null /etc/backupFTP.env
#   sudo tee -a /etc/backupFTP.env <<'EOF'
#   FTP_PASSWORD_EXAMPLE1=...
#   SMTP_PASSWORD=...
#   EOF

BACKUP_USER=changeme
BACKUP_DIR=/home/changeme
ENV_FILE=/etc/backupFTP.env

if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck source=/dev/null
    . "$ENV_FILE"
    set +a
fi

exec /usr/sbin/runuser -u "$BACKUP_USER" --preserve-environment -- /usr/bin/python3 "$BACKUP_DIR/backupFTP.py" --config "$BACKUP_DIR/backupFTP.conf" >> /var/log/backupFTP-cron.log 2>&1
