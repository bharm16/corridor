"""#693 No application relation carries a privilege granted to PUBLIC.

#680 revoked 124 relations from `corridor_web` and, for one of them, the
revoke reported success while the relation stayed readable.
`subject_resolution_decisions` also carried `GRANT SELECT ... TO PUBLIC`,
left by the command role that created it. A revoke aimed at a named role
does not remove a PUBLIC grant, and `has_table_privilege('corridor_web',
...)` answers *true* through PUBLIC without saying so — so both halves of
#680's check agreed the boundary was complete while it was not.

#680 answered that with a one-relation list, which fixed the relation it had
found and left `fact_decisions` and `project_record_revisions` carrying the
same grant. Neither is intentional; both are incidental to the command role
that owns them. So this block states the rule instead of naming relations:
**no application table or view in `public` holds any privilege granted to
PUBLIC**, relation-level or column-level, and the sweep discovers what to
revoke from the catalog. A relation that acquires such a grant later is
governed by the same statement, not by somebody remembering to extend a list.

**How bad it was, stated accurately.** `fact_decisions` and
`project_record_revisions` are partitioned, so a role reading them through
PUBLIC met row-level security and saw no rows; this was not an open
cross-project read. What was wrong is that nothing intended or documented
the grant, and any role added to this database later would have started with
SELECT on two accepted-record relations by decision of nobody.

**What is deliberately left alone.** PostgreSQL grants `USAGE` on the
`public` schema to PUBLIC, and grants `EXECUTE` on a function to PUBLIC when
the function carries no ACL of its own. The schema grant is what makes any
named table grant reachable and is not a privilege on a relation. The
function default is real, but every `SECURITY DEFINER` command here already
carries an explicit ACL with no PUBLIC entry — the narrow execution grants
#492 and #531 wrote — and the rest run with the caller's own rights.
The schema owner's `ALTER DEFAULT PRIVILEGES` is also left exactly as #680
left it: it hands each *named* runtime login the privileges on a new table,
which is a decision about named capabilities that #680 answered with a
compensating live test rather than by changing the default. It grants
nothing to PUBLIC, so it is not this block's subject.
"""

from __future__ import annotations



# Intentional PUBLIC privileges, keyed by relation and privilege, valued by
# the written reason. Empty by decision (#693): an entry means every role in
# the customer database, including every role added later, is meant to hold
# that privilege, and that needs a reason rather than an omission.
PUBLIC_RELATION_PRIVILEGE_ALLOWLIST: dict[tuple[str, str], str] = {}

# What the sweep is known to find at this revision, so the downgrade hands
# back the exact prior shape rather than a blanket re-grant. All three were
# `SELECT` at relation level, granted by `corridor_fact_decision_writer`,
# which owns all three tables; the migration runs as the schema owner, and a
# superuser's `GRANT` on a table it does not own records the table's owner as
# the grantor, so re-granting here reproduces the ACL entry byte for byte
# rather than an equivalent one with a different grantor.
PUBLIC_RELATION_PRIVILEGES_AT_THIS_REVISION: tuple[tuple[str, str], ...] = (
    ("fact_decisions", "SELECT"),
    ("project_record_revisions", "SELECT"),
    ("subject_resolution_decisions", "SELECT"),
)


def _text_array_sql(values: tuple[str, ...]) -> str:
    """A PL/pgSQL `text[]` literal that is still valid when the list is empty.

    `array[]` on its own is a syntax error, so an empty allowlist rendered the
    obvious way makes the migration fail to *build* rather than making the
    guard fail — which is a broken statement, not a proof of anything.
    """

    if not values:
        return "array[]::text[]"
    inner = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"array[{inner}]"


_PUBLIC_ALLOWED_SQL = _text_array_sql(
    tuple(
        f"{relation}:{privilege}"
        for relation, privilege in sorted(PUBLIC_RELATION_PRIVILEGE_ALLOWLIST)
    )
)

_PUBLIC_RECORDED_SQL = _text_array_sql(
    tuple(
        f"{relation}:{privilege}"
        for relation, privilege in PUBLIC_RELATION_PRIVILEGES_AT_THIS_REVISION
    )
)

# `aclexplode` on `relacl` and on `attacl`, grantee 0, is the *effective*
# reading: it reports the grant whatever role made it and whichever named
# capability list does or does not mention the relation. Column grants are
# swept for the same reason #680 revoked `all` rather than `select` — a
# privilege nobody thinks to name is exactly the one that survives.
PUBLIC_PRIVILEGE_REVOKE = f"""
do $$
declare
    v_allowed text[] := {_PUBLIC_ALLOWED_SQL};
    v_recorded text[] := {_PUBLIC_RECORDED_SQL};
    v_unrecorded text[];
    v_row record;
begin
    select coalesce(array_agg(distinct entry order by entry), array[]::text[])
      into v_unrecorded
      from (
        select c.relname || ':' || a.privilege_type as entry
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
          cross join lateral aclexplode(c.relacl) a
         where n.nspname = 'public'
           and c.relkind in ('r', 'p', 'v', 'm', 'f')
           and a.grantee = 0
        union all
        select c.relname || ':' || att.attname || ':' || a.privilege_type
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
          join pg_attribute att
            on att.attrelid = c.oid and att.attnum > 0 and not att.attisdropped
          cross join lateral aclexplode(att.attacl) a
         where n.nspname = 'public'
           and c.relkind in ('r', 'p', 'v', 'm', 'f')
           and a.grantee = 0
      ) found
     where not (entry = any(v_allowed))
       and not (entry = any(v_recorded));

    if array_length(v_unrecorded, 1) is not null then
        raise exception
            '#693 found PUBLIC privileges this revision did not record: %. '
            'The downgrade restores exactly what the upgrade removed, so a '
            'grant it cannot name would be lost rather than handed back. '
            'Record it beside the other three, or allowlist it with a reason.',
            array_to_string(v_unrecorded, ', ');
    end if;

    for v_row in
        select c.relname, a.privilege_type
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
          cross join lateral aclexplode(c.relacl) a
         where n.nspname = 'public'
           and c.relkind in ('r', 'p', 'v', 'm', 'f')
           and a.grantee = 0
           and not (c.relname || ':' || a.privilege_type = any(v_allowed))
         order by c.relname, a.privilege_type
    loop
        execute format(
            'revoke %s on public.%I from public',
            v_row.privilege_type, v_row.relname
        );
    end loop;

    for v_row in
        select c.relname, att.attname, a.privilege_type
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
          join pg_attribute att
            on att.attrelid = c.oid and att.attnum > 0 and not att.attisdropped
          cross join lateral aclexplode(att.attacl) a
         where n.nspname = 'public'
           and c.relkind in ('r', 'p', 'v', 'm', 'f')
           and a.grantee = 0
           and not (
             c.relname || ':' || att.attname || ':' || a.privilege_type
               = any(v_allowed)
           )
         order by c.relname, att.attname, a.privilege_type
    loop
        execute format(
            'revoke %s (%I) on public.%I from public',
            v_row.privilege_type, v_row.attname, v_row.relname
        );
    end loop;
end $$;
"""

# The exact prior shape, relation by relation and privilege by privilege. Not
# `grant all`, and not a loop over the denied list: two of these three are
# relations the pilot keeps and reads, and widening them on the way down would
# use the downgrade to grant something the upgrade never took.
PUBLIC_PRIVILEGE_RESTORE = "\n".join(
    f"grant {privilege.lower()} on public.{relation} to public;"
    for relation, privilege in PUBLIC_RELATION_PRIVILEGES_AT_THIS_REVISION
)


def upgrade(op) -> None:
    # Last of all, after every block above has created its relations and set
    # its grants, because this one is a sweep of the finished catalog rather
    # than a statement about named relations. Running it earlier would leave a
    # PUBLIC grant made by a later block standing, which is the exact failure
    # mode it exists to close.
    op.execute(PUBLIC_PRIVILEGE_REVOKE)


def downgrade(op) -> None:
    # First, because the upgrade added it last. The restore names the exact
    # three relation-and-privilege pairs the sweep removed, so the ACL that
    # comes back is the one that was there — a blanket `grant all to public`
    # would hand the database back wider than it was found.
    op.execute(PUBLIC_PRIVILEGE_RESTORE)
