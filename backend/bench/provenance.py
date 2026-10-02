"""What a run's agent ran with: its system prompts, and one digest of them and its configs."""

import hashlib
import json
from dataclasses import asdict

from app.agent.protocol import build_system_prompt
from bench.executor import AgentConfigs


def system_prompts(configs: AgentConfigs) -> dict[str, str]:
    """The system prompt each kind of task starts from, tool definitions and all."""
    return {
        kind.value: build_system_prompt(
            config.require_verified_finish, config.allow_escalation, config.extra_rules
        )
        for kind, config in configs.items()
    }


def agent_digest(configs: AgentConfigs) -> str:
    """Changes when a prompt, a tool's definition or any agent setting does, and only then."""
    material = {
        "system_prompts": system_prompts(configs),
        "agent_configs": {kind.value: asdict(config) for kind, config in configs.items()},
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
