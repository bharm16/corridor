# Shadow and activation tooling

The fixture software for #564 and #535 does not authorize customer data or
activate a real environment. The signed partner authorization, selected source
configuration, deployed capability checks and operational disposition rehearsal
remain live evidence requirements on those issues.

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
roll back on failure. Retries return the previously frozen result, while a changed
configuration for the same source digest refuses relabeling it. The artifact keeps
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

`ActivationConfiguration` binds environment, customer, project, source/ingress,
region, code/database/image revisions, governance/security/disposition revisions,
web role and exact route-manifest digest. Evidence artifacts have fixed digests,
observation times, exact configuration identity and successful outcomes. Missing,
failed, changed or future-dated evidence writes no activation. PDF, model and pull
checkpoint gates apply only when those capabilities are selected.

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
