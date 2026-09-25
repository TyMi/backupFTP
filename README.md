# backupFTP

![Built with AI](https://img.shields.io/badge/Built_with-AI-success)
[![CI](https://github.com/TyMi/backupFTP/actions/workflows/ci.yml/badge.svg)](https://github.com/TyMi/backupFTP/actions/workflows/ci.yml)

Python tool for backing up multiple web spaces via FTP(S) or SFTP. Mirrors
each configured server recursively into a local directory, keeps the last
N generations and sends an email notification about the success or
failure of each run.

## Requirements

- Python 3.9 or newer
- No external packages needed for FTP(S) jobs (standard library only:
  `ftplib`, `smtplib`, `configparser`, `pathlib`, ...)
- For SFTP jobs (`protocol = sftp`): the `paramiko` package
  (`pip install paramiko`). Only imported/required when at least one job
  actually uses `protocol = sftp`.

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

# Connect, list and estimate the transfer size only - no download,
# no directory/log created, no rotation, no mail sent
python3 backupFTP.py --config backupFTP.conf --dry-run
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

## Incremental backups (hardlinks)

Every run compares each remote file's size and modification time (from
`MLSD`'s `modify` fact, or SFTP's file attributes) against the same file
in the previous generation. If both match, the file is **not**
downloaded again - it is hardlinked from the previous generation instead
(`os.link()`), at zero extra network transfer and zero extra disk space.
Only new or changed files are actually transferred. This happens
automatically whenever a previous generation exists; there is no option
to turn it off, and none is needed to turn it on.

**Important limitation - this is a heuristic, not a checksum:**
size+mtime matching is the same "quick check" `rsync` uses by default,
not a content hash. A file with the exact same size that was rewritten
with different content at the exact same recorded mtime would be
(incorrectly) treated as unchanged and skipped. This is a known,
accepted trade-off for avoiding a full download+hash of every file on
every run - the same trade-off essentially every hardlink-based backup
tool (`rsync --link-dest`, `rsnapshot`, Time Machine) makes. It also
depends on the server reporting `modify`/mtime correctly; RFC 3659
recommends UTC for `MLSD`, but not every FTP server complies, and
servers without `MLSD` support (see "Excluding files" below regarding
the `LIST` fallback) don't provide a comparably reliable timestamp at
all, so incremental reuse effectively does not trigger for those and
every file is downloaded normally.

**Also important:** unchanged files are physically shared (hardlinked)
between generations - the same file may exist under several
`<base_dir>/<job_key>/<timestamp>/` directories while only occupying
disk space once. Treat every generation directory as **read-only**.
Editing a file in place inside an old generation would silently change
it in every other generation sharing that hardlink. Deleting a whole
generation directory (as rotation already does) is always safe - the
underlying data is only actually freed once its last hardlink is
removed, exactly like any other file on the filesystem.

## Retention (flat count or GFS)

By default, `keep` retains a flat number of the most recent generations
(as above). For longer history at the same storage cost, set one or more
of `keep_daily`, `keep_weekly`, `keep_monthly` (global and/or per job)
instead:

```ini
keep_daily = 7
keep_weekly = 4
keep_monthly = 6
```

This switches that job to grandfather-father-son (GFS) retention: the
most recent `keep_daily` generations are kept outright, plus the latest
generation of each of the last `keep_weekly` ISO weeks and each of the
last `keep_monthly` calendar months (a generation matched by more than
one rule still only counts once). `keep` is ignored once any of these
three is set. Leaving all three at their default of `0` keeps the
existing flat `keep` behavior unchanged.

## SFTP jobs

Set `protocol = sftp` on a job (default: `ftp`) to use SFTP instead of
FTP(S). Relevant options (global or per job):

- `sftp_port` (default: `22`)
- `ftpuser` / `password` / `password_env` are reused as the SSH
  username/password.
- `ssh_key_file`: path to a private key for key-based auth instead of a
  password (password fields are ignored if set).
- `known_hosts_file`: path to an `known_hosts`-format file used, in
  addition to the system's own known hosts, to verify the server's host
  key.

The server's host key is **always verified** (against the system's known
hosts and/or `known_hosts_file`) and the connection is refused if it is
unknown or does not match - there is no option to disable this, the same
way FTPS certificate verification cannot be disabled (see Security
below). Populate the known-hosts entry once beforehand, e.g. with
`ssh-keyscan -p <sftp_port> <sourceserver> >> known_hosts_file`
(verify the printed fingerprint out-of-band before trusting it).

## Excluding files

Set `exclude` (global and/or per job) to a comma-separated list of glob
patterns (`fnmatch` syntax), matched against each entry's path relative
to the job root - works the same for FTP(S) and SFTP jobs:

```ini
exclude = cache/*, *.log, wp-content/uploads/cache/*
```

A pattern that matches a directory itself (like `cache/*` matching
everything under `cache`) skips that whole subtree without descending
into it at all - not just its files one by one. `--dry-run` respects
`exclude` too, so its size estimate matches what an actual run would
transfer.

## Retries and disk space checking

Set `retries` (default: `0`) to retry the whole connect+mirror attempt
on a transient connection or transfer error, instead of failing the job
immediately - useful for flaky links or servers that occasionally drop
the connection (e.g. a timeout or a `421` reply). Each retry waits
`retry_backoff` seconds (default: `5`), doubling every attempt
(exponential backoff). A failed attempt's partial download is discarded
before the next try. This does not resume a partially transferred file
or tree - each retry starts the mirror over from scratch.

Before mirroring, a listing pass estimates the total transfer size and
compares it against the free space in `base_dir` (10% safety margin);
if it looks insufficient, the job fails immediately with a clear error
instead of running out of disk space mid-transfer. Set
`check_disk_space = false` to skip this (it costs one extra listing
pass over the whole tree).

## Email notifications

By default, every run sends an email: on success a short "SUCCEEDED"
notice with file count, transferred size, duration and whether the
transport was verified/encrypted; on failure the collected log plus the
error. To only be notified about failures, set `notify_on_success =
false` in `[global]` and/or in a specific job section (job setting
overrides the global default). Failure emails are always sent
regardless of this setting.

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

- FTP connections use FTPS (TLS) with certificate verification by
  default (`tls = required`). Set `tls = preferred` to fall back to
  plain FTP when a server does not support FTPS (logged as a warning
  and marked as insecure in the success mail), or `tls = off` to always
  use plain FTP. For servers with a self-signed/private-CA certificate,
  set `tls_ca_file` to a CA bundle instead of disabling verification.
- SFTP connections (`protocol = sftp`) always verify the server's host
  key and refuse to connect if it is unknown or does not match - this
  cannot be disabled, see "SFTP jobs" above.
- SMTP connections use STARTTLS (or implicit TLS on port 465) with
  certificate verification. If `smtp_user`/`smtp_password` are set but
  the server does not support STARTTLS, sending is aborted rather than
  sending the login in plaintext.
- Passwords should be passed via environment variables (`password_env` /
  `smtp_password_env`) rather than stored in plain text in the config.
- If passwords are stored directly in the config anyway: restrict file
  permissions to `600`. A warning is printed at startup if a plaintext
  password is configured and the config file is readable by group/other.
- Filenames reported by the server are validated before being written
  locally to prevent a compromised/malicious FTP server from writing
  outside the backup directory (path traversal).
- The process umask is set to `077` at startup, so mirrored backups and
  logs are not readable by other local users regardless of the
  system/cron default umask.
- A single unreadable/failing file no longer aborts the whole job: it is
  skipped and listed in the notification mail, and the run is reported
  as a partial success (`SUCCEEDED (Teilerfolg: ...)`).
- Each downloaded file's size is compared against the size the server
  reported while listing it; a mismatch (e.g. a transfer that ended
  early without the client noticing) is treated like any other failed
  file - skipped, removed locally and reported as a partial success,
  rather than silently kept as a truncated backup.

## Known limitations / not currently planned

- **No database dump.** For a web space whose application relies on a
  database (e.g. WordPress, most CMS/shop systems), this tool only
  backs up the FTP-visible files - the database itself is not included
  and needs a separate backup. A per-job DB dump hook (triggered via
  SSH or an HTTP endpoint before the FTP mirror runs) would close this
  gap, but is postponed for now since there is no current use case for
  it.
