#!/usr/bin/env python3
"""
backupFTP - Backup web instances via FTP(S)

Reads a list of FTP backup jobs from a config file, mirrors each
configured FTP server recursively into a local directory, keeps the
last N generations (each with its own log) and notifies the admin by
email about success or failure.
"""

from __future__ import annotations

import argparse
import configparser
import fnmatch
import ftplib
import getpass
import logging
import os
import re
import shutil
import smtplib
import ssl
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

try:
    import paramiko
except ImportError:
    paramiko = None  # only required for protocol = sftp

DEFAULT_CONFIG_PATH = Path(__file__).with_name("backupFTP.conf")
DEFAULT_BASE_DIR = Path("/data/bak")
DEFAULT_ADMIN_MAIL = "admin@example.com"
DEFAULT_SMTP_HOST = "localhost"
DEFAULT_SMTP_PORT = 25
DEFAULT_KEEP_GENERATIONS = 7

VALID_JOB_KEY = re.compile(r"^[a-zA-Z0-9_-]+$")
DEFAULT_NOTIFY_ON_SUCCESS = True
VALID_TLS_MODES = ("required", "preferred", "off")
DEFAULT_TLS_MODE = "required"
VALID_PROTOCOLS = ("ftp", "sftp")
DEFAULT_PROTOCOL = "ftp"
DEFAULT_FTP_PORT = 21
DEFAULT_SFTP_PORT = 22
DEFAULT_RETRIES = 0
DEFAULT_RETRY_BACKOFF = 5.0
DEFAULT_CHECK_DISK_SPACE = True

# ftplib.all_errors is itself a tuple; concatenating (rather than nesting it
# inside another except tuple) keeps the result flat, as required by except.
FTPS_CONNECT_ERRORS: tuple[type[BaseException], ...] = ftplib.all_errors + (ssl.SSLError, OSError)
FTP_TRANSFER_ERRORS: tuple[type[BaseException], ...] = ftplib.all_errors + (OSError,)


class BackupError(Exception):
    """Base class for all errors during a backup run."""


class ConfigError(BackupError):
    pass


class DirectoryError(BackupError):
    pass


class FtpConnectionError(BackupError):
    pass


class DownloadError(BackupError):
    pass


class RotationError(BackupError):
    pass


class MailError(BackupError):
    pass


@dataclass
class JobConfig:
    key: str
    name: str
    sourceserver: str
    ftpuser: str
    password: str | None
    password_env: str | None
    protocol: str
    tls: str
    tls_ca_file: str | None
    ftp_port: int
    sftp_port: int
    ssh_key_file: str | None
    known_hosts_file: str | None
    exclude: list[str]
    retries: int
    retry_backoff: float
    check_disk_space: bool
    base_dir: Path
    keep: int
    keep_daily: int
    keep_weekly: int
    keep_monthly: int
    admin_mail: str
    smtp_host: str
    smtp_port: int
    smtp_user: str | None
    smtp_password: str | None
    smtp_password_env: str | None
    notify_on_success: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backupFTP",
        description="Backup web instances via FTP(S), driven by a config file",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to the config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--job",
        action="append",
        dest="jobs",
        metavar="JOB_KEY",
        help="Only run the given job (section name from the config). "
        "Can be given multiple times. Runs all jobs if omitted.",
    )
    parser.add_argument(
        "--list-jobs",
        action="store_true",
        help="List configured jobs and exit, without running a backup",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Connect, list and estimate the transfer size only; "
        "do not download, write, rotate or send mail",
    )
    return parser.parse_args()


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _validate_tls_mode(section_name: str, value: str) -> str:
    if value not in VALID_TLS_MODES:
        raise ConfigError(
            f"Job '{section_name}': invalid tls mode '{value}', "
            f"must be one of {', '.join(VALID_TLS_MODES)}"
        )
    return value


def _validate_protocol(section_name: str, value: str) -> str:
    if value not in VALID_PROTOCOLS:
        raise ConfigError(
            f"Job '{section_name}': invalid protocol '{value}', "
            f"must be one of {', '.join(VALID_PROTOCOLS)}"
        )
    return value


def _parse_exclude(value: str | None) -> list[str]:
    if not value:
        return []
    return [pattern.strip() for pattern in value.split(",") if pattern.strip()]


def _is_excluded(rel_path: str, exclude: list[str], is_dir: bool) -> bool:
    """Whether rel_path (job-relative, no leading slash) matches an exclude pattern.

    For a directory, also treats it as excluded (skipping the whole subtree,
    without descending into it) if a pattern would match anything inside it
    - e.g. "cache/*" excludes the whole "cache" directory, not just its
    files one by one.
    """
    if any(fnmatch.fnmatch(rel_path, pattern) for pattern in exclude):
        return True
    if is_dir:
        probe = rel_path + "/\x00probe\x00"
        if any(fnmatch.fnmatch(probe, pattern) for pattern in exclude):
            return True
    return False


def _validate_keep(section_name: str, value: int, gfs_active: bool) -> int:
    # keep is unused (GFS retention takes over entirely) once any of
    # keep_daily/keep_weekly/keep_monthly is set, so it isn't validated then.
    if not gfs_active and value < 1:
        raise ConfigError(
            f"Job '{section_name}': invalid keep={value}, must be >= 1 "
            "(keep=0 or negative would delete the backup just created)"
        )
    return value


def _validate_gfs_keep(section_name: str, option_name: str, value: int) -> int:
    if value < 0:
        raise ConfigError(f"Job '{section_name}': invalid {option_name}={value}, must be >= 0")
    return value


def _validate_retries(section_name: str, value: int) -> int:
    if value < 0:
        raise ConfigError(f"Job '{section_name}': invalid retries={value}, must be >= 0")
    return value


def _validate_retry_backoff(section_name: str, value: float) -> float:
    if value <= 0:
        raise ConfigError(f"Job '{section_name}': invalid retry_backoff={value}, must be > 0")
    return value


def _check_config_permissions(config_path: Path, jobs: list[JobConfig]) -> None:
    has_plaintext_password = any(job.password or job.smtp_password for job in jobs)
    if not has_plaintext_password:
        return
    mode = config_path.stat().st_mode
    if mode & 0o077:
        print(
            f"Warning: {config_path} contains a plaintext password and is readable "
            f"by group/other (mode {oct(mode & 0o777)}). Run: chmod 600 {config_path}",
            file=sys.stderr,
        )


def load_config(config_path: Path) -> list[JobConfig]:
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")

    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")

    global_section = parser["global"] if parser.has_section("global") else {}
    global_base_dir = Path(global_section.get("base_dir", str(DEFAULT_BASE_DIR)))
    global_keep_daily = _validate_gfs_keep(
        "global", "keep_daily", int(global_section.get("keep_daily", 0))
    )
    global_keep_weekly = _validate_gfs_keep(
        "global", "keep_weekly", int(global_section.get("keep_weekly", 0))
    )
    global_keep_monthly = _validate_gfs_keep(
        "global", "keep_monthly", int(global_section.get("keep_monthly", 0))
    )
    global_gfs_active = bool(global_keep_daily or global_keep_weekly or global_keep_monthly)
    global_keep = _validate_keep(
        "global", int(global_section.get("keep", DEFAULT_KEEP_GENERATIONS)), global_gfs_active
    )
    global_admin_mail = global_section.get("admin_mail", DEFAULT_ADMIN_MAIL)
    global_smtp_host = global_section.get("smtp_host", DEFAULT_SMTP_HOST)
    global_smtp_port = int(global_section.get("smtp_port", DEFAULT_SMTP_PORT))
    global_smtp_user = global_section.get("smtp_user")
    global_smtp_password = global_section.get("smtp_password")
    global_smtp_password_env = global_section.get("smtp_password_env")
    global_notify_on_success = _parse_bool(
        global_section.get("notify_on_success"), DEFAULT_NOTIFY_ON_SUCCESS
    )
    global_tls = _validate_tls_mode("global", global_section.get("tls", DEFAULT_TLS_MODE))
    global_tls_ca_file = global_section.get("tls_ca_file")
    global_protocol = _validate_protocol("global", global_section.get("protocol", DEFAULT_PROTOCOL))
    global_ftp_port = int(global_section.get("ftp_port", DEFAULT_FTP_PORT))
    global_sftp_port = int(global_section.get("sftp_port", DEFAULT_SFTP_PORT))
    global_ssh_key_file = global_section.get("ssh_key_file")
    global_known_hosts_file = global_section.get("known_hosts_file")
    global_exclude_raw = global_section.get("exclude")
    global_retries = _validate_retries("global", int(global_section.get("retries", DEFAULT_RETRIES)))
    global_retry_backoff = _validate_retry_backoff(
        "global", float(global_section.get("retry_backoff", DEFAULT_RETRY_BACKOFF))
    )
    global_check_disk_space = _parse_bool(
        global_section.get("check_disk_space"), DEFAULT_CHECK_DISK_SPACE
    )

    jobs: list[JobConfig] = []
    for section_name in parser.sections():
        if section_name == "global":
            continue

        if not VALID_JOB_KEY.match(section_name):
            raise ConfigError(
                f"Invalid job section name '{section_name}': "
                "only a-z, A-Z, 0-9, '-' and '_' are allowed (used as directory name)"
            )

        section = parser[section_name]

        try:
            sourceserver = section["sourceserver"]
            ftpuser = section["ftpuser"]
        except KeyError as exc:
            raise ConfigError(f"Job '{section_name}': required field {exc} missing in config") from exc

        keep_daily = _validate_gfs_keep(
            section_name, "keep_daily", int(section.get("keep_daily", global_keep_daily))
        )
        keep_weekly = _validate_gfs_keep(
            section_name, "keep_weekly", int(section.get("keep_weekly", global_keep_weekly))
        )
        keep_monthly = _validate_gfs_keep(
            section_name, "keep_monthly", int(section.get("keep_monthly", global_keep_monthly))
        )
        gfs_active = bool(keep_daily or keep_weekly or keep_monthly)

        jobs.append(
            JobConfig(
                key=section_name,
                name=section.get("name", section_name),
                sourceserver=sourceserver,
                ftpuser=ftpuser,
                password=section.get("password"),
                password_env=section.get("password_env"),
                protocol=_validate_protocol(section_name, section.get("protocol", global_protocol)),
                tls=_validate_tls_mode(section_name, section.get("tls", global_tls)),
                tls_ca_file=section.get("tls_ca_file", global_tls_ca_file),
                ftp_port=int(section.get("ftp_port", global_ftp_port)),
                sftp_port=int(section.get("sftp_port", global_sftp_port)),
                ssh_key_file=section.get("ssh_key_file", global_ssh_key_file),
                known_hosts_file=section.get("known_hosts_file", global_known_hosts_file),
                exclude=_parse_exclude(section.get("exclude", global_exclude_raw)),
                retries=_validate_retries(section_name, int(section.get("retries", global_retries))),
                retry_backoff=_validate_retry_backoff(
                    section_name, float(section.get("retry_backoff", global_retry_backoff))
                ),
                check_disk_space=_parse_bool(
                    section.get("check_disk_space"), global_check_disk_space
                ),
                base_dir=Path(section.get("base_dir", str(global_base_dir))),
                keep=_validate_keep(section_name, int(section.get("keep", global_keep)), gfs_active),
                keep_daily=keep_daily,
                keep_weekly=keep_weekly,
                keep_monthly=keep_monthly,
                admin_mail=section.get("admin_mail", global_admin_mail),
                smtp_host=section.get("smtp_host", global_smtp_host),
                smtp_port=int(section.get("smtp_port", global_smtp_port)),
                smtp_user=section.get("smtp_user", global_smtp_user),
                smtp_password=section.get("smtp_password", global_smtp_password),
                smtp_password_env=section.get("smtp_password_env", global_smtp_password_env),
                notify_on_success=_parse_bool(
                    section.get("notify_on_success"), global_notify_on_success
                ),
            )
        )

    if not jobs:
        raise ConfigError(f"No job sections found in {config_path}")

    _check_config_permissions(config_path, jobs)

    return jobs


def get_ftp_password(job: JobConfig) -> str | None:
    if job.password_env:
        password = os.environ.get(job.password_env)
        if password:
            return password
        raise ConfigError(
            f"Job '{job.name}': environment variable {job.password_env} is not set"
        )
    if job.password:
        return job.password
    if job.protocol == "sftp" and job.ssh_key_file:
        return None
    if not sys.stdin.isatty():
        raise ConfigError(
            f"Job '{job.name}': no password configured (password/password_env) "
            "and no TTY available for an interactive prompt (e.g. running under cron)"
        )
    return getpass.getpass(f"FTP password for '{job.name}' ({job.ftpuser}@{job.sourceserver}): ")


def get_smtp_password(job: JobConfig) -> str | None:
    if job.smtp_password_env:
        password = os.environ.get(job.smtp_password_env)
        if password:
            return password
        raise ConfigError(
            f"Job '{job.name}': environment variable {job.smtp_password_env} is not set"
        )
    if job.smtp_password:
        return job.smtp_password
    if job.smtp_user:
        if not sys.stdin.isatty():
            raise ConfigError(
                f"Job '{job.name}': no SMTP password configured (smtp_password/smtp_password_env) "
                "and no TTY available for an interactive prompt (e.g. running under cron)"
            )
        return getpass.getpass(f"SMTP password for '{job.name}' ({job.smtp_user}@{job.smtp_host}): ")
    return None


def setup_logging(job_name: str, log_path: Path) -> logging.Logger:
    logger = logging.getLogger(f"backupFTP.{job_name}")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter(f"%(asctime)s [%(levelname)s] [{job_name}] %(message)s")

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger


def connect_ftp(
    sourceserver: str,
    port: int,
    ftpuser: str,
    ftppasswd: str,
    tls_mode: str,
    tls_ca_file: str | None,
    logger: logging.Logger,
) -> tuple[ftplib.FTP, bool]:
    """Connects and logs in. Returns (ftp, used_tls)."""

    ftp: ftplib.FTP

    if tls_mode in ("required", "preferred"):
        try:
            ctx = ssl.create_default_context(cafile=tls_ca_file) if tls_ca_file else ssl.create_default_context()
            ftp = ftplib.FTP_TLS(context=ctx, timeout=30)
            ftp.connect(sourceserver, port)
            ftp.login(ftpuser, ftppasswd)
            ftp.prot_p()
            logger.info("Connected via FTPS (TLS, certificate verified) to %s:%d", sourceserver, port)
            return ftp, True
        except FTPS_CONNECT_ERRORS as exc:
            if tls_mode == "required":
                raise FtpConnectionError(
                    f"FTPS (TLS) connection to {sourceserver}:{port} failed and tls=required: {exc}"
                ) from exc
            logger.warning(
                "FTPS not available/supported for %s:%d (%s) - falling back to plain FTP "
                "(tls=preferred, credentials will be sent in plaintext)",
                sourceserver,
                port,
                exc,
            )

    try:
        ftp = ftplib.FTP(timeout=30)
        ftp.connect(sourceserver, port)
        ftp.login(ftpuser, ftppasswd)
        logger.info("Connected via plain FTP to %s:%d", sourceserver, port)
        return ftp, False
    except ftplib.all_errors as exc:
        raise FtpConnectionError(f"Connection to {sourceserver}:{port} failed: {exc}") from exc


def connect_sftp(
    sourceserver: str,
    port: int,
    ftpuser: str,
    ftppasswd: str | None,
    ssh_key_file: str | None,
    known_hosts_file: str | None,
    logger: logging.Logger,
) -> tuple[paramiko.SFTPClient, paramiko.SSHClient]:
    if paramiko is None:
        raise FtpConnectionError(
            "protocol=sftp requires the 'paramiko' package, which is not installed "
            "(pip install paramiko)"
        )

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    if known_hosts_file:
        try:
            client.load_host_keys(known_hosts_file)
        except OSError as exc:
            raise FtpConnectionError(f"Cannot read known_hosts_file {known_hosts_file}: {exc}") from exc
    # Never auto-trust an unknown host key (equivalent to certificate
    # verification for FTPS) - reject instead of silently accepting it.
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    try:
        client.connect(
            sourceserver,
            port=port,
            username=ftpuser,
            password=None if ssh_key_file else ftppasswd,
            key_filename=ssh_key_file,
            timeout=30,
        )
        sftp = client.open_sftp()
        logger.info("Connected via SFTP (host key verified) to %s:%d", sourceserver, port)
        return sftp, client
    except (paramiko.SSHException, OSError) as exc:
        client.close()
        raise FtpConnectionError(f"SFTP connection to {sourceserver}:{port} failed: {exc}") from exc


_UNIX_LIST_LINE_RE = re.compile(
    r"^([\-dlbcps])[\-rwxXsStT]{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\S+\s+\S+\s+\S+\s+(.+)$"
)


def _parse_unix_list_line(line: str) -> tuple[str, dict[str, str]] | None:
    """Parses one line of an old-style Unix LIST reply as a MLSD-like (name, facts) pair.

    Used as a fallback for servers that don't support MLSD. LIST output is
    not standardized; this covers the common vsftpd/ProFTPD/Pure-FTPd
    ls -l style. Symlinks and other special entries are intentionally not
    reported as dir/file (same as an unrecognized MLSD type).
    """
    match = _UNIX_LIST_LINE_RE.match(line.rstrip("\r\n"))
    if not match:
        return None
    type_char, size, name = match.groups()
    if type_char == "l" and " -> " in name:
        name = name.split(" -> ", 1)[0]
    if name in (".", ".."):
        return None
    entry_type = "dir" if type_char == "d" else "file" if type_char == "-" else "other"
    return name, {"type": entry_type, "size": size}


def _list_ftp_directory(
    ftp: ftplib.FTP, remote_dir: str, logger: logging.Logger
) -> list[tuple[str, dict[str, str]]]:
    """Lists remote_dir via MLSD, falling back to parsing LIST if unsupported."""
    try:
        return list(ftp.mlsd(remote_dir))
    except ftplib.error_perm as exc:
        logger.warning(
            "Server does not support MLSD for %s (%s) - falling back to LIST parsing",
            remote_dir,
            exc,
        )
    except ftplib.all_errors as exc:
        raise DownloadError(f"Error listing {remote_dir}: {exc}") from exc

    lines: list[str] = []
    try:
        ftp.retrlines(f"LIST {remote_dir}", lines.append)
    except ftplib.all_errors as exc:
        raise DownloadError(f"Error listing {remote_dir} via LIST: {exc}") from exc

    entries: list[tuple[str, dict[str, str]]] = []
    for line in lines:
        parsed = _parse_unix_list_line(line)
        if parsed is not None:
            entries.append(parsed)
    return entries


def _parse_mlsd_modify(value: str) -> float | None:
    """Parses an MLSD 'modify' fact (YYYYMMDDHHMMSS[.sss], RFC 3659 recommends
    UTC, but not every server complies) into a Unix timestamp."""
    try:
        dt = datetime.strptime(value.split(".", 1)[0], "%Y%m%d%H%M%S")
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _find_latest_generation(job_base_dir: Path) -> Path | None:
    if not job_base_dir.exists():
        return None
    generations = sorted(
        (d for d in job_base_dir.iterdir() if d.is_dir() and not d.name.startswith("tmp_")),
        key=lambda d: d.name,
    )
    return generations[-1] if generations else None


def _try_reuse_from_previous(
    prev_gen_dir: Path | None,
    rel_path: str,
    expected_size: str | int | None,
    mtime: float | None,
    local_path: Path,
) -> int | None:
    """Hardlinks an unchanged file from the previous generation instead of
    downloading it again. Returns its size on success, None if not
    applicable (no previous generation, missing facts, the file differs, or
    linking fails for any reason) - the caller should then download normally.

    "Unchanged" is a heuristic (size + mtime match, like rsync's quick
    check), not a checksum - see the README for the trade-off.
    """
    if prev_gen_dir is None or expected_size is None or mtime is None:
        return None
    prev_file = prev_gen_dir / rel_path
    try:
        prev_stat = prev_file.stat()
    except OSError:
        return None
    if prev_stat.st_size != int(expected_size):
        return None
    if abs(prev_stat.st_mtime - mtime) > 2:  # tolerate whole-second/rounding differences
        return None
    try:
        os.link(prev_file, local_path)
    except OSError:
        return None
    return prev_stat.st_size


def mirror_ftp(
    ftp: ftplib.FTP,
    remote_dir: str,
    local_dir: Path,
    logger: logging.Logger,
    skipped: list[str],
    exclude: list[str],
    stats: dict[str, int],
    prev_gen_dir: Path | None = None,
) -> None:
    local_dir.mkdir(parents=True, exist_ok=True)
    local_dir_resolved = local_dir.resolve()

    entries = _list_ftp_directory(ftp, remote_dir, logger)

    for name, facts in entries:
        if name in (".", ".."):
            continue
        if not name or "/" in name or "\\" in name:
            logger.warning("Suspicious entry from server skipped: %r", name)
            continue

        entry_type = facts.get("type", "file")
        remote_path = f"{remote_dir}/{name}" if remote_dir != "/" else f"/{name}"
        rel_path = remote_path.lstrip("/")
        if _is_excluded(rel_path, exclude, entry_type == "dir"):
            logger.info("Excluded by pattern: %s", remote_path)
            continue

        local_path = (local_dir / name).resolve()
        if not local_path.is_relative_to(local_dir_resolved):
            raise DownloadError(f"Path traversal attempt via server filename: {name!r}")

        if entry_type == "dir":
            logger.debug("Directory: %s", remote_path)
            mirror_ftp(ftp, remote_path, local_path, logger, skipped, exclude, stats, prev_gen_dir)
        elif entry_type == "file":
            logger.debug("File: %s", remote_path)
            expected_size = facts.get("size")
            modify_value = facts.get("modify")
            mtime = _parse_mlsd_modify(modify_value) if modify_value else None
            reused_size = _try_reuse_from_previous(
                prev_gen_dir, rel_path, expected_size, mtime, local_path
            )
            if reused_size is not None:
                logger.debug("Unchanged since last generation, hardlinked: %s", remote_path)
                stats["files"] += 1
                stats["bytes"] += reused_size
                stats["hardlinked"] += 1
                continue
            try:
                with open(local_path, "wb") as fh:
                    ftp.retrbinary(f"RETR {remote_path}", fh.write)
                actual_size = local_path.stat().st_size
                if expected_size is not None and actual_size != int(expected_size):
                    raise OSError(
                        f"size mismatch after download: expected {expected_size} bytes, "
                        f"got {actual_size}"
                    )
                if mtime is not None:
                    os.utime(local_path, (mtime, mtime))
                stats["files"] += 1
                stats["bytes"] += actual_size
            except FTP_TRANSFER_ERRORS as exc:
                logger.warning("Skipping unreadable file %s: %s", remote_path, exc)
                skipped.append(f"{remote_path}: {exc}")
                try:
                    local_path.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            logger.debug("Skipping entry of type %s: %s", entry_type, remote_path)


def mirror_sftp(
    sftp: paramiko.SFTPClient,
    remote_dir: str,
    local_dir: Path,
    logger: logging.Logger,
    skipped: list[str],
    exclude: list[str],
    stats: dict[str, int],
    prev_gen_dir: Path | None = None,
) -> None:
    local_dir.mkdir(parents=True, exist_ok=True)
    local_dir_resolved = local_dir.resolve()

    try:
        entries = sftp.listdir_attr(remote_dir)
    except OSError as exc:
        raise DownloadError(f"Error listing {remote_dir}: {exc}") from exc

    for attr in entries:
        name = attr.filename
        if name in (".", ".."):
            continue
        if not name or "/" in name or "\\" in name:
            logger.warning("Suspicious entry from server skipped: %r", name)
            continue

        remote_path = f"{remote_dir}/{name}" if remote_dir != "/" else f"/{name}"
        rel_path = remote_path.lstrip("/")
        mode = attr.st_mode or 0
        if _is_excluded(rel_path, exclude, stat.S_ISDIR(mode)):
            logger.info("Excluded by pattern: %s", remote_path)
            continue

        local_path = (local_dir / name).resolve()
        if not local_path.is_relative_to(local_dir_resolved):
            raise DownloadError(f"Path traversal attempt via server filename: {name!r}")

        if stat.S_ISDIR(mode):
            logger.debug("Directory: %s", remote_path)
            mirror_sftp(sftp, remote_path, local_path, logger, skipped, exclude, stats, prev_gen_dir)
        elif stat.S_ISREG(mode):
            logger.debug("File: %s", remote_path)
            expected_size = attr.st_size
            mtime = float(attr.st_mtime) if attr.st_mtime is not None else None
            reused_size = _try_reuse_from_previous(
                prev_gen_dir, rel_path, expected_size, mtime, local_path
            )
            if reused_size is not None:
                logger.debug("Unchanged since last generation, hardlinked: %s", remote_path)
                stats["files"] += 1
                stats["bytes"] += reused_size
                stats["hardlinked"] += 1
                continue
            try:
                sftp.get(remote_path, str(local_path))
                actual_size = local_path.stat().st_size
                if expected_size is not None and actual_size != expected_size:
                    raise OSError(
                        f"size mismatch after download: expected {expected_size} bytes, "
                        f"got {actual_size}"
                    )
                if mtime is not None:
                    os.utime(local_path, (mtime, mtime))
                stats["files"] += 1
                stats["bytes"] += actual_size
            except (OSError, paramiko.SSHException) as exc:
                logger.warning("Skipping unreadable file %s: %s", remote_path, exc)
                skipped.append(f"{remote_path}: {exc}")
                try:
                    local_path.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            logger.debug("Skipping entry of type (mode=%o): %s", mode, remote_path)


def dry_run_listing(
    ftp: ftplib.FTP, remote_dir: str, logger: logging.Logger, exclude: list[str]
) -> tuple[int, int, int]:
    """Recursively lists the server like mirror_ftp, but writes nothing locally.

    Returns (file_count, dir_count, total_bytes).
    """
    entries = _list_ftp_directory(ftp, remote_dir, logger)

    file_count = 0
    dir_count = 0
    total_bytes = 0

    for name, facts in entries:
        if name in (".", ".."):
            continue
        if not name or "/" in name or "\\" in name:
            continue

        entry_type = facts.get("type", "file")
        remote_path = f"{remote_dir}/{name}" if remote_dir != "/" else f"/{name}"
        rel_path = remote_path.lstrip("/")
        if _is_excluded(rel_path, exclude, entry_type == "dir"):
            continue

        if entry_type == "dir":
            dir_count += 1
            sub_files, sub_dirs, sub_bytes = dry_run_listing(ftp, remote_path, logger, exclude)
            file_count += sub_files
            dir_count += sub_dirs
            total_bytes += sub_bytes
        elif entry_type == "file":
            file_count += 1
            try:
                total_bytes += int(facts.get("size", 0))
            except ValueError:
                logger.debug("No usable size fact for %s", remote_path)

    return file_count, dir_count, total_bytes


def dry_run_listing_sftp(
    sftp: paramiko.SFTPClient, remote_dir: str, logger: logging.Logger, exclude: list[str]
) -> tuple[int, int, int]:
    """Same as dry_run_listing(), but over an already-connected SFTP client."""
    try:
        entries = sftp.listdir_attr(remote_dir)
    except OSError as exc:
        raise DownloadError(f"Error listing {remote_dir}: {exc}") from exc

    file_count = 0
    dir_count = 0
    total_bytes = 0

    for attr in entries:
        name = attr.filename
        if name in (".", ".."):
            continue
        if not name or "/" in name or "\\" in name:
            continue

        remote_path = f"{remote_dir}/{name}" if remote_dir != "/" else f"/{name}"
        rel_path = remote_path.lstrip("/")
        mode = attr.st_mode or 0
        if _is_excluded(rel_path, exclude, stat.S_ISDIR(mode)):
            continue

        if stat.S_ISDIR(mode):
            dir_count += 1
            sub_files, sub_dirs, sub_bytes = dry_run_listing_sftp(sftp, remote_path, logger, exclude)
            file_count += sub_files
            dir_count += sub_dirs
            total_bytes += sub_bytes
        elif stat.S_ISREG(mode):
            file_count += 1
            total_bytes += attr.st_size or 0

    return file_count, dir_count, total_bytes


def _gfs_bucket_key(generation: Path, period: str) -> tuple[int, int]:
    timestamp = datetime.strptime(generation.name, "%Y%m%d_%H%M%S")
    if period == "week":
        iso_year, iso_week, _ = timestamp.isocalendar()
        return (iso_year, iso_week)
    return (timestamp.year, timestamp.month)


def _select_gfs_survivors(
    generations: list[Path], keep_daily: int, keep_weekly: int, keep_monthly: int
) -> set[Path]:
    """Selects which generations (sorted ascending) to keep under GFS retention.

    Keeps the most recent keep_daily generations outright, plus the most
    recent generation of each of the last keep_weekly ISO weeks and each of
    the last keep_monthly calendar months.
    """
    survivors: set[Path] = set()

    if keep_daily > 0:
        survivors.update(generations[-keep_daily:])

    for period, keep_n in (("week", keep_weekly), ("month", keep_monthly)):
        if keep_n <= 0:
            continue
        latest_per_bucket: dict[tuple[int, int], Path] = {}
        for generation in generations:
            latest_per_bucket[_gfs_bucket_key(generation, period)] = generation
        chosen = sorted(latest_per_bucket.values(), key=lambda d: d.name)[-keep_n:]
        survivors.update(chosen)

    return survivors


def rotate_backups(
    base_dir: Path,
    keep: int,
    logger: logging.Logger,
    keep_daily: int = 0,
    keep_weekly: int = 0,
    keep_monthly: int = 0,
) -> None:
    try:
        for stale in base_dir.glob("tmp_*"):
            logger.warning("Removing stale leftover from a previous failed run: %s", stale)
            if stale.is_dir():
                shutil.rmtree(stale)
            else:
                stale.unlink()

        generations = sorted(
            (d for d in base_dir.iterdir() if d.is_dir() and not d.name.startswith("tmp_")),
            key=lambda d: d.name,
        )

        if keep_daily or keep_weekly or keep_monthly:
            survivors = _select_gfs_survivors(generations, keep_daily, keep_weekly, keep_monthly)
            obsolete = [d for d in generations if d not in survivors]
        else:
            obsolete = generations[:-keep]

        for old_dir in obsolete:
            logger.info("Removing old backup generation %s", old_dir)
            shutil.rmtree(old_dir)
            old_log = base_dir / f"{old_dir.name}.log"
            if old_log.exists():
                old_log.unlink()

        # .failed.log files from failed runs aren't generations and are
        # never covered by the cleanup above - cap how many accumulate.
        failed_logs = sorted(base_dir.glob("*.failed.log"), key=lambda p: p.name)
        for old_failed_log in failed_logs[: -max(keep, 1)]:
            logger.info("Removing old failed-run log %s", old_failed_log)
            old_failed_log.unlink()
    except OSError as exc:
        raise RotationError(f"Error cleaning up old generations in {base_dir}: {exc}") from exc
    except ValueError as exc:
        raise RotationError(f"Error parsing generation timestamp in {base_dir}: {exc}") from exc


def _sanitize_subject(text: str, max_len: int = 200) -> str:
    """Mail headers may not contain line breaks; collapse and truncate."""
    return " ".join(text.split())[:max_len]


def send_mail(
    smtp_host: str,
    smtp_port: int,
    smtp_user: str | None,
    smtp_password: str | None,
    admin_mail: str,
    subject: str,
    body: str,
) -> None:
    msg = EmailMessage()
    msg["From"] = smtp_user or admin_mail
    msg["To"] = admin_mail
    msg["Subject"] = _sanitize_subject(subject)
    msg.set_content(body)

    ssl_context = ssl.create_default_context()
    smtp_ctx: smtplib.SMTP

    try:
        if smtp_port == 465:
            # Implicit TLS (SMTPS), used by many hosting providers on port 465
            smtp_ctx = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30, context=ssl_context)
        else:
            smtp_ctx = smtplib.SMTP(smtp_host, smtp_port, timeout=30)

        with smtp_ctx as smtp:
            if smtp_port != 465:
                try:
                    smtp.starttls(context=ssl_context)
                except smtplib.SMTPNotSupportedError as exc:
                    if smtp_user and smtp_password:
                        raise MailError(
                            f"SMTP server {smtp_host}:{smtp_port} does not support STARTTLS - "
                            "refusing to send login credentials in plaintext"
                        ) from exc
            if smtp_user and smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailError(f"Error sending mail via {smtp_host}:{smtp_port}: {exc}") from exc


def _clear_directory(path: Path) -> None:
    """Removes the contents of path (but not path itself), for a clean retry."""
    if not path.exists():
        return
    for entry in path.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _check_free_disk_space(estimated_bytes: int, tmp_dir: Path, logger: logging.Logger) -> None:
    """Raises DownloadError if estimated_bytes (with a safety margin) exceeds free space.

    Fails the job early instead of mid-transfer.
    """
    free_bytes = shutil.disk_usage(tmp_dir).free
    margin = 1.1
    if estimated_bytes * margin > free_bytes:
        raise DownloadError(
            f"Insufficient disk space: estimated {estimated_bytes} bytes needed "
            f"(x{margin:.1f} safety margin) but only {free_bytes} bytes free in {tmp_dir}"
        )
    logger.debug(
        "Disk space check OK: estimated %d bytes needed, %d bytes free", estimated_bytes, free_bytes
    )


def _connect_and_mirror(
    job: JobConfig,
    ftppasswd: str | None,
    tmp_dir: Path,
    prev_gen_dir: Path | None,
    logger: logging.Logger,
) -> tuple[bool, list[str], dict[str, int]]:
    """Connects via the job's configured protocol and mirrors it into tmp_dir.

    Retries the whole connect+mirror attempt up to job.retries times, with
    exponential backoff (job.retry_backoff * 2**attempt), on a transient
    connection/listing/download error.

    Returns (transport_secure, skipped, stats) - transport_secure is False
    only for a plain-text FTP fallback (tls=preferred); SFTP is always
    host-key verified and therefore always reported as secure.
    """
    attempt = 0
    while True:
        skipped: list[str] = []
        stats = {"files": 0, "bytes": 0, "hardlinked": 0}
        try:
            if job.protocol == "sftp":
                sftp, ssh_client = connect_sftp(
                    job.sourceserver,
                    job.sftp_port,
                    job.ftpuser,
                    ftppasswd,
                    job.ssh_key_file,
                    job.known_hosts_file,
                    logger,
                )
                try:
                    if job.check_disk_space:
                        try:
                            _, _, estimated_bytes = dry_run_listing_sftp(
                                sftp, "/", logger, job.exclude
                            )
                        except DownloadError as exc:
                            logger.debug("Skipping disk space check, listing failed: %s", exc)
                        else:
                            _check_free_disk_space(estimated_bytes, tmp_dir, logger)
                    mirror_sftp(
                        sftp, "/", tmp_dir, logger, skipped, job.exclude, stats, prev_gen_dir
                    )
                finally:
                    sftp.close()
                    ssh_client.close()
                return True, skipped, stats

            # get_ftp_password() only ever returns None for protocol=sftp with
            # a key file, and that path already returned above - a plain FTP
            # job always has a password here.
            assert ftppasswd is not None
            ftp, used_tls = connect_ftp(
                job.sourceserver, job.ftp_port, job.ftpuser, ftppasswd, job.tls, job.tls_ca_file, logger
            )
            try:
                if job.check_disk_space:
                    try:
                        _, _, estimated_bytes = dry_run_listing(ftp, "/", logger, job.exclude)
                    except DownloadError as exc:
                        logger.debug("Skipping disk space check, listing failed: %s", exc)
                    else:
                        _check_free_disk_space(estimated_bytes, tmp_dir, logger)
                mirror_ftp(ftp, "/", tmp_dir, logger, skipped, job.exclude, stats, prev_gen_dir)
            finally:
                try:
                    ftp.quit()
                except ftplib.all_errors:
                    ftp.close()
            return used_tls, skipped, stats

        except (FtpConnectionError, DownloadError) as exc:
            if attempt >= job.retries:
                raise
            delay = job.retry_backoff * (2**attempt)
            attempt += 1
            logger.warning(
                "Transient error on attempt %d/%d, retrying in %.1fs: %s",
                attempt,
                job.retries + 1,
                delay,
                exc,
            )
            time.sleep(delay)
            _clear_directory(tmp_dir)


def run_job(job: JobConfig) -> bool:
    """Runs a single backup job. Returns True on success."""

    job_base_dir = job.base_dir / job.key
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir = job_base_dir / f"tmp_{timestamp}"
    final_dir = job_base_dir / timestamp
    tmp_log_path = job_base_dir / f"tmp_{timestamp}.log"
    final_log_path = job_base_dir / f"{timestamp}.log"
    failed_log_path = job_base_dir / f"{timestamp}.failed.log"

    try:
        job_base_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[{job.name}] Could not create backup directory: {exc}", file=sys.stderr)
        return False

    logger = setup_logging(job.name, tmp_log_path)
    logger.info("Starting backup for '%s' (%s) -> %s", job.name, job.sourceserver, final_dir)

    smtp_password: str | None = None
    current_log_path = tmp_log_path
    start_time = time.monotonic()

    try:
        ftppasswd = get_ftp_password(job)
        smtp_password = get_smtp_password(job)

        prev_gen_dir = _find_latest_generation(job_base_dir)
        used_tls, skipped, stats = _connect_and_mirror(
            job, ftppasswd, tmp_dir, prev_gen_dir, logger
        )
        duration = time.monotonic() - start_time

        try:
            tmp_dir.rename(final_dir)
        except OSError as exc:
            raise RotationError(f"Could not rename {tmp_dir} to {final_dir}: {exc}") from exc

        try:
            tmp_log_path.rename(final_log_path)
            current_log_path = final_log_path
        except OSError as exc:
            raise RotationError(f"Could not rename log {tmp_log_path} to {final_log_path}: {exc}") from exc

        rotate_backups(
            job_base_dir, job.keep, logger, job.keep_daily, job.keep_weekly, job.keep_monthly
        )

        if skipped:
            logger.warning(
                "Backup for '%s' completed as partial success: %d file(s) skipped",
                job.name,
                len(skipped),
            )
        else:
            logger.info("Backup for '%s' completed successfully", job.name)

        if job.notify_on_success:
            notes = []
            if not used_tls:
                notes.append("INSECURE: plain-text FTP, no encryption")
            if skipped:
                notes.append(f"partial success: {len(skipped)} file(s) skipped")
            subject_suffix = f" ({', '.join(notes)})" if notes else ""
            body = (
                f"OK\n\n"
                f"Files: {stats['files']} ({stats['hardlinked']} unchanged, "
                f"reused from the previous generation)\n"
                f"Size: {stats['bytes'] / (1024 * 1024):.1f} MiB\n"
                f"Duration: {duration:.1f} s\n"
                f"TLS/host key verified: {'yes' if used_tls else 'NO'}"
            )
            if skipped:
                body += "\n\nThe following files could not be backed up:\n" + "\n".join(skipped)
            send_mail(
                job.smtp_host,
                job.smtp_port,
                job.smtp_user,
                smtp_password,
                job.admin_mail,
                f"backupFTP for '{job.name}' ({job.sourceserver}) --> {final_dir} SUCCEEDED{subject_suffix}",
                body,
            )
        else:
            logger.info("Success notification suppressed (notify_on_success = false)")
        return True

    except BackupError as exc:
        logger.exception("Backup for '%s' failed: %s", job.name, exc)
        current_log_path = _cleanup_failed_run(tmp_dir, tmp_log_path, failed_log_path, current_log_path, logger)
        _notify_failure(job, current_log_path, exc, smtp_password, logger)
        return False

    except Exception as exc:  # unexpected error - still log and notify cleanly
        logger.exception("Unexpected error for '%s': %s", job.name, exc)
        current_log_path = _cleanup_failed_run(tmp_dir, tmp_log_path, failed_log_path, current_log_path, logger)
        _notify_failure(job, current_log_path, exc, smtp_password, logger)
        return False

    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def run_job_dry_run(job: JobConfig) -> bool:
    """Connects, lists and estimates the transfer size. Writes nothing to disk."""

    logger = logging.getLogger(f"backupFTP.{job.name}")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        logging.Formatter(f"%(asctime)s [%(levelname)s] [{job.name}] %(message)s")
    )
    logger.addHandler(console_handler)

    try:
        ftppasswd = get_ftp_password(job)

        if job.protocol == "sftp":
            sftp, ssh_client = connect_sftp(
                job.sourceserver,
                job.sftp_port,
                job.ftpuser,
                ftppasswd,
                job.ssh_key_file,
                job.known_hosts_file,
                logger,
            )
            try:
                file_count, dir_count, total_bytes = dry_run_listing_sftp(
                    sftp, "/", logger, job.exclude
                )
            finally:
                sftp.close()
                ssh_client.close()
            used_tls = True
        else:
            assert ftppasswd is not None  # only None for protocol=sftp with a key file
            ftp, used_tls = connect_ftp(
                job.sourceserver, job.ftp_port, job.ftpuser, ftppasswd, job.tls, job.tls_ca_file, logger
            )
            try:
                file_count, dir_count, total_bytes = dry_run_listing(ftp, "/", logger, job.exclude)
            finally:
                try:
                    ftp.quit()
                except ftplib.all_errors:
                    ftp.close()

        logger.info(
            "DRY-RUN for '%s' (%s): %d file(s) in %d director(y/ies), %.1f MiB total "
            "(tls=%s) - nothing downloaded",
            job.name,
            job.sourceserver,
            file_count,
            dir_count,
            total_bytes / (1024 * 1024),
            "yes" if used_tls else "NO",
        )
        return True

    except BackupError as exc:
        logger.error("Dry-run for '%s' failed: %s", job.name, exc)
        return False

    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def _cleanup_failed_run(
    tmp_dir: Path,
    tmp_log_path: Path,
    failed_log_path: Path,
    current_log_path: Path,
    logger: logging.Logger,
) -> Path:
    """Removes an incomplete mirror and keeps the log under a .failed.log name."""
    if tmp_dir.exists():
        try:
            shutil.rmtree(tmp_dir)
        except OSError as exc:
            logger.error("Could not remove incomplete backup directory %s: %s", tmp_dir, exc)

    if current_log_path == tmp_log_path and tmp_log_path.exists():
        try:
            tmp_log_path.rename(failed_log_path)
            return failed_log_path
        except OSError as exc:
            logger.error("Could not rename log %s to %s: %s", tmp_log_path, failed_log_path, exc)

    return current_log_path


def _notify_failure(
    job: JobConfig, log_path: Path, exc: Exception, smtp_password: str | None, logger: logging.Logger
) -> None:
    log_content = ""
    try:
        log_content = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass

    try:
        send_mail(
            job.smtp_host,
            job.smtp_port,
            job.smtp_user,
            smtp_password,
            job.admin_mail,
            f"Error during backupFTP for '{job.name}' ({job.sourceserver}): {exc}",
            log_content or str(exc),
        )
    except MailError as mail_exc:
        logger.error("Additionally failed: could not send error mail: %s", mail_exc)


def main() -> int:
    os.umask(0o077)
    args = parse_args()

    try:
        jobs = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    if args.jobs:
        unknown = set(args.jobs) - {job.key for job in jobs}
        if unknown:
            print(f"Unknown job keys: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 1
        jobs = [job for job in jobs if job.key in args.jobs]

    if args.list_jobs:
        for job in jobs:
            print(f"{job.key}\t{job.name}\t{job.sourceserver}")
        return 0

    if args.dry_run:
        results = [run_job_dry_run(job) for job in jobs]
        return 0 if all(results) else 1

    results = [run_job(job) for job in jobs]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
