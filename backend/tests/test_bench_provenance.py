from dataclasses import replace

from bench.executor import AGENT_CONFIGS, agent_configs
from bench.provenance import agent_digest, system_prompts
from bench.taskset import TaskKind

RULE = "Keep the thought field to one short sentence."


def test_the_digest_is_stable_and_moves_with_the_prompt_or_any_setting() -> None:
    assert agent_digest(AGENT_CONFIGS) == agent_digest(agent_configs())
    with_rule = agent_digest(agent_configs([RULE]))
    assert with_rule != agent_digest(AGENT_CONFIGS)
    assert with_rule == agent_digest(agent_configs([RULE]))
    # a setting that never reaches the prompt still changes what the agent ran with
    hotter = {kind: replace(config, temperature=0.5) for kind, config in AGENT_CONFIGS.items()}
    assert agent_digest(hotter) != agent_digest(AGENT_CONFIGS)


def test_every_kind_s_prompt_carries_the_experiment_s_rule() -> None:
    prompts = system_prompts(agent_configs([RULE]))
    assert set(prompts) == {kind.value for kind in TaskKind}
    assert all(prompt.endswith(f"- {RULE}") for prompt in prompts.values())
    # the ticket prompt offers escalation and the benchmark one doesn't, as the configs say
    assert "escalate:" in prompts[TaskKind.TICKET.value]
    assert "escalate:" not in prompts[TaskKind.BENCHMARK.value]
