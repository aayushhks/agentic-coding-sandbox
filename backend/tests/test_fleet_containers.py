import json
from pathlib import Path

from fleet.containers import (
    DEADLINE_GRACE_SECONDS,
    DOCKER_DEFAULT_SECCOMP,
    NESTED_SANDBOX_SYSCALLS,
    labels,
    proxy_config,
    task_config,
    task_seccomp_profile,
)
from fleet.policy import ExecutionPolicy


def test_the_task_profile_is_dockers_default_plus_unshare() -> None:
    default = json.loads(DOCKER_DEFAULT_SECCOMP.read_text())
    profile = task_seccomp_profile()
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO"
    assert profile["syscalls"][:-1] == default["syscalls"]
    assert profile["syscalls"][-1] == {"names": ["unshare"], "action": "SCMP_ACT_ALLOW"}
    assert NESTED_SANDBOX_SYSCALLS == ("unshare",)
    assert {key: value for key, value in profile.items() if key != "syscalls"} == {
        key: value for key, value in default.items() if key != "syscalls"
    }


def _config(policy: ExecutionPolicy, proxy: str | None = None) -> dict[str, object]:
    return task_config(
        image="fleet-task:local",
        runner="bench.runner:run_job",
        policy=policy,
        input_dir=Path("/work/in"),
        token="0123abcd",
        network=None if proxy is None else "fleet-7-1",
        proxy=proxy,
        labels=labels(job_id=7, attempt=1, worker_id="w0"),
    )


def test_a_task_container_gets_its_limits_and_nothing_more() -> None:
    config = _config(ExecutionPolicy(cpus=0.5, memory_mb=256, pids=64, tmp_mb=100))
    host = config["HostConfig"]
    assert isinstance(host, dict)
    assert (host["NanoCpus"], host["Memory"], host["MemorySwap"], host["PidsLimit"]) == (
        500_000_000,
        256 * 1024 * 1024,
        256 * 1024 * 1024,
        64,
    )
    assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"]
    # the task process itself is pid 1: an init would hold its stdout and its token, dumpable
    assert host["Init"] is False
    assert host["SecurityOpt"][0] == "no-new-privileges"
    assert host["Tmpfs"] == {"/tmp": "rw,nosuid,nodev,exec,size=100m"}
    # the job is the only thing mounted in, read-only; nothing comes back through a mount
    assert host["Binds"] == ["/work/in:/in:ro"]
    assert host["NetworkMode"] == "none"
    assert config["User"] == "10001:10001"
    env = config["Env"]
    assert isinstance(env, list)
    assert "SANDBOX_REQUIRE_ISOLATION=1" in env and "FLEET_ATTEMPT_TOKEN=0123abcd" in env
    assert not any(item.startswith("SANDBOX_HIDDEN_PATHS") for item in env)
    assert not any(item.startswith(("HTTPS_PROXY", "HTTP_PROXY")) for item in env)
    assert config["Labels"] == {
        "fleet.managed": "1",
        "fleet.job": "7",
        "fleet.attempt": "1",
        "fleet.worker": "w0",
    }


def test_the_container_carries_its_own_deadline_behind_the_workers() -> None:
    cmd = _config(ExecutionPolicy(timeout_seconds=90))["Cmd"]
    assert cmd == [
        *("python", "-m", "fleet.task", "--runner", "bench.runner:run_job"),
        *("--deadline-seconds", str(90 + DEADLINE_GRACE_SECONDS)),
    ]


def test_granted_egress_goes_through_a_proxy_on_a_network_of_its_own() -> None:
    config = _config(ExecutionPolicy(egress=("api.groq.com:443",)), proxy="http://egress:3128")
    host = config["HostConfig"]
    assert isinstance(host, dict) and host["NetworkMode"] == "fleet-7-1"
    assert "HTTPS_PROXY=http://egress:3128" in config["Env"]  # type: ignore[operator]
    proxy = proxy_config(
        image="fleet-task:local",
        egress=("api.groq.com:443",),
        network="fleet-7-1",
        labels=labels(job_id=7, attempt=1, worker_id="w0"),
    )
    assert proxy["Cmd"] == [
        *("python", "-m", "fleet.egress", "--port", "3128"),
        *("--allow", "api.groq.com:443"),
    ]
    assert proxy["HostConfig"]["CapDrop"] == ["ALL"]
