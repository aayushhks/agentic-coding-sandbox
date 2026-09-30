"""add execution policies, cancellation and what each attempt ran under

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UPGRADE = (
    # the limits and grants every attempt of the job runs under, fixed at submission
    """
    alter table fleet_jobs
        add column policy jsonb not null default '{}'::jsonb,
        add column cancel_requested_at timestamptz
    """,
    "alter table fleet_attempts drop constraint fleet_attempts_ended_by_check",
    """
    alter table fleet_attempts add constraint fleet_attempts_ended_by_check
        check (ended_by in ('published', 'lease_expired', 'released', 'cancelled'))
    """,
    # how the attempt ran (container, image, limits applied, exit) and the tail of its output
    "alter table fleet_attempts add column execution jsonb, add column logs text",
)

_DOWNGRADE = (
    "alter table fleet_attempts drop column execution, drop column logs",
    "alter table fleet_attempts drop constraint fleet_attempts_ended_by_check",
    """
    alter table fleet_attempts add constraint fleet_attempts_ended_by_check
        check (ended_by in ('published', 'lease_expired', 'released'))
    """,
    "alter table fleet_jobs drop column policy, drop column cancel_requested_at",
)


def upgrade() -> None:
    for statement in _UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in _DOWNGRADE:
        op.execute(statement)
