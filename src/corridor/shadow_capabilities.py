"""Low-level PostgreSQL proof of the shadow runtime's source-only capability.

This verifier depends only on SQLAlchemy. Source-command guards can call it
without importing ingestion, models, connector orchestration, or model clients.
Provisioning remains an explicit higher-level owner operation.
"""
from sqlalchemy import text


class ShadowRefused(ValueError):
    """A source, credential or environment has not met the shadow boundary."""



def verify_runtime(session, *, project_id, customer, environment):
    """Query privileges of the login and every identity it can SET ROLE into.

    PostgreSQL 16 MEMBER ignores membership options. SET finds identities the
    session can assume; privilege functions also account for each identity's
    INHERIT chains, including inheritance across a SET FALSE membership.
    """
    role = session.execute(text("select current_user, session_user, current_database()")).one()
    if role[0] != "corridor_worker" or role[1] != "corridor_worker":
        raise ShadowRefused("shadow capture requires the actual corridor_worker login")
    unsafe = session.scalar(text("""
        with reachable as (
          select r.* from pg_roles r
          where r.rolname=current_user or pg_has_role(session_user, r.oid, 'SET')
        ), protected_tables as (
          select c.oid, c.relowner from pg_class c
          where c.oid in ('public.release_candidates'::regclass,
                         'public.release_packages'::regclass,
                         'public.release_preparation_requests'::regclass)
        )
        select exists(select 1 from reachable r where
          r.rolsuper or r.rolcreaterole or r.rolbypassrls or r.rolcreatedb
          or pg_has_role(r.oid, 'corridor_fact_decision_writer', 'USAGE'))
        or exists(select 1 from reachable identity
          cross join pg_proc p join pg_roles owner on owner.oid=p.proowner
          join pg_namespace n on n.oid=p.pronamespace
          where n.nspname='public' and p.prosecdef
          and owner.rolname='corridor_fact_decision_writer'
          and has_function_privilege(identity.oid,p.oid,'EXECUTE'))
        or exists(select 1 from reachable identity cross join protected_tables relation
          where pg_has_role(identity.oid, relation.relowner, 'USAGE')
             or has_table_privilege(identity.oid,relation.oid,'INSERT,UPDATE,DELETE,TRUNCATE')
             or has_any_column_privilege(identity.oid,relation.oid,'INSERT,UPDATE'))
    """))
    if unsafe:
        raise ShadowRefused("runtime retains accepted-record or release authority")
    row = session.execute(text("select customer, environment, database_name from shadow_projects where project_id=:id"), {"id": project_id}).first()
    if row is None or tuple(row) != (customer, environment, role[2]):
        raise ShadowRefused("runtime is not bound to the provisioned shadow project")
    if session.scalar(text("select count(*) from customer_environment_binding")):
        raise ShadowRefused("shadow database acquired customer routing")
    if session.scalar(text("select count(*) from projects where id <> :id"), {"id": project_id}):
        raise ShadowRefused("shadow database now contains another project")

