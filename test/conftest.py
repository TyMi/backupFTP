"""Pytest fixtures for backupFTP integration tests.

Spins up real local FTP (pyftpdlib) and SFTP (paramiko) servers on
ephemeral loopback ports, so tests exercise the actual wire protocol
instead of mocks. Test-only dependencies (not needed to run backupFTP
itself): pytest, pyftpdlib, paramiko.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
from pathlib import Path

import paramiko
import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backupFTP as bftp  # noqa: E402

logging.getLogger("pyftpdlib").setLevel(logging.WARNING)


def _make_ftp_server(root: Path, *, mlsd: bool = True):
    authorizer = DummyAuthorizer()
    authorizer.add_user("testuser", "testpass", str(root), perm="elradfmw")

    handler = type("_TestFTPHandler", (FTPHandler,), {})
    handler.authorizer = authorizer
    if not mlsd:
        handler.proto_cmds = dict(FTPHandler.proto_cmds)
        del handler.proto_cmds["MLSD"]
        del handler.proto_cmds["MLST"]

    server = FTPServer(("127.0.0.1", 0), handler)
    host, port = server.address
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"handle_exit": False, "timeout": 0.1}, daemon=True
    )
    thread.start()
    return server, thread, host, port


@pytest.fixture
def ftp_server(tmp_path):
    """A real local FTP server with MLSD support, serving tmp_path/ftproot."""
    root = tmp_path / "ftproot"
    root.mkdir()
    server, thread, host, port = _make_ftp_server(root)
    try:
        yield {"host": host, "port": port, "root": root, "user": "testuser", "password": "testpass"}
    finally:
        server.close_all()
        thread.join(timeout=5)


@pytest.fixture
def ftp_server_no_mlsd(tmp_path):
    """Same as ftp_server, but MLSD disabled - forces the LIST fallback (O5)."""
    root = tmp_path / "ftproot"
    root.mkdir()
    server, thread, host, port = _make_ftp_server(root, mlsd=False)
    try:
        yield {"host": host, "port": port, "root": root, "user": "testuser", "password": "testpass"}
    finally:
        server.close_all()
        thread.join(timeout=5)


class _SFTPServerInterface(paramiko.SFTPServerInterface):
    ROOT = None  # set per-test via a fresh subclass

    def _realpath(self, path):
        return self.ROOT.rstrip("/") + path

    def list_folder(self, path):
        real = self._realpath(path)
        try:
            entries = []
            for fname in os.listdir(real):
                attr = paramiko.SFTPAttributes.from_stat(os.lstat(os.path.join(real, fname)))
                attr.filename = fname
                entries.append(attr)
            return entries
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def stat(self, path):
        real = self._realpath(path)
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(real))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def open(self, path, flags, attr):
        real = self._realpath(path)
        is_write = bool(flags & (os.O_WRONLY | os.O_RDWR))
        try:
            fd = os.open(real, flags, 0o666) if flags & os.O_CREAT else os.open(real, flags)
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)
        f = os.fdopen(fd, "wb" if is_write else "rb")
        handle = paramiko.SFTPHandle(flags)
        handle.readfile = f
        handle.writefile = f
        return handle

    def canonicalize(self, path):
        return path


class _SSHServer(paramiko.ServerInterface):
    def __init__(self, password, pubkey):
        self.password = password
        self.pubkey = pubkey

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_auth_password(self, username, password):
        if self.password is not None and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        if self.pubkey is not None and key.get_base64() == self.pubkey.get_base64():
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_channel_subsystem_request(self, channel, name):
        if name == "sftp":
            server = paramiko.SFTPServer(channel, name, server=self, sftp_si=self._sftp_si)
            server.start()
            return True
        return False


def _handle_one_sftp_connection(conn, host_key, password, pubkey, root, sftp_si):
    transport = paramiko.Transport(conn)
    transport.add_server_key(host_key)
    sftp_si.ROOT = root
    server = _SSHServer(password, pubkey)
    server._sftp_si = sftp_si
    transport.start_server(server=server)
    channel = transport.accept(20)
    if channel is None:
        return
    while transport.is_active():
        threading.Event().wait(0.05)


def _serve_sftp_connections(sock, host_key, password, pubkey, root, sftp_si):
    """Accepts connections until sock is closed, handling each on its own
    thread so a new connection is never stuck behind a slow-to-close
    previous one (tests may need several sequential connections, e.g.
    across two backup runs).
    """
    sock.settimeout(0.2)
    while True:
        try:
            conn, _addr = sock.accept()
        except TimeoutError:
            continue  # socket.timeout is an OSError subclass - must be checked first
        except OSError:
            return  # socket was closed by the fixture teardown
        threading.Thread(
            target=_handle_one_sftp_connection,
            args=(conn, host_key, password, pubkey, root, sftp_si),
            daemon=True,
        ).start()


@pytest.fixture(scope="session")
def _sftp_host_key():
    # Generating a 2048-bit RSA key is the slow part of setting up an SFTP
    # server; the identity itself doesn't need to be unique per test.
    return paramiko.RSAKey.generate(2048)


@pytest.fixture(scope="session")
def _sftp_client_key():
    return paramiko.RSAKey.generate(2048)


@pytest.fixture
def sftp_server(tmp_path, _sftp_host_key, _sftp_client_key):
    """A real local SFTP server (paramiko), serving tmp_path/sftproot.

    Accepts either the fixed password or the fixed client key (both are
    always registered, so a test can pick whichever it wants to exercise).
    """
    root = tmp_path / "sftproot"
    root.mkdir()

    host_key = _sftp_host_key

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    host, port = sock.getsockname()

    # Each test connection needs its own SFTPServerInterface subclass since
    # ROOT is a class attribute (paramiko instantiates sftp_si internally).
    sftp_si = type("_SI", (_SFTPServerInterface,), {})

    thread = threading.Thread(
        target=_serve_sftp_connections,
        args=(sock, host_key, "testpass", _sftp_client_key, str(root), sftp_si),
        daemon=True,
    )
    thread.start()

    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(f"[127.0.0.1]:{port} {host_key.get_name()} {host_key.get_base64()}\n")

    client_key_file = tmp_path / "client_key"
    _sftp_client_key.write_private_key_file(str(client_key_file))

    try:
        yield {
            "host": host,
            "port": port,
            "root": root,
            "user": "testuser",
            "password": "testpass",
            "ssh_key_file": str(client_key_file),
            "known_hosts_file": str(known_hosts),
            "host_key": host_key,
        }
    finally:
        sock.close()
        thread.join(timeout=5)


def make_job(**overrides) -> bftp.JobConfig:
    """Builds a JobConfig with sane test defaults, overridden by kwargs."""
    defaults = dict(
        key="job1",
        name="job1",
        sourceserver="127.0.0.1",
        ftpuser="testuser",
        password="testpass",
        password_env=None,
        protocol="ftp",
        tls="off",
        tls_ca_file=None,
        ftp_port=21,
        sftp_port=22,
        ssh_key_file=None,
        known_hosts_file=None,
        exclude=[],
        retries=0,
        retry_backoff=5.0,
        check_disk_space=False,
        base_dir=Path("/tmp"),
        keep=7,
        keep_daily=0,
        keep_weekly=0,
        keep_monthly=0,
        admin_mail="admin@example.com",
        smtp_host="localhost",
        smtp_port=25,
        smtp_user=None,
        smtp_password=None,
        smtp_password_env=None,
        notify_on_success=False,
    )
    defaults.update(overrides)
    return bftp.JobConfig(**defaults)


@pytest.fixture
def job_factory():
    return make_job
