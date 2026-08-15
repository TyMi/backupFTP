# backupFTP

Python tool for backing up multiple web spaces via FTP(S). Mirrors each
configured FTP server recursively into a local directory, keeps the last
N generations and sends an email notification about the success or
failure of each run.

## Requirements

- Python 3.9 or newer
- No external packages needed (standard library only: `ftplib`,
  `smtplib`, `configparser`, `pathlib`, ...)

## Setup

1. Create the config file from the template:

   ```bash
   cp backupFTP.conf.example backupFTP.conf
   chmod 600 backupFTP.conf
   ```

2. Adjust `backupFTP.conf`: a `[global]` section with the base directory,
   number of generations to keep and SMTP settings, plus one section per
   FTP server to back up. Details and all available options are
   documented as comments in `backupFTP.conf.example`.

3. Preferably pass FTP and/or SMTP passwords via environment variable
   instead of storing them in plain text in the config (see
   `password_env` / `smtp_password_env` in the example config).

## Usage

```bash
# Run all configured jobs
python3 backupFTP.py --config backupFTP.conf

# Run only specific jobs (section name from the config)
python3 backupFTP.py --config backupFTP.conf --job example1

# List configured jobs without running a backup
python3 backupFTP.py --config backupFTP.conf --list-jobs
```

Without `--config`, `backupFTP.conf` is expected next to the script.

## Directory structure

Each job/run produces the following under `<base_dir>/<job_key>/`:

```
<base_dir>/<job_key>/<timestamp>/        mirrored web space content
<base_dir>/<job_key>/<timestamp>.log     log for this run
```

Log and data are intentionally kept separate (the log is **not** inside
the mirrored directory), so that a restore only ever brings back the
plain web space content. Only the last `keep` generations (default: 7),
including their log, are kept automatically; older ones are removed on
every successful run.

## Email notifications

By default, every run sends an email: on success a short "SUCCEEDED"
notice, on failure the collected log plus the error. To only be notified
about failures, set `notify_on_success = false` in `[global]` and/or in a
specific job section (job setting overrides the global default). Failure
emails are always sent regardless of this setting.

## Automated operation (cron.daily)

A ready-made wrapper template is included as `backupFTP-cron-daily.sh`
for a daily automated run. Setup (Linux, the `cron` package must be
installed, e.g. via `sudo apt-get install cron`):

1. In `backupFTP-cron-daily.sh`, adjust the variables `BACKUP_USER` (the
   system user `backupFTP.py` should run as) and `BACKUP_DIR` (directory
   containing `backupFTP.py`/`backupFTP.conf`).

2. Install the file into `/etc/cron.daily/` (no dots in the filename,
   otherwise `run-parts` skips it):

   ```bash
   sudo cp backupFTP-cron-daily.sh /etc/cron.daily/backupFTP
   sudo chown root:root /etc/cron.daily/backupFTP
   sudo chmod 755 /etc/cron.daily/backupFTP
   ```

3. Check that `run-parts` picks it up (does not execute it):

   ```bash
   sudo run-parts --test /etc/cron.daily
   ```

The wrapper is started by `cron` as `root` and internally switches to
`BACKUP_USER` via `runuser`, so backup and config file ownership stays
unchanged. Stdout/stderr is additionally captured in
`/var/log/backupFTP-cron.log`; the actual job logs remain under
`<base_dir>/<job_key>/<timestamp>.log` as usual.

On many Debian/Ubuntu systems without `anacron`, `cron` runs the scripts
in `/etc/cron.daily/` by default at 06:25 system time (see
`/etc/crontab`). Since `backupFTP.py` uses `datetime.now()`, the system
timezone should be set correctly (`timedatectl status`).

## Security

- The FTP connection tries FTPS (TLS) first, falling back to plain FTP
  if unsupported (with a warning logged).
- Passwords should be passed via environment variables (`password_env` /
  `smtp_password_env`) rather than stored in plain text in the config.
- If passwords are stored directly in the config anyway: restrict file
  permissions to `600`.
