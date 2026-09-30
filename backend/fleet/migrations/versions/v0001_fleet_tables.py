"""create the fleet job store tables

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# one statement per execute: asyncpg prepares each statement on its own
_STATEMENTS = (
    """
    create table fleet_batches (
        id              bigint generated always as identity primary key,
        idempotency_key text unique,
        request_digest  text not null,
        label           text not null,
        created_at      timestamptz not null default now()
    )
    """,
    """
    create table fleet_jobs (
        id               bigint generated always as identity primary key,
        batch_id         bigint not null references fleet_batches (id),
        position         integer not null check (position >= 0),
        name             text not null,
        payload          jsonb not null,
        payload_digest   text not null,
        state            text not null default 'queued' check (state in (
                             'queued', 'claimed', 'running',
                             'succeeded', 'failed', 'escalated', 'cancelled')),
        attempt          integer not null default 0 check (attempt >= 0),
        worker_id        text,
        lease_expires_at timestamptz,
        result_id        bigint,
        submitted_at     timestamptz not null default now(),
        claimed_at       timestamptz,
        started_at       timestamptz,
        finished_at      timestamptz,
        updated_at       timestamptz not null default now(),
        unique (batch_id, position),
        -- a job holds a lease exactly while a worker has it
        check ((state in ('claimed', 'running')) = (lease_expires_at is not null)),
        -- a job is finished exactly when it has a published result
        check ((state in ('succeeded', 'failed', 'escalated')) = (result_id is not null))
    )
    """,
    "create index fleet_jobs_queued on fleet_jobs (id) where state = 'queued'",
    """
    create index fleet_jobs_leased on fleet_jobs (lease_expires_at)
        where state in ('claimed', 'running')
    """,
    """
    create table fleet_attempts (
        job_id           bigint not null references fleet_jobs (id),
        attempt          integer not null,
        worker_id        text not null,
        claimed_at       timestamptz not null,
        lease_expires_at timestamptz not null,
        ended_at         timestamptz,
        ended_by         text check (ended_by in ('published', 'lease_expired')),
        primary key (job_id, attempt)
    )
    """,
    """
    create table fleet_results (
        id           bigint generated always as identity primary key,
        job_id       bigint not null unique references fleet_jobs (id),
        attempt      integer not null,
        worker_id    text not null,
        published_at timestamptz not null default now(),
        outcome      text not null check (outcome in ('succeeded', 'failed', 'escalated')),
        body         jsonb not null
    )
    """,
    """
    alter table fleet_jobs add constraint fleet_jobs_result_fk
        foreign key (result_id) references fleet_results (id)
    """,
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("drop table if exists fleet_results, fleet_attempts, fleet_jobs, fleet_batches cascade")
