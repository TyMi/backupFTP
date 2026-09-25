"""Sanity checks for the test fixtures themselves (conftest.py)."""

from __future__ import annotations

import ftplib

import paramiko


def test_ftp_server_fixture_basic(ftp_server):
    (ftp_server["root"] / "hello.txt").write_bytes(b"HELLO")
    ftp = ftplib.FTP()
    ftp.connect(ftp_server["host"], ftp_server["port"], timeout=5)
    ftp.login(ftp_server["user"], ftp_server["password"])
    entries = list(ftp.mlsd("/"))
    assert entries[0][0] == "hello.txt"
    ftp.quit()


def test_ftp_server_no_mlsd_fixture(ftp_server_no_mlsd):
    (ftp_server_no_mlsd["root"] / "hello.txt").write_bytes(b"HELLO")
    ftp = ftplib.FTP()
    ftp.connect(ftp_server_no_mlsd["host"], ftp_server_no_mlsd["port"], timeout=5)
    ftp.login(ftp_server_no_mlsd["user"], ftp_server_no_mlsd["password"])
    try:
        list(ftp.mlsd("/"))
        raise AssertionError("MLSD should have been rejected")
    except ftplib.error_perm:
        pass
    ftp.quit()


def test_sftp_server_fixture_basic(sftp_server):
    (sftp_server["root"] / "hello.txt").write_bytes(b"HELLO")

    client = paramiko.SSHClient()
    client.load_host_keys(sftp_server["known_hosts_file"])
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(
        sftp_server["host"],
        port=sftp_server["port"],
        username=sftp_server["user"],
        password=sftp_server["password"],
        timeout=5,
    )
    sftp = client.open_sftp()
    entries = sftp.listdir("/")
    assert entries == ["hello.txt"]
    sftp.close()
    client.close()
