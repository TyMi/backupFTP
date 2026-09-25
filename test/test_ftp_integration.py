"""Integration tests for the FTP(S) mirror path against a real local FTP
server (pyftpdlib) - exercises the actual wire protocol (real MLSD facts,
real LIST output, real RETR), not mocks.
"""

from __future__ import annotations

import ftplib
import logging
import time
from unittest import mock

import backupFTP as bftp
from conftest import make_job


def _logger() -> logging.Logger:
    logger = logging.getLogger(f"test.{time.monotonic_ns()}")
    logger.addHandler(logging.NullHandler())
    return logger


def _connect(server) -> ftplib.FTP:
    ftp, used_tls = bftp.connect_ftp(
        server["host"], server["port"], server["user"], server["password"], "off", None, _logger()
    )
    assert used_tls is False
    return ftp


def test_mirror_downloads_nested_tree(ftp_server, tmp_path):
    root = ftp_server["root"]
    (root / "sub").mkdir()
    (root / "a.txt").write_bytes(b"AAAA")
    (root / "sub" / "b.txt").write_bytes(b"BBBBBB")

    local_dir = tmp_path / "out"
    skipped: list[str] = []
    stats = {"files": 0, "bytes": 0, "hardlinked": 0}

    ftp = _connect(ftp_server)
    try:
        bftp.mirror_ftp(ftp, "/", local_dir, _logger(), skipped, [], stats)
    finally:
        ftp.quit()

    assert (local_dir / "a.txt").read_bytes() == b"AAAA"
    assert (local_dir / "sub" / "b.txt").read_bytes() == b"BBBBBB"
    assert stats["files"] == 2
    assert stats["bytes"] == 10
    assert not skipped


def test_exclude_skips_whole_subtree_without_descending(ftp_server, tmp_path):
    root = ftp_server["root"]
    (root / "cache").mkdir()
    (root / "cache" / "x.tmp").write_bytes(b"cached")
    (root / "keep.txt").write_bytes(b"kept")
    (root / "debug.log").write_bytes(b"log line")

    local_dir = tmp_path / "out"
    skipped: list[str] = []
    stats = {"files": 0, "bytes": 0, "hardlinked": 0}

    ftp = _connect(ftp_server)
    try:
        bftp.mirror_ftp(
            ftp, "/", local_dir, _logger(), skipped, ["cache/*", "*.log"], stats
        )
    finally:
        ftp.quit()

    assert [p.name for p in local_dir.iterdir()] == ["keep.txt"]
    assert not (local_dir / "cache").exists()
    assert stats["files"] == 1


def test_partial_success_on_unreadable_file(ftp_server, tmp_path):
    root = ftp_server["root"]
    (root / "good.txt").write_bytes(b"readable")
    secret = root / "secret.txt"
    secret.write_bytes(b"unreadable")
    secret.chmod(0o000)
    try:
        local_dir = tmp_path / "out"
        skipped: list[str] = []
        stats = {"files": 0, "bytes": 0, "hardlinked": 0}

        ftp = _connect(ftp_server)
        try:
            bftp.mirror_ftp(ftp, "/", local_dir, _logger(), skipped, [], stats)
        finally:
            ftp.quit()

        assert (local_dir / "good.txt").read_bytes() == b"readable"
        assert not (local_dir / "secret.txt").exists()
        assert len(skipped) == 1
        assert "secret.txt" in skipped[0]
    finally:
        secret.chmod(0o644)  # restore so tmp_path cleanup can remove it


def test_size_mismatch_is_treated_as_a_failure(ftp_server, tmp_path):
    root = ftp_server["root"]
    (root / "a.txt").write_bytes(b"0123456789")  # server reports size=10 via MLSD

    local_dir = tmp_path / "out"
    skipped: list[str] = []
    stats = {"files": 0, "bytes": 0, "hardlinked": 0}

    ftp = _connect(ftp_server)
    real_retrbinary = ftp.retrbinary

    def truncating_retrbinary(cmd, callback):
        # Simulate a transfer that silently ends early: write fewer bytes
        # than the size MLSD reported, without raising an exception.
        return real_retrbinary(cmd, lambda data: callback(data[:3]))

    try:
        with mock.patch.object(ftp, "retrbinary", side_effect=truncating_retrbinary):
            bftp.mirror_ftp(ftp, "/", local_dir, _logger(), skipped, [], stats)
    finally:
        ftp.quit()

    assert not (local_dir / "a.txt").exists()
    assert len(skipped) == 1
    assert "size mismatch" in skipped[0]
    assert stats["files"] == 0


def test_list_fallback_mirrors_correctly_without_mlsd(ftp_server_no_mlsd, tmp_path):
    root = ftp_server_no_mlsd["root"]
    (root / "sub").mkdir()
    (root / "a.txt").write_bytes(b"hello")
    (root / "sub" / "b.txt").write_bytes(b"world!")

    local_dir = tmp_path / "out"
    skipped: list[str] = []
    stats = {"files": 0, "bytes": 0, "hardlinked": 0}

    ftp = _connect(ftp_server_no_mlsd)
    try:
        bftp.mirror_ftp(ftp, "/", local_dir, _logger(), skipped, [], stats)
    finally:
        ftp.quit()

    assert (local_dir / "a.txt").read_bytes() == b"hello"
    assert (local_dir / "sub" / "b.txt").read_bytes() == b"world!"
    assert stats["files"] == 2
    assert not skipped


def test_incremental_hardlink_across_two_real_mirrors(ftp_server, tmp_path):
    root = ftp_server["root"]
    (root / "unchanged.txt").write_bytes(b"same content")
    (root / "changed.txt").write_bytes(b"before")

    gen1 = tmp_path / "gen1"
    stats1 = {"files": 0, "bytes": 0, "hardlinked": 0}
    ftp = _connect(ftp_server)
    try:
        bftp.mirror_ftp(ftp, "/", gen1, _logger(), [], [], stats1)
    finally:
        ftp.quit()
    assert stats1["hardlinked"] == 0

    # The server's mtime resolution for MLSD is whole seconds; make sure the
    # rewritten file gets a strictly later mtime than the unchanged one.
    time.sleep(1.1)
    (root / "changed.txt").write_bytes(b"after-with-different-length")

    gen2 = tmp_path / "gen2"
    stats2 = {"files": 0, "bytes": 0, "hardlinked": 0}
    ftp = _connect(ftp_server)
    try:
        bftp.mirror_ftp(ftp, "/", gen2, _logger(), [], [], stats2, prev_gen_dir=gen1)
    finally:
        ftp.quit()

    assert stats2["hardlinked"] == 1
    assert (gen2 / "unchanged.txt").stat().st_ino == (gen1 / "unchanged.txt").stat().st_ino
    assert (gen2 / "changed.txt").read_bytes() == b"after-with-different-length"
    assert (gen2 / "changed.txt").stat().st_ino != (gen1 / "changed.txt").stat().st_ino


def test_run_job_end_to_end_against_real_server(ftp_server, tmp_path):
    root = ftp_server["root"]
    (root / "index.html").write_bytes(b"<html></html>")

    base_dir = tmp_path / "backups"
    job = make_job(
        base_dir=base_dir,
        sourceserver=ftp_server["host"],
        ftp_port=ftp_server["port"],
        password=ftp_server["password"],
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
    assert "Files: 1" in captured_mail["body"]
