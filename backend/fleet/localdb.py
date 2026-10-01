"""A throwaway local Postgres cluster, for tests and local runs when no server is configured."""

import os
import pwd
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self


class PostgresUnavailableError(RuntimeError):
    """No usable Postgres server binaries on this machine."""


def find_bin_dir() -> Path | None:
    """The directory holding pg_ctl and initdb: on PATH, else the newest Debian-style install."""
    on_path = shutil.which("pg_ctl")
    if on_path is not None:
        return Path(on_path).parent
    installs = sorted(Path("/usr/lib/postgresql").glob("*/bin"), key=lambda p: p.parent.name)
    return installs[-1] if installs and (installs[-1] / "pg_ctl").exists() else None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@dataclass(slots=True)
class LocalPostgres:
    root: Path
    port: int
    bin_dir: Path
    # initdb refuses to run as root, so a root caller runs the server as the postgres user
    run_as: list[str] = field(default_factory=list)

    @classmethod
    def start(cls) -> Self:
        bin_dir = find_bin_dir()
        if bin_dir is None:
            raise PostgresUnavailableError("no pg_ctl on PATH or under /usr/lib/postgresql")
        run_as: list[str] = []
        if os.geteuid() == 0:
            try:
                owner = pwd.getpwnam("postgres")
            except KeyError as exc:
                raise PostgresUnavailableError("running as root with no postgres user") from exc
            run_as = ["runuser", "-u", "postgres", "--"]
            # the system temp dir, since the postgres user can't traverse a private one
            root = Path(tempfile.mkdtemp(prefix="fleet-pg-", dir="/tmp"))
            os.chown(root, owner.pw_uid, owner.pw_gid)
        else:
            root = Path(tempfile.mkdtemp(prefix="fleet-pg-"))
        cluster = cls(root=root, port=_free_port(), bin_dir=bin_dir, run_as=run_as)
        try:
            cluster._run("initdb", "-D", str(root / "data"), "-U", "postgres", "--auth=trust")
            cluster._serve()
        except (OSError, subprocess.CalledProcessError) as exc:
            shutil.rmtree(root, ignore_errors=True)
            raise PostgresUnavailableError(f"could not start a local cluster: {exc}") from exc
        return cluster

    def _serve(self) -> None:
        self._run(
            "pg_ctl",
            "-D",
            str(self.root / "data"),
            "-l",
            str(self.root / "server.log"),
            "-o",
            f"-p {self.port} -k {self.root} -c listen_addresses=127.0.0.1",
            "-w",
            "start",
        )

    def restart(self) -> None:
        """Stop the server the way a crash would, then start it again on the same port and data."""
        # immediate: no checkpoint, so committed work survives only through the write-ahead log
        self._run("pg_ctl", "-D", str(self.root / "data"), "-m", "immediate", "stop")
        self._serve()

    def _run(self, binary: str, *args: str) -> None:
        command = [*self.run_as, str(self.bin_dir / binary), *args]
        subprocess.run(command, check=True, capture_output=True, timeout=120)

    @property
    def url(self) -> str:
        return f"postgresql+asyncpg://postgres@127.0.0.1:{self.port}/postgres"

    @property
    def pid(self) -> int | None:
        """The server's main process, whose children are its sessions; None when it is down."""
        try:
            return int((self.root / "data" / "postmaster.pid").read_text().split()[0])
        except (OSError, IndexError, ValueError):
            return None

    def stop(self) -> None:
        # immediate: the data is thrown away, so there's nothing to flush
        stop = [*self.run_as, str(self.bin_dir / "pg_ctl"), "-D", str(self.root / "data")]
        subprocess.run([*stop, "-m", "immediate", "stop"], check=False, capture_output=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
