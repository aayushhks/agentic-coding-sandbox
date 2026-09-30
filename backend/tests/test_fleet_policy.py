import pytest
from pydantic import ValidationError

from fleet.config import FleetSettings
from fleet.policy import DEFAULT_POLICY, ExecutionPolicy, OperatorLimits, PolicyError


def test_the_default_policy_grants_no_network() -> None:
    assert DEFAULT_POLICY.egress == ()
    assert DEFAULT_POLICY.memory_mb > 0 and DEFAULT_POLICY.timeout_seconds > 0


def test_egress_is_granted_as_host_and_port_only() -> None:
    policy = ExecutionPolicy(egress=("API.groq.com:443", "api.groq.com:443", "10.0.0.5:8080"))
    assert policy.egress == ("10.0.0.5:8080", "api.groq.com:443")
    for bad in ("api.groq.com", "*.groq.com:443", "api.groq.com:0", "http://x:80", "x:99999"):
        with pytest.raises(ValidationError, match="host:port"):
            ExecutionPolicy(egress=(bad,))


def test_a_policy_cannot_carry_unknown_permissions() -> None:
    with pytest.raises(ValidationError):
        ExecutionPolicy.model_validate({"privileged": True})


def test_the_operator_refuses_every_ceiling_a_policy_breaks() -> None:
    limits = OperatorLimits(max_memory_mb=512, grantable_egress=frozenset({"api.groq.com:443"}))
    limits.check(ExecutionPolicy(memory_mb=512, egress=("api.groq.com:443",)))
    with pytest.raises(PolicyError) as refused:
        limits.check(ExecutionPolicy(memory_mb=1024, cpus=8, egress=("example.com:443",)))
    message = str(refused.value)
    assert "memory_mb 1024 is over the limit of 512" in message
    assert "cpus 8.0 is over the limit of 2.0" in message
    assert "egress to example.com:443 is not grantable" in message


def test_the_operator_sets_the_ceilings_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLEET_MAX_MEMORY_MB", "8192")
    monkeypatch.setenv("FLEET_GRANTABLE_EGRESS", "api.groq.com:443, PyPI.org:443")
    limits = FleetSettings().operator_limits()
    assert limits.max_memory_mb == 8192
    assert limits.grantable_egress == frozenset({"api.groq.com:443", "pypi.org:443"})
    assert OperatorLimits().grantable_egress == frozenset()
