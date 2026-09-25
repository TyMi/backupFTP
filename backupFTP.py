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
import ftplib
import getpass
import logging
import os
import re
import shutil
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

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
    tls: str
    tls_ca_file: str | None
    base_dir: Path
    keep: int
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


def _validate_keep(section_name: str, value: int) -> int:
    if value < 1:
        raise ConfigError(
            f"Job '{section_name}': invalid keep={value}, must be >= 1 "
            "(keep=0 or negative would delete the backup just created)"
        )
    return value


def load_config(config_path: Path) -> list[JobConfig]:
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")

    parser = configparser.ConfigParser()
    parser.read(config_path, encoding="utf-8")

    global_section = parser["global"] if parser.has_section("global") else {}
    global_base_dir = Path(global_section.get("base_dir", str(DEFAULT_BASE_DIR)))
    global_keep = _validate_keep("global", int(global_section.get("keep", DEFAULT_KEEP_GENERATIONS)))
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

        jobs.append(
            JobConfig(
                key=section_name,
                name=section.get("name", section_name),
                sourceserver=sourceserver,
                ftpuser=ftpuser,
                password=section.get("password"),
                password_env=section.get("password_env"),
                tls=_validate_tls_mode(section_name, section.get("tls", global_tls)),
                tls_ca_file=section.get("tls_ca_file", global_tls_ca_file),
                base_dir=Path(section.get("base_dir", str(global_base_dir))),
                keep=_validate_keep(section_name, int(section.get("keep", global_keep))),
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

    return jobs


def get_ftp_password(job: JobConfig) -> str:
    if job.password_env:
        password = os.environ.get(job.password_env)
        if password:
            return password
        raise ConfigError(
            f"Job '{job.name}': environment variable {job.password_env} is not set"
        )
    if job.password:
        return job.password
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
    ftpuser: str,
    ftppasswd: str,
    tls_mode: str,
    tls_ca_file: str | None,
    logger: logging.Logger,
) -> tuple[ftplib.FTP, bool]:
    """Connects and logs in. Returns (ftp, used_tls)."""

    if tls_mode in ("required", "preferred"):
        try:
            ctx = ssl.create_default_context(cafile=tls_ca_file) if tls_ca_file else ssl.create_default_context()
            ftp = ftplib.FTP_TLS(context=ctx, timeout=30)
            ftp.connect(sourceserver)
            ftp.login(ftpuser, ftppasswd)
            ftp.prot_p()
            logger.info("Connected via FTPS (TLS, certificate verified) to %s", sourceserver)
            return ftp, True
        except (ftplib.all_errors, ssl.SSLError, OSError) as exc:
            if tls_mode == "required":
                raise FtpConnectionError(
                    f"FTPS (TLS) connection to {sourceserver} failed and tls=required: {exc}"
                ) from exc
            logger.warning(
                "FTPS not available/supported for %s (%s) - falling back to plain FTP "
                "(tls=preferred, credentials will be sent in plaintext)",
                sourceserver,
                exc,
            )

    try:
        ftp = ftplib.FTP(timeout=30)
        ftp.connect(sourceserver)
        ftp.login(ftpuser, ftppasswd)
        logger.info("Connected via plain FTP to %s", sourceserver)
        return ftp, False
    except ftplib.all_errors as exc:
        raise FtpConnectionError(f"Connection to {sourceserver} failed: {exc}") from exc


def mirror_ftp(ftp: ftplib.FTP, remote_dir: str, local_dir: Path, logger: logging.Logger) -> None:
    local_dir.mkdir(parents=True, exist_ok=True)
    local_dir_resolved = local_dir.resolve()

    try:
        ftp.cwd(remote_dir)
    except ftplib.all_errors as exc:
        raise DownloadError(f"Cannot change to remote directory {remote_dir}: {exc}") from exc

    try:
        entries = list(ftp.mlsd())
    except ftplib.error_perm as exc:
        raise DownloadError(
            f"Server does not support MLSD, cannot mirror {remote_dir}: {exc}"
        ) from exc
    except ftplib.all_errors as exc:
        raise DownloadError(f"Error listing {remote_dir}: {exc}") from exc

    for name, facts in entries:
        if name in (".", ".."):
            continue
        if not name or "/" in name or "\\" in name:
            logger.warning("Suspicious entry from server skipped: %r", name)
            continue

        entry_type = facts.get("type", "file")
        remote_path = f"{remote_dir}/{name}" if remote_dir != "/" else f"/{name}"
        local_path = (local_dir / name).resolve()
        if not local_path.is_relative_to(local_dir_resolved):
            raise DownloadError(f"Path traversal attempt via server filename: {name!r}")

        if entry_type == "dir":
            logger.debug("Directory: %s", remote_path)
            mirror_ftp(ftp, remote_path, local_path, logger)
            ftp.cwd(remote_dir)
        elif entry_type == "file":
            logger.debug("File: %s", remote_path)
            try:
                with open(local_path, "wb") as fh:
                    ftp.retrbinary(f"RETR {name}", fh.write)
            except (ftplib.all_errors, OSError) as exc:
                raise DownloadError(f"Error downloading {remote_path}: {exc}") from exc
        else:
            logger.debug("Skipping entry of type %s: %s", entry_type, remote_path)


def rotate_backups(base_dir: Path, keep: int, logger: logging.Logger) -> None:
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
        obsolete = generations[:-keep]
        for old_dir in obsolete:
            logger.info("Removing old backup generation %s", old_dir)
            shutil.rmtree(old_dir)
            old_log = base_dir / f"{old_dir.name}.log"
            if old_log.exists():
                old_log.unlink()
    except OSError as exc:
        raise RotationError(f"Error cleaning up old generations in {base_dir}: {exc}") from exc


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
                except smtplib.SMTPNotSupportedError:
                    if smtp_user and smtp_password:
                        raise MailError(
                            f"SMTP server {smtp_host}:{smtp_port} does not support STARTTLS - "
                            "refusing to send login credentials in plaintext"
                        )
            if smtp_user and smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailError(f"Error sending mail via {smtp_host}:{smtp_port}: {exc}") from exc


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

    try:
        ftppasswd = get_ftp_password(job)
        smtp_password = get_smtp_password(job)

        ftp, used_tls = connect_ftp(
            job.sourceserver, job.ftpuser, ftppasswd, job.tls, job.tls_ca_file, logger
        )
        try:
            mirror_ftp(ftp, "/", tmp_dir, logger)
        finally:
            try:
                ftp.quit()
            except ftplib.all_errors:
                ftp.close()

        try:
            tmp_dir.rename(final_dir)
        except OSError as exc:
            raise RotationError(f"Could not rename {tmp_dir} to {final_dir}: {exc}") from exc

        try:
            tmp_log_path.rename(final_log_path)
            current_log_path = final_log_path
        except OSError as exc:
            raise RotationError(f"Could not rename log {tmp_log_path} to {final_log_path}: {exc}") from exc

        rotate_backups(job_base_dir, job.keep, logger)

        logger.info("Backup for '%s' completed successfully", job.name)
        if job.notify_on_success:
            subject_suffix = "" if used_tls else " (UNSICHER: Klartext-FTP, keine Verschluesselung)"
            send_mail(
                job.smtp_host,
                job.smtp_port,
                job.smtp_user,
                smtp_password,
                job.admin_mail,
                f"backupFTP for '{job.name}' ({job.sourceserver}) --> {final_dir} SUCCEEDED{subject_suffix}",
                "OK",
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
    args = parse_args()

    try:
        jobs = load_config(args.config)
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1

    if args.list_jobs:
        for job in jobs:
            print(f"{job.key}\t{job.name}\t{job.sourceserver}")
        return 0

    if args.jobs:
        unknown = set(args.jobs) - {job.key for job in jobs}
        if unknown:
            print(f"Unknown job keys: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 1
        jobs = [job for job in jobs if job.key in args.jobs]

    results = [run_job(job) for job in jobs]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
