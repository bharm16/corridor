"""The bound customer environment, attested in this database (#656).

Attestation only: the environment registry and the destruction receipts
live in an independently bootstrapped control-plane database and never in
this schema.  The single row is immutable, and a bound environment refuses
to downgrade its routing attestation rather than lose it.
"""

from __future__ import annotations

import sqlalchemy as sa




def upgrade(op) -> None:
    # #656: attestation only. The registry and destruction receipts are in an
    # independently bootstrapped control-plane database, never this schema.
    op.execute("""
        create table public.customer_environment_binding (
            singleton boolean primary key constraint ck_customer_environment_singleton check (singleton),
            customer_id varchar(128) not null,
            environment_id varchar(128) not null,
            deployment_id varchar(128) not null
        );
        revoke all on public.customer_environment_binding from corridor_web, corridor_worker;
        grant select on public.customer_environment_binding to corridor_web, corridor_worker;
        create function public.preserve_customer_environment_binding() returns trigger
        language plpgsql set search_path = pg_catalog as $$
        begin raise exception 'customer environment binding is immutable'; end $$;
        revoke all on function public.preserve_customer_environment_binding() from public;
        create trigger customer_environment_no_rewrite before update or delete
            on public.customer_environment_binding for each row
            execute function public.preserve_customer_environment_binding();
        create trigger customer_environment_no_truncate before truncate
            on public.customer_environment_binding for each statement
            execute function public.preserve_customer_environment_binding();
    """)


def downgrade(op) -> None:
    if op.get_bind().scalar(sa.text("select exists (select 1 from public.customer_environment_binding)")):
        raise RuntimeError("a bound customer environment cannot downgrade its routing attestation")
    op.execute("drop table public.customer_environment_binding")
    op.execute("drop function public.preserve_customer_environment_binding()")
