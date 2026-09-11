# Shadow and activation tooling

The fixture software for #564 and #535 does not authorize customer data or
activate a real environment. The signed partner authorization, selected source
configuration, deployed capability checks and operational disposition rehearsal
remain live evidence requirements on those issues. So does the retained proof
that a limited onboarding authorization for this customer, environment and
project was valid when the baseline adoption committed: under
[ADR-0099](../adr/0099-onboarding-before-activation-runs-under-a-limited-authorization-not-under-relaxed-activation-checks.md)
the baseline that #535 requires is adopted under that authorization, never by
relaxing the activation checks while the project is unadopted. Activation does
not ask for an unexpired authorization; it asks for that proof, alongside
current customer authorization and the ordinary activation prerequisites.

#827 built the authorization, its control-plane record, the retained proof and
the granted adoption command. The proof an activation reads is a
`project_onboarding_acts` row for that project and the `adopt_baseline`
operation: it carries the authorization identity and version the act ran under,
the validity that held when it committed, and the receipt it produced. What
that is, who may issue and withdraw one, and the **maximum stale-validity
window before customer deployment** are in
[the onboarding authorization guide](onboarding-authorization.md).

## Shadow capture

Use a separate database with exactly one project and no customer-environment
routing binding. A named human adopts its baseline through the existing #509
command. The schema owner then calls `provision_shadow_project`; the runtime
never receives that owner's session or the baseline-adoption credential.
Provisioning refuses existing roster memberships or release state and revokes
worker release preparation writes within this database. Normal migrations install
the registry, immutable run receipts, customer-surface triggers and restrictive
project visibility policy. Neither runtime calls nor tests install their own
schema.

`run_shadow_ucm` takes an actual `corridor_worker` login. It checks PostgreSQL's
session login, effective role membership, accepted-record command execution and
release insertion grants before processing. It verifies the stored compatibility
receipt's digest, exact byte digest, resolved mapping reading and customer/project,
and separately requires the authorization's shadow stage. Its #511 delivery must
already be stored. #606 resolves the registered mapping and produces native #518
identities without a model call. No connector or provider is selected implicitly.

The capture and frozen receipt share one transaction. Commit once after success;
roll back on failure. Retries return the previously frozen result. A delivery and shadow configuration
have one receipt; different external delivery versions are not equated merely
because their bytes match. The native document-delivery binding still applies. The artifact keeps
native delta/group identities and lifecycle, typed values, mapping, source segment
locators, fact links, delivery identity, actual database freeze time and delivery
watermark. A comparison successor must have both a later delivery identity and a
stored delivery timestamp after the freeze. The watermark alone does not prove
chronology when another delivery was in flight.

The runtime can neither add the shadow project to a coordinator roster nor create
its release request, candidate or package. Customer web readings cannot see it.
A whole-environment disposition handles its retention; registry and receipt
updates/deletion are refused, and downgrade refuses while custody remains.

## Activation evidence

`ActivationConfiguration` binds environment, customer, project, the recorded
source authorization the deployment processes under, ingress,
region, code/database/image revisions, governance/security/disposition revisions,
web role and exact route-manifest digest. The source half is three values --
`source_authorization_identity`, `source_authorization_version` and
`source_authorization_sha256` -- naming one version of the project's authorized
source bindings, which is recorded in the customer database by the operations
capability through `record_project_source_authorization` and whose digest is
read back from that record rather than computed by the operator. It replaced a
single channel and configuration pair, which could name one channel and so
refused a valid product upload on a deployment activated for a project alias
(#886). Every channel a project takes delivery on, manual upload included, is
authorized by name there or the delivery is refused; recording a higher version
withdraws or widens the set, and a deployment activated against the version it
replaced stops taking new deliveries until a new activation revision names the
current one. Evidence artifacts have fixed digests,
observation times, exact configuration identity and successful outcomes. Missing,
failed, changed or future-dated evidence writes no activation. Every selected
customer image requires the #766 built-image audit, including a deterministic
UCM-only activation. Its actual image identity, code revision, both Python
environments, executable/system-package absence and retained PDFium dependency
notices must agree. Fixture audit observations exercise this validation but do
not prove any deployed image. Model and pull checkpoint gates apply only when
those capabilities are selected.

`collect_boundary_smoke` reads privileges as the actual `corridor_web` login and
calls the deployed authenticated HTTP client. It requires the service's health
response to report an enforced boundary, successful fixture requests for every
approved route and refusal for a disabled route. Provide approved fixture payloads
for write routes. The collector does not manufacture principals or authorize a
customer-side write. Running smoke against a synthetic service proves tooling;
the actual selected deployment must supply its own evidence.

`activate` freezes a revision in separate receipt custody, publishing only a fully
written object. Reusing a revision is idempotent only with identical inputs.
`processing_authorized` rejects that receipt after any configuration change. This
module does not enable a control-plane route: the deployment's authorized operator
must use the receipt at that boundary. A synthetic receipt cannot discharge signed
customer authorization or actual operational gate requirements.

## Runtime enforcement

Production processes must declare `CORRIDOR_DEPLOYMENT_DATA_CLASS` as `customer`,
`synthetic`, or `shadow`. An empty or unknown production value refuses. The
unconfigured development/test path remains available for existing fixtures.
A shadow process cannot configure a customer router; its source commands check
the actual worker login and provisioned shadow database instead.

A customer deployment supplies trusted `CORRIDOR_ACTIVATION_CONFIGURATION_PATH`,
`CORRIDOR_ACTIVATION_RECEIPT_PATH`, and `CORRIDOR_ACTIVATION_RECEIPT_SHA256`, together
with its current `CORRIDOR_DEPLOYMENT_IMAGE_DIGEST`,
`CORRIDOR_DEPLOYMENT_DATA_REGION`, customer/environment/deployment identity, and
explicit live web boundary. The baked `CORRIDOR_CODE_REVISION` must match. Neither
requests nor `session.info` can select these inputs. Routing checks before
resolver SQL or credential resolution; every use rereads the files and receipt.
Database revision and image identity are trusted deployment attestations; the web
role gains no migration-table privilege.

Push and production pull intake compare the actual delivery binding's customer,
project, channel, configuration identity and configuration version before fetch
or storage. Every source command checks the current project activation and actual
database identity even if its caller supplied an existing session directly.
Corrections to previously captured facts require current project activation,
without pretending their historical source version is a new delivery under the
current connector configuration.

Human baseline adoption uses a separate explicit source-bootstrap context after
validating the named human principal. In customer/shadow deployments that context
requires the actual schema owner on that exact session; it grants no bypass to a
second session, request metadata or a runtime login. Compatibility intake retains
its independent database-free authorization and storage boundary.

The database's `seal_shadow_run` trigger owns the actual freeze instant, delivery
watermark, native delta values/lifecycle, groups and source provenance. Caller
values cannot backdate a receipt or hide a delivery already in the database. Its
only canonical JSON representation is PostgreSQL `payload::text`, identified as
`postgresql-jsonb-text-v1`. `read_shadow_run` exports those exact bytes and their
SHA alongside the verified parsed payload; Python never recreates the output
digest through its own JSON serializer. There is no second stored payload copy.

## Operator commands

`make activation ARGS="validate --configuration configuration.json --evidence evidence.json --operator local:operator --revision activation-1"`
checks all prerequisite artifacts and writes nothing. The evidence manifest maps
each gate to `{"path": "artifact.json", "sha256": "..."}`; relative paths resolve
beside that manifest. Use `freeze` with the same arguments and `--custody <dir>`
to publish the immutable receipt. Neither command enables a route or produces
missing live evidence.

`make shadow-processing ARGS="provision --database-url-env SHADOW_OWNER_URL --project-id <id> --customer <customer> --environment <environment> --operator <principal>"`
provisions isolation for an existing, separately human-adopted baseline. The named
environment variable holds the explicit schema-owner URL; the URL is never a
command-line argument. Optional `--intake-secret-env SHADOW_INTAKE_SECRET` binds
a webhook credential to this project or verifies its existing exact binding.

Use `run` with `--database-url-env SHADOW_WORKER_URL`, the same project/customer/
environment, a named operator, `--source-configuration`, `--deletion-date`,
`--authorization`, `--compatibility-receipt`, and `--compatibility-sha256`.
Choose an existing `--delivery-id`, or provide `--input` with the registered
`--intake-secret-env` and an optional stable `--transport-delivery-id`. Exact
compatibility and signed shadow authorization are verified before input storage.
Use `--complete --sealed` only for an explicitly complete enumerative source;
both default to false. Runtime commands accept only the actual worker credential.

Use `export` with the worker URL, project/customer/environment, frozen `--identity`
and `--output`. It writes a verified envelope containing the exact PostgreSQL
payload text and its SHA, without printing source content. Repeating the export
is idempotent; an existing destination with different bytes refuses.
