"""add retry budgets, backoff and dead letters

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPGRADE = (
    "alter table fleet_jobs drop constraint fleet_jobs_state_check",
    """
    alter table fleet_jobs add constraint fleet_jobs_state_check check (state in (
        'queued', 'claimed', 'running',
        'succeeded', 'failed', 'escalated', 'dead_lettered', 'cancelled'))
    """,
    """
    alter table fleet_jobs
        add column max_attempts integer not null default 3 check (max_attempts >= 1),
        add column available_at timestamptz not null default now(),
        add column last_error text
    """,
    # a job is never claimed past its retry budget
    """
    alter table fleet_jobs add constraint fleet_jobs_attempts_within_budget
        check (attempt <= max_attempts)
    """,
    "alter table fleet_attempts drop constraint fleet_attempts_ended_by_check",
    """
    alter table fleet_attempts add constraint fleet_attempts_ended_by_check
        check (ended_by in ('published', 'lease_expired', 'released'))
    """,
    "alter table fleet_attempts add column error text",
    # an attempt that ended says how
    """
    alter table fleet_attempts add constraint fleet_attempts_ended_with_reason
        check ((ended_at is null) = (ended_by is null))
    """,
)

_DOWNGRADE = (
    "alter table fleet_attempts drop constraint fleet_attempts_ended_with_reason",
    "alter table fleet_attempts drop column error",
    "alter table fleet_attempts drop constraint fleet_attempts_ended_by_check",
    """
    alter table fleet_attempts add constraint fleet_attempts_ended_by_check
        check (ended_by in ('published', 'lease_expired'))
    """,
    "alter table fleet_jobs drop constraint fleet_jobs_attempts_within_budget",
    "alter table fleet_jobs drop column max_attempts, drop column available_at, "
    "drop column last_error",
    "alter table fleet_jobs drop constraint fleet_jobs_state_check",
    """
    alter table fleet_jobs add constraint fleet_jobs_state_check check (state in (
        'queued', 'claimed', 'running', 'succeeded', 'failed', 'escalated', 'cancelled'))
    """,
)


def upgrade() -> None:
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in _DOWNGRADE:
        op.execute(statement)
