# Customer environments and external receipt custody

#656 supplies the software registry and routing contract for ADR-0079 and
ADR-0083. Its acceptance uses a separate local PostgreSQL control-plane
database and synthetic customer databases, including actual removal of a
synthetic database. It does not establish an AWS deployment (#489), customer
activation (#535), or customer destruction (#514).

The control plane contains only `control_plane.customer_environments` and
`control_plane.destruction_receipts`, using separate SQLAlchemy metadata. It
contains stable customer/environment/deployment and database identifiers,
credential and connector references, the object namespace reference, enabled
state, hold state, and external receipt observations. It has no Project Record
relations, project IDs, source contents, source bytes, or customer replicas.

Each customer database has one immutable `customer_environment_binding` row.
That is an identity attestation, with no project or content metadata. Routing
compares this row with the authenticated customer and the control-plane route
before reading sign-in or customer data. Multiple projects remain inside that
customer database; their existing membership, designation and PostgreSQL
partition checks still apply.

## Inputs deployment #489 supplies

One configured customer environment per application/worker process is the
initial topology. There is no request header, hostname or project-ID selector.

| Input | Consumer and boundary |
| --- | --- |
| `CONTROL_PLANE_DATABASE_URL` | Control-plane schema owner, only for `initialize`. Explicit PostgreSQL URL for a separate database. Never injected into web or worker. |
| `CONTROL_PLANE_OPERATIONS_DATABASE_URL` | Distinct operations login granted `corridor_control_operations`. Registration, state changes and receipt custody only. Never injected into web or worker. |
| `CONTROL_PLANE_RESOLVER_DATABASE_URL` | Distinct runtime login granted `corridor_control_resolver`, with no operations membership or direct table grants. Web and worker may execute only the customer/environment/deployment lookup. |
| `CORRIDOR_CUSTOMER_ID` | Stable customer ID, identical in the registry and the customer's local attestation. |
| `CORRIDOR_CUSTOMER_ENVIRONMENT_ID` | Stable environment ID, identical in the registry and local attestation. |
| `CORRIDOR_DEPLOYMENT_ID` | Stable deployment ID, identical in the registry and local attestation. |
| `CORRIDOR_CUSTOMER_ROUTING_KEY` | Separate random secret of at least 32 bytes, web only. Used to bind customer identity to the exact authenticated browser session. Rotation invalidates existing customer cookies and requires sign-in again. Never stored in the registry. |
| `WEB_DATABASE_URL` | Existing `corridor_web` credential for the customer database, injected at runtime. The registry stores only `env:WEB_DATABASE_URL`. |
| `WORKER_DATABASE_URL` | Existing `corridor_worker` credential for the customer database, worker only. The registry stores only `env:WORKER_DATABASE_URL`. Never injected into the web task. |
| `DATABASE_URL` | Customer schema owner, migration and `bind-customer` only. Never a runtime fallback. |

The URLs above are runtime secret values: obtain them through the deployment's
secret-injection mechanism, never command arguments, committed JSON or logs.
The bootstrap creates two NOLOGIN capability roles. Deployment provisions
distinct logins, grants each exactly one capability, and supplies its URL.
The control-plane database revokes PUBLIC connect and grants it only to the
two capabilities (and retains its owner); customer runtime logins cannot enter
it. Resolver credentials must not carry direct registry or receipt privileges.

Database endpoints in registration are identifiers without credentials.
Credential references name environment variables only. The resolver accepts
only a matching `postgresql+psycopg` host/port/database and the exact customer
capability login. URL query options may configure TLS, connection timeout and
application name; they may not override the database, host, user or role.
Deployed URLs should retain the existing `sslmode=verify-full` and trust bundle
configuration described in the [AWS runbook](../deployment/nonproduction-aws.md).

The object namespace reference is deployment inventory. #489 must also supply
the existing object-storage settings for that same customer namespace. This
registry does not create a second storage adapter or copy any content.

The web task retains only its web and scoped resolver credentials. Its existing
`/health` and inbound-mail machine routes require a worker capability and remain
unavailable in that deployment; missing worker credentials refuse without a
fallback. Deployment probes use `/livez` and `/readyz`, with separate worker
health verification. Enabling a machine ingress requires its separately scoped
deployment path, not adding worker authority to the web task.

## Initialization and bounded operations

Provision the separate database and credentials through the deployment path.
These commands do not create a database or an AWS resource. Supply secrets
through the environment before invoking a target; none of these commands prints
URLs or driver exception text.

1. With the separate control-plane owner URL, run `make control-plane
   ARGS="initialize"`. It refuses a database containing any customer or
   unrelated application relation. Re-running installs the same schema and
   grants; it does not run the customer Alembic migrations.
2. Provision the operations and resolver logins with the capability grants
   above. Do not give either login the customer schema-owner role.
3. Run the customer schema migration to the repository's current head through
   the existing migration job. With that customer's explicit `DATABASE_URL`
   and three identity inputs, run `make control-plane ARGS="bind-customer"`.
   Repeating the same identity succeeds; changing a bound identity refuses.
4. Review a registration file containing identifiers/references only, initially
   disabled. Run `make control-plane ARGS="register --file
   environment-registration.json"` using the operations URL. Registration is
   idempotent for identical input. Customer/environment/deployment identity and
   database/credential/namespace bindings cannot be overwritten. Changes to
   routing identity require a separately reviewed environment, not reassignment.
5. Verify the deployed routing and remaining activation gates before changing
   enabled state. #535 owns permission to activate customer data. Registering
   an environment or setting `enabled` is not evidence those gates passed.

Example synthetic registration:

```json
{
  "customer_id": "synthetic-a",
  "environment_id": "synthetic-a-nonproduction",
  "deployment_id": "corridor-nonproduction",
  "database_host": "configured-postgresql-host",
  "database_port": 5432,
  "database_name": "corridor_synthetic_a",
  "web_credential_ref": "env:WEB_DATABASE_URL",
  "worker_credential_ref": "env:WORKER_DATABASE_URL",
  "object_namespace_ref": "namespace:corridor-nonproduction/synthetic-a",
  "connector_configuration_ref": "configuration:none",
  "enabled": false,
  "hold": false
}
```

`make control-plane ARGS="inspect synthetic-a-nonproduction"` reads the
registered identifiers and state. `make control-plane ARGS="state
synthetic-a-nonproduction --enabled --hold"` explicitly sets both routing and
hold state. Use `--disabled` and/or `--no-hold` only for the intended change;
neither state defaults from an omitted flag. The optional
`--connector-configuration-ref configuration:approved-version` changes that
reference. Hold is independent of serving access: a hold blocks disposition
under ADR-0083, not ordinary authenticated reads. #514 must inspect it again
at every destructive step.

After magic-link authentication, the web application issues an HttpOnly,
Secure, SameSite=Strict customer cookie signed over the three IDs and the
existing opaque session secret's digest. Every authenticated request verifies
that binding and then re-reads the enabled route. Existing person-session
revocation, CSRF checks and project authorization run in the selected database.
The configured route also covers sign-in and probes, before any database data
is read. Worker sessions re-resolve the same configured environment on every
opening. A cached connection never caches permission to use the environment.

After routing-key rotation, visit `/sign-in` to authenticate again. Only the
three sign-in endpoints can discard an invalid or missing customer binding:
they ignore the old session identity, verify the configured environment, and
clear the old customer/session/CSRF cookies on form and unsuccessful-attempt
responses. A successful fresh magic link issues a new session and customer
binding under the current key. A browser can also submit an already-open form
or consume a fresh link while still carrying stale cookies. Protected routes
continue to refuse those cookies before any customer-database query; disabled
or mismatched environment configuration cannot use the recovery path.

Missing, unknown, disabled, mismatched or inaccessible routes refuse. A
credential pointing at another database, a local attestation mismatch, or a
customer cookie from another session refuses before customer data. There is
no fallback to the control plane or a default customer database. Only a
development/test process with all routing inputs absent retains the existing
local single-database workflow; partial configuration and every deployed
environment fail closed.

## Receipt handoff to #514

`make control-plane ARGS="record-destruction --file destruction-receipt.json"`
appends an observed outcome under operations credentials. It authorizes and
executes no deletion. `make control-plane ARGS="receipts
synthetic-a-nonproduction"` reads the retained observations after the customer
database disappears.

```json
{
  "receipt_id": "synthetic-operation-postgresql-removed",
  "environment_id": "synthetic-a-nonproduction",
  "operation_id": "synthetic-operation",
  "component": "postgresql",
  "outcome": "completed",
  "evidence_ref": "receipt:synthetic-operation/database-absence",
  "recorded_by": "local:operator-name",
  "observed_at": "2026-09-08T12:00:00+00:00"
}
```

The example is a shape, not an operational receipt. Components are
`postgresql`, `object_namespace`, `encryption_key`, `backups` or `environment`;
outcome is `completed` or `failed`. Each later observation uses a new receipt
ID under the same operation. An identical replay returns the same receipt;
reusing an ID for a different observation refuses. Database triggers and
privileges reject updates, deletes and truncation. The retained environment
registration preserves the referenced customer and deployment identities.

#514 owns legal hold precedence, export/custody transfer, ordered destruction,
backup expiration, failure recovery and evidence supporting each observation.
Removing PostgreSQL alone is not complete environment destruction. Evidence
references must resolve outside the customer database and object namespace
being removed. #489 supplies the separate control-plane backup and object
custody location; this software proof does not claim it exists in AWS.

## Synthetic acceptance

Run `make test-focused ARGS="tests/test_control_plane.py"` and `make check`.
The test harness owns separate disposable PostgreSQL databases and scoped
control-plane logins. It proves colliding customer-local project IDs,
membership/RLS after routing, actual database removal with surviving receipts,
forbidden content/operations access, and route/credential/binding refusals.
The required PR CI gate owns complete suite and migration proof.
