"""Low-level PostgreSQL proof of the shadow runtime's source-only capability.

This verifier depends only on SQLAlchemy. Source-command guards can call it
without importing ingestion, models, connector orchestration, or model clients.
Provisioning remains an explicit higher-level owner operation.
"""
from sqlalchemy import text


class ShadowRefused(ValueError):
    """A source, credential or environment has not met the shadow boundary."""



def verify_runtime(session, *, project_id, customer, environment):
    """Query the actual login, role inheritance and effective command grants."""
    role = session.execute(text("select current_user, session_user, current_database()")).one()
    if role[0] != "corridor_worker" or role[1] != "corridor_worker":
        raise ShadowRefused("shadow capture requires the actual corridor_worker login")
    unsafe = session.scalar(text("""
        select exists(select 1 from pg_roles r where
          (r.rolsuper or r.rolcreaterole or r.rolbypassrls or r.rolcreatedb
           or r.rolname='corridor_fact_decision_writer')
          and pg_has_role(current_user, r.oid, 'MEMBER'))
        or exists(select 1 from pg_proc p join pg_roles r on r.oid=p.proowner
          join pg_namespace n on n.oid=p.pronamespace
          where n.nspname='public' and p.prosecdef
          and r.rolname='corridor_fact_decision_writer'
          and has_function_privilege(current_user,p.oid,'EXECUTE'))
        or has_table_privilege(current_user,'release_candidates','INSERT')
        or has_table_privilege(current_user,'release_packages','INSERT')
        or has_table_privilege(current_user,'release_preparation_requests','INSERT')
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

