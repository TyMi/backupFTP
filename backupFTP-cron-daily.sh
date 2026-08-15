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

BACKUP_USER=changeme
BACKUP_DIR=/home/changeme

exec /usr/sbin/runuser -u "$BACKUP_USER" -- /usr/bin/python3 "$BACKUP_DIR/backupFTP.py" --config "$BACKUP_DIR/backupFTP.conf" >> /var/log/backupFTP-cron.log 2>&1
