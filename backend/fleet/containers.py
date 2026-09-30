"""What a task container gets: its limits, filesystem, network and system calls, from its policy."""

import json
from functools import cache
from pathlib import Path
from typing import Any

from fleet.policy import ExecutionPolicy

# docker's default seccomp profile, fetched from github.com/moby/profiles seccomp/default.json on
# 2026-09-30 (sha256 6416b47770785a41ac59073cdc77d9fe98517df2799dc83ef207e622de3053f6)
DOCKER_DEFAULT_SECCOMP = Path(__file__).with_name("profiles") / "docker-default-seccomp.json"
# what the agent's sandbox needs to give generated code its own namespaces; with no capabilities,
# the container can only use them inside a user namespace the sandbox creates for itself
NESTED_SANDBOX_SYSCALLS = ("unshare", "mount", "umount2")
TASK_UID = 10001
# the in-container deadline trails the worker's timeout, and only matters if the worker is gone
DEADLINE_GRACE_SECONDS = 30
MANAGED = "fleet.managed"


@cache
def task_seccomp_profile() -> dict[str, Any]:
    profile: dict[str, Any] = json.loads(DOCKER_DEFAULT_SECCOMP.read_text())
    profile["syscalls"].append({"names": list(NESTED_SANDBOX_SYSCALLS), "action": "SCMP_ACT_ALLOW"})
    return profile


def labels(*, job_id: int, attempt: int, worker_id: str) -> dict[str, str]:
    """How a worker finds the containers of attempts that no longer hold a lease."""
    return {
        MANAGED: "1",
        "fleet.job": str(job_id),
        "fleet.attempt": str(attempt),
        "fleet.worker": worker_id,
    }


def _locked_down(policy: ExecutionPolicy) -> dict[str, Any]:
    """Host settings every fleet container gets: no privilege, no swap, a read-only root."""
    return {
        "NanoCpus": int(policy.cpus * 1_000_000_000),
        "Memory": policy.memory_mb * 1024 * 1024,
        "MemorySwap": policy.memory_mb * 1024 * 1024,
        "PidsLimit": policy.pids,
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges", f"seccomp={json.dumps(task_seccomp_profile())}"],
        "Init": True,
        "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}},
    }


def task_config(
    *,
    image: str,
    runner: str,
    policy: ExecutionPolicy,
    input_dir: Path,
    output_dir: Path,
    network: str | None,
    proxy: str | None,
    labels: dict[str, str],
) -> dict[str, Any]:
    """The container for one attempt: its job in at /in, its result and progress out at /out."""
    deadline = int(policy.timeout_seconds) + DEADLINE_GRACE_SECONDS
    env = {
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        # generated code must run in its own namespaces, and never see the job's control files
        "SANDBOX_REQUIRE_ISOLATION": "1",
        "SANDBOX_HIDDEN_PATHS": "/in:/out",
    }
    if proxy is not None:
        env |= {
            "HTTPS_PROXY": proxy,
            "HTTP_PROXY": proxy,
            "https_proxy": proxy,
            "http_proxy": proxy,
        }
    host = _locked_down(policy) | {
        # the only writable places: a size-capped scratch space and the output directory
        "Tmpfs": {"/tmp": f"rw,nosuid,nodev,exec,size={policy.tmp_mb}m"},
        "Binds": [f"{input_dir}:/in:ro", f"{output_dir}:/out:rw"],
        "NetworkMode": network or "none",
    }
    return {
        "Image": image,
        "Cmd": [
            *("timeout", "-s", "KILL", str(deadline)),
            *("python", "-m", "fleet.task", "--runner", runner),
        ],
        "User": f"{TASK_UID}:{TASK_UID}",
        "WorkingDir": "/tmp",
        "Env": [f"{key}={value}" for key, value in env.items()],
        "Labels": labels,
        "HostConfig": host,
    }


def proxy_config(
    *, image: str, egress: tuple[str, ...], network: str, labels: dict[str, str]
) -> dict[str, Any]:
    """The egress proxy for one attempt: it forwards exactly the granted destinations."""
    allow = [arg for destination in egress for arg in ("--allow", destination)]
    host = _locked_down(ExecutionPolicy(cpus=0.5, memory_mb=128, pids=64)) | {
        "NetworkMode": network
    }
    return {
        "Image": image,
        "Cmd": ["python", "-m", "fleet.egress", "--port", "3128", *allow],
        "User": f"{TASK_UID}:{TASK_UID}",
        "Labels": labels,
        "HostConfig": host,
    }
