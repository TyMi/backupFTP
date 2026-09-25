"""Integration tests for the SFTP path against a real local SFTP server
(paramiko) - in particular the host-key verification behaviour, since
that's the core security property of protocol = sftp (F1/S1 analogue).
"""

from __future__ import annotations

import logging
import time
from unittest import mock

import paramiko
import pytest

import backupFTP as bftp
from conftest import make_job


def _logger() -> logging.Logger:
    logger = logging.getLogger(f"test.{time.monotonic_ns()}")
    logger.addHandler(logging.NullHandler())
    return logger


def test_mirror_with_password_auth(sftp_server, tmp_path):
    root = sftp_server["root"]
    (root / "sub").mkdir()
    (root / "a.txt").write_bytes(b"AAAA")
    (root / "sub" / "b.txt").write_bytes(b"BBBBBB")

    sftp, ssh_client = bftp.connect_sftp(
        sftp_server["host"],
        sftp_server["port"],
        sftp_server["user"],
        sftp_server["password"],
        None,
        sftp_server["known_hosts_file"],
        _logger(),
    )
    local_dir = tmp_path / "out"
    skipped: list[str] = []
    stats = {"files": 0, "bytes": 0, "hardlinked": 0}
    try:
        bftp.mirror_sftp(sftp, "/", local_dir, _logger(), skipped, [], stats)
    finally:
        sftp.close()
        ssh_client.close()

    assert (local_dir / "a.txt").read_bytes() == b"AAAA"
    assert (local_dir / "sub" / "b.txt").read_bytes() == b"BBBBBB"
    assert stats["files"] == 2
    assert not skipped


def test_mirror_with_key_auth(sftp_server, tmp_path):
    root = sftp_server["root"]
    (root / "a.txt").write_bytes(b"KEYED")

    sftp, ssh_client = bftp.connect_sftp(
        sftp_server["host"],
        sftp_server["port"],
        sftp_server["user"],
        None,
        sftp_server["ssh_key_file"],
        sftp_server["known_hosts_file"],
        _logger(),
    )
    local_dir = tmp_path / "out"
    skipped: list[str] = []
    stats = {"files": 0, "bytes": 0, "hardlinked": 0}
    try:
        bftp.mirror_sftp(sftp, "/", local_dir, _logger(), skipped, [], stats)
    finally:
        sftp.close()
        ssh_client.close()

    assert (local_dir / "a.txt").read_bytes() == b"KEYED"


def test_host_key_mismatch_is_rejected(sftp_server, tmp_path):
    """The core security property: an unknown/wrong host key must never be
    silently trusted (no MITM), the same guarantee FTPS cert verification
    gives on the FTP side."""
    wrong_key = paramiko.RSAKey.generate(2048)
    wrong_known_hosts = tmp_path / "wrong_known_hosts"
    wrong_known_hosts.write_text(
        f"[127.0.0.1]:{sftp_server['port']} {wrong_key.get_name()} {wrong_key.get_base64()}\n"
    )

    with pytest.raises(bftp.FtpConnectionError, match="does not match|Host key"):
        bftp.connect_sftp(
            sftp_server["host"],
            sftp_server["port"],
            sftp_server["user"],
            sftp_server["password"],
            None,
            str(wrong_known_hosts),
            _logger(),
        )


def test_host_key_unknown_is_rejected(sftp_server, tmp_path):
    """No known_hosts entry at all (not even a wrong one) must also be rejected,
    not treated as trust-on-first-use."""
    empty_known_hosts = tmp_path / "empty_known_hosts"
    empty_known_hosts.write_text("")

    with pytest.raises(bftp.FtpConnectionError):
        bftp.connect_sftp(
            sftp_server["host"],
            sftp_server["port"],
            sftp_server["user"],
            sftp_server["password"],
            None,
            str(empty_known_hosts),
            _logger(),
        )


def test_exclude_and_partial_success(sftp_server, tmp_path):
    root = sftp_server["root"]
    (root / "cache").mkdir()
    (root / "cache" / "x.tmp").write_bytes(b"cached")
    (root / "keep.txt").write_bytes(b"kept")
    secret = root / "secret.txt"
    secret.write_bytes(b"unreadable")
    secret.chmod(0o000)
    try:
        sftp, ssh_client = bftp.connect_sftp(
            sftp_server["host"],
            sftp_server["port"],
            sftp_server["user"],
            sftp_server["password"],
            None,
            sftp_server["known_hosts_file"],
            _logger(),
        )
        local_dir = tmp_path / "out"
        skipped: list[str] = []
        stats = {"files": 0, "bytes": 0, "hardlinked": 0}
        try:
            bftp.mirror_sftp(sftp, "/", local_dir, _logger(), skipped, ["cache/*"], stats)
        finally:
            sftp.close()
            ssh_client.close()

        assert (local_dir / "keep.txt").exists()
        assert not (local_dir / "cache").exists()
        assert not (local_dir / "secret.txt").exists()
        assert len(skipped) == 1
    finally:
        secret.chmod(0o644)


def test_incremental_hardlink_across_two_real_mirrors(sftp_server, tmp_path):
    root = sftp_server["root"]
    (root / "unchanged.bin").write_bytes(b"same content")
    (root / "changed.bin").write_bytes(b"before")

    def connect():
        return bftp.connect_sftp(
            sftp_server["host"],
            sftp_server["port"],
            sftp_server["user"],
            sftp_server["password"],
            None,
            sftp_server["known_hosts_file"],
            _logger(),
        )

    gen1 = tmp_path / "gen1"
    stats1 = {"files": 0, "bytes": 0, "hardlinked": 0}
    sftp, ssh_client = connect()
    try:
        bftp.mirror_sftp(sftp, "/", gen1, _logger(), [], [], stats1)
    finally:
        sftp.close()
        ssh_client.close()
    assert stats1["hardlinked"] == 0

    time.sleep(1.1)
    (root / "changed.bin").write_bytes(b"after-with-different-length")

    gen2 = tmp_path / "gen2"
    stats2 = {"files": 0, "bytes": 0, "hardlinked": 0}
    sftp, ssh_client = connect()
    try:
        bftp.mirror_sftp(sftp, "/", gen2, _logger(), [], [], stats2, prev_gen_dir=gen1)
    finally:
        sftp.close()
        ssh_client.close()

    assert stats2["hardlinked"] == 1
    assert (gen2 / "unchanged.bin").stat().st_ino == (gen1 / "unchanged.bin").stat().st_ino
    assert (gen2 / "changed.bin").read_bytes() == b"after-with-different-length"


def test_run_job_end_to_end_against_real_server(sftp_server, tmp_path):
    root = sftp_server["root"]
    (root / "index.html").write_bytes(b"<html></html>")

    base_dir = tmp_path / "backups"
    job = make_job(
        base_dir=base_dir,
        protocol="sftp",
        sourceserver=sftp_server["host"],
        sftp_port=sftp_server["port"],
        password=sftp_server["password"],
        known_hosts_file=sftp_server["known_hosts_file"],
        notify_on_success=True,
    )

    captured_mail: dict[str, str] = {}

    def fake_send_mail(smtp_host, smtp_port, smtp_user, smtp_password, admin_mail, subject, body):
        captured_mail["subject"] = subject
        captured_mail["body"] = body

    with mock.patch.object(bftp, "send_mail", side_effect=fake_send_mail):
        result = bftp.run_job(job)

    assert result is True
    generations = sorted(p for p in (base_dir / "job1").iterdir() if p.is_dir())
    assert len(generations) == 1
    assert (generations[0] / "index.html").read_bytes() == b"<html></html>"
    assert "SUCCEEDED" in captured_mail["subject"]
