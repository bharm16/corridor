# How long a session record, a sign-in link and an authentication attempt are kept

**Date:** 2026-09-11. **Question raised by:** [#907](https://github.com/bharm16/corridor/issues/907), which found that `web_sessions`, `sign_in_tokens` and `sign_in_attempts` evaluate expiry inline but that nothing deletes them, and asked for "a stated retention for each" and for a decision on "whether a sign-in attempt is kept longer than a session or a token, and why". **Procedure:** [Research before proposing terminology](../agents/domain.md#research-before-proposing-terminology), applied to the terms below; the retention periods themselves are recorded in [ADR-0102](../adr/0102-a-sign-in-record-is-deleted-once-it-can-no-longer-authorize-and-an-attempt-log-is-kept-longer.md).

**What this note is for.** #907 asks for three numbers. Before proposing any of them the honest question is which of the three has a published basis and which is a judgement. The answer turns out to be uneven, and the unevenness is the most important finding here, so it is stated first.

## 0. The short answer

| Record | Is there a published number for how long it may **live**? | Is there a published number for how long the **row may be kept after it dies**? |
|---|---|---|
| Session record | Yes, several, and they disagree by design across assurance levels | **No.** Every source says *invalidate*; none says *delete* |
| Single-use emailed sign-in link | Yes: 10 minutes as an authenticator, 24 hours as an emailed confirmation code | **No** |
| Authentication attempt log | The throttle counter's useful life is short and NIST says to reset it on success | **Yes**: 90 days, 6-12 months and 12 months from three independent sources |

So of the three periods ADR-0102 proposes, **only the attempt-log period rests on published practice.** The session and token periods are judgements, and the ADR says so rather than dressing them in citations.

**One thing I looked for and did not find.** No primary source states the distinction #907 assumes — that an authentication attempt record is a security log retained for investigation while a session record and a one-time token are operational credentials to be deleted once they can no longer authorize anything. I searched for it directly. What exists is two separate bodies of requirement that are consistent with that reasoning without ever joining it up (§4 below). The step from "this row can no longer authorize anything" to "therefore delete this row" is Corridor's argument to make, not a citation it can lean on.

## 1. What the sources say about a session record

### NIST SP 800-63B-4

**Digital Identity Guidelines: Authentication and Authenticator Management**, NIST, July 2025, final ([DOI 10.6028/NIST.SP.800-63B-4](https://doi.org/10.6028/NIST.SP.800-63B-4)). This revision supersedes SP 800-63B (June 2017, updated 2 March 2020), and the title changed with it: Rev 3 was "Authentication and Lifecycle Management". Everything below is Rev 4.

- §2.1.3 (AAL1): "A definite reauthentication overall timeout SHALL be established, which SHOULD be no more than 30 days at AAL1. An inactivity timeout MAY be applied but is not required at AAL1."
- §2.2.3 (AAL2): "A definite reauthentication overall timeout SHALL be established, which SHOULD be no more than 24 hours at AAL2. The inactivity timeout SHOULD be no more than 1 hour."
- §2.3.3 (AAL3): "At AAL3, the overall timeout for reauthentication SHALL be no more than 12 hours. The inactivity timeout SHOULD be no more than 15 minutes."
- §5.1: session binding secrets are "erased or invalidated by the session subject when the subscriber logs out", and "Secrets will time out and are not accepted after the times specified in Sec. 2.1.3."

Corridor's `access.SESSION_TTL` is 12 hours with no inactivity timeout, which sits at the AAL3 overall figure and well inside AAL1 and AAL2. Nothing in #907 changes it.

The sentence that matters most for #907 is §5.1's: **"erased *or invalidated*"**. Invalidation is sufficient, which is exactly what Corridor already does, so NIST does not require the deletion #907 is asking for.

The one place NIST does address keeping records is **§2.4.2 (Records Retention Policy)**, and it delegates while insisting a decision be made:

> "The verifier SHALL comply with its respective records retention policies in accordance with applicable laws, regulations, and policies, including any National Archives and Records Administration (NARA) records retention schedules that may apply. If the verifier opts to retain records in the absence of mandatory requirements, the verifier or the CSP or IdP of which it is a part SHALL conduct a risk management process [NISTRMF], including assessments of privacy and security risks, to determine how long records should be retained and SHALL inform the subscriber of that retention policy."

That is the strongest published support for #907's own premise: a system that keeps these records without a stated period is non-conformant, whatever period it eventually picks.

### NIST SP 800-53 Rev 5, AC-12

"Automatically terminate a user session after [Assignment: organization-defined conditions or trigger events]." The discussion names "organization-defined periods of user inactivity, targeted responses to certain types of incidents, or time-of-day restrictions". **No period, and nothing about the record.**

### PCI DSS v4.0.1, requirement 8.2.8

If a user session has been idle for more than **15 minutes**, the user must re-authenticate. (In v3.2.1 this was 8.1.8 and was a recommendation.) *Not verified against the primary PDF: PCI SSC gates its standard behind a click-through terms acceptance, so this rests on multiple consistent secondary reproductions and should be checked against a copy of the standard before it is relied on.* Corridor is not in PCI scope today; the figure is recorded because it is the number most often quoted at a session timeout, and because it is **stricter than NIST's AAL2 hour** — it matches NIST's AAL3 inactivity figure, not AAL2, despite commentary that usually describes 8.2.8 as aligning with NIST.

### OWASP

- **Session Management Cheat Sheet**: "Common idle timeouts ranges are 2-5 minutes for high-value applications and 15-30 minutes for low risk applications", and for absolute timeouts "an appropriate absolute timeout range could be between 4 and 8 hours". On expiry: "When a session expires, the web application must take active actions to invalidate the session on both sides, client and server." Its session-lifecycle logging section recommends logging "the creation, renewal, and destruction of session IDs" and adds "It is recommended to log a salted-hash of the session ID instead of the session ID itself" — which is what Corridor stores in the row itself. **It says nothing about how long to keep the row.**
- **ASVS 5.0** (May 2025), **7.1.1 (L2)**: the inactivity timeout and absolute maximum session lifetime must be documented, "and that the documentation includes justification for any deviations from NIST SP 800-63B re-authentication requirements". ASVS 5.0 deliberately deleted its own session numbers and defers to NIST. **7.4.1 (L1)**: on logout or expiration the application "disallows any further use of the session. For reference tokens or stateful sessions, this means invalidating the session data at the application backend."
- ASVS 4.0.3 V3.3.2 *did* carry numbers — 30 days (L1), 12 hours or 30 minutes inactivity (L2), 12 hours or 15 minutes with 2FA (L3) — mirroring 800-63B **Rev 3**. Those are superseded. Citing "NIST says 12 hours and 30 minutes" today cites a withdrawn document.

**Conclusion for the session record.** There is a great deal of published practice on when a session must stop working and none at all on when its row must go. Any Corridor period for `web_sessions` is a judgement.

## 2. What the sources say about the emailed sign-in link

"Magic link" appears in no standard. Two NIST terms cover it, and they give numbers two orders of magnitude apart because they describe different acts.

- **NIST SP 800-63B-4 §3.1.3.2 (Out-of-Band Verifiers)**: "In all cases, the authentication SHALL be considered invalid unless completed within 10 minutes. Verifiers SHALL accept a given authentication secret as valid only once during the validity period to provide replay resistance." Corridor's `SIGN_IN_TOKEN_TTL` is 15 minutes and consumption is a single atomic `UPDATE`, so the single-use rule is met and the validity window is five minutes over this figure.
- **NIST SP 800-63A-4 §3.8 (Requirements for Confirmation Codes)**, July 2025, is the only place in the suite that describes an emailed link: a confirmation code may be presented as "a secure (e.g., https) link containing a representation of the confirmation code", and "Confirmation codes SHALL be valid for at most: ... **24 hours when sent to a validated email address**". "Upon its use, the CSP SHALL invalidate the confirmation code."
- **ASVS 5.0, 6.5.5 (L2)**: out-of-band requests, codes and TOTPs must have a defined lifetime; "Out of band requests must have a maximum lifetime of 10 minutes". **6.5.1 (L2)** requires single use.

**The scope caveat, stated rather than buried.** A confirmation code under 800-63A-4 proves control of an address during proofing and enrollment; it is not an authentication act, so 24 hours is not NIST endorsing a 24-hour sign-in link. And ASVS 5.0's V6.6 intro says outright that "Unsafe out-of-band authentication mechanisms such as **e-mail** and VOIP are not permitted" — so OWASP does not treat an emailed link as an acceptable authenticator at all, and 6.5.5's 10 minutes is being borrowed rather than applied on its own terms. Corridor's 15-minute window is defensible against both, but neither source blesses the channel.

**And again: nothing says when the consumed row goes.** 800-63A-4 says "invalidate", not "delete". Any Corridor period for `sign_in_tokens` is a judgement.

## 3. What the sources say about the attempt log — the one with real numbers

### The obligation to have it

- **OWASP Authentication Cheat Sheet**: "Enable logging and monitoring of authentication functions to detect attacks/failures on a real-time basis", "Ensure that all failures are logged and reviewed".
- **OWASP Logging Cheat Sheet**, "Which events to log": "Authentication successes and failures."
- **ASVS 5.0, 16.3.1 (L2)**: "Verify that all authentication operations are logged, including successful and unsuccessful attempts."
- **ASVS 5.0, 16.1.1 (L2)**: the log inventory must document "**for how long logs are kept**" — a second published requirement to state a period, whatever it is.

### The periods

- **PCI DSS v4.0.1, requirement 10.5.1**: retain audit log history for at least **12 months**, with at least the most recent **three months** immediately available for analysis. *Same primary-source caveat as §1.*
- **CIS Critical Security Controls v8/v8.1, Safeguard 8.10 (Retain Audit Logs)**: "Retain audit logs across enterprise assets for a minimum of **90 days**." Implementation Groups IG2 and IG3; not IG1.
- **CNIL Délibération n° 2021-122 du 14 octobre 2021 portant adoption d'une recommandation relative à la journalisation** (CNIL, France), ¶8: "La Commission recommande de conserver ces données pendant une durée comprise entre **six mois et un an**", balancing "la nécessité de disposer de données de journalisation permettant d'identifier les atteintes au système de traitement" against "la nécessité de ne pas conserver un volume de données trop important pouvant faire l'objet d'attaques ou de détournements de finalité". ¶19 allows up to **three years** where logging also serves internal control. ¶22: where the main processing keeps personal data for less than six months, the log should not keep personal data from it either and "peut ne conserver que des identifiants pseudonymes". CNIL's guidance page [Sécurité : Tracer les opérations](https://www.cnil.fr/fr/securite-tracer-les-operations) (updated 14 March 2024) restates it as "une période glissante comprise entre six mois et un an". **This is a French national recommendation under GDPR Article 32; it is not binding outside France and is not an EDPB position.**
- **NIST SP 800-53 Rev 5, AU-11 (Audit Record Retention)**: "Retain audit records for [Assignment: organization-defined time period] to provide support for after-the-fact investigations of incidents and to meet regulatory and organizational information retention requirements." The discussion points to NARA General Records Schedules. **No period**; Rev 5 also dropped Rev 4's "consistent with records retention policy" qualifier.
- **NIST SP 800-92** (September 2006, still the current final) names no period either; §4.1 says policy should define "How long each type of log data must or should be preserved", and footnote 43 points at NARA GRS 20. Its draft successor, **SP 800-92r1 ipd** (11 October 2023, no final published), asks in its own call for comments: "6. Should guidance for determining storage retention periods be included?" — which is direct evidence that NIST currently does not give one.

### How the three numbers relate

They are not the same kind of statement. PCI's 12 months and CIS's 90 days are **floors** ("at least", "a minimum of"). CNIL's six-months-to-a-year is a **band with a soft ceiling**, justified by the risk of holding the data at all. **365 days is the only single value that clears both floors and stays inside CNIL's band.** 180 days clears CIS and sits mid-band but falls a long way under PCI's floor.

Corridor is in neither PCI nor (today) GDPR scope, so no floor binds; the choice is which posture to adopt in advance of a customer contract or agency records schedule that does bind. That is the maintainer's call and ADR-0102 puts it to them.

### The tension inside the table, which the sources do point at

**NIST SP 800-63B-4 §3.2.2 (Rate Limiting (Throttling))** treats the throttle counter as something with a deliberately *short* useful life: "When the subscriber successfully authenticates, the verifier SHOULD disregard any previous failed attempts for the authenticators used in the successful authentication. Following successful authentication at a given AAL, the verifier SHOULD reset the retry count." Corridor's equivalent is `access.ATTEMPT_WINDOW`, fifteen minutes.

PCI, CIS and CNIL treat the authentication log as something with a *long* useful life. `sign_in_attempts` is doing both jobs in one relation, and the two pull in opposite directions over the same rows. The throttle scope is a normalized email address or a client address — exactly the personal data CNIL ¶22 and the OWASP Logging Cheat Sheet's de-identification advice ("deletion, scrambling or pseudonymization") are concerned about. **Separating the counter from the security log, or pseudonymizing the scope once the counter has reset, is the move these sources point toward. None of them prescribes it, and #907 does not ask for it.** It is recorded here and raised in ADR-0102 as a question, not built.

One unrelated observation for whoever next touches `access`: the OWASP Authentication Cheat Sheet says "The counter of failed logins should be associated with the account itself, rather than the source IP address, in order to prevent an attacker from making login attempts from a large number of different IP addresses." Corridor counts by both (`ISSUE_EMAIL`, `ISSUE_IP`, `CONSUME_IP`), which satisfies it. Nothing to do.

## 4. The distinction #907 assumes, and what actually supports it

#907 says the answer for a consumed token "is probably not the answer for a sign-in attempt (which is the record of who tried to reach the product and may be wanted for longer)". **No source states that.** What the sources give is two halves that never meet:

- The **investigative** rationale for keeping logs is explicit. NIST 800-53 AU-11: retain audit records "to provide support for after-the-fact investigations of incidents". NIST SP 800-92 §2.3.2 defines the pair Corridor's own design needs — "**Log retention** is archiving logs on a regular basis as part of standard operational activities. **Log preservation** is keeping logs that normally would be discarded, because they contain records of activity of particular interest. Log preservation is typically performed in support of incident handling or investigations." CNIL ¶9 grounds retention in security that is "essentiellement « active »".
- The **invalidation** rationale for credentials is equally explicit, and equally careful never to say *delete*. NIST 800-63B-4 §5.1: "erased or invalidated". NIST 800-63A-4 §3.8: "Upon its use, the CSP SHALL invalidate". ASVS 7.4.1: "invalidating the session data at the application backend".

CNIL ¶22 comes closest to joining them, by saying the log should not outlive the main processing's own personal data.

So the argument for keeping attempts longer than sessions and tokens has to be made from Corridor's own facts, and it can be. `audit.SIGN_IN` is recorded at session creation (`access.py`), so a **successful** sign-in leaves a durable `audit_log` entry that `identity_audit` exports and that this sweep never touches. A **failed or unknown-email** attempt leaves no audit entry at all: `sign_in_attempts` is the only record that it happened. Deleting a dead session or a spent token therefore destroys no history; deleting an attempt row destroys the only evidence of an access attempt that did not succeed. That is the asymmetry, and it is a property of this codebase rather than a citation.

## 5. Terminology

Nothing here is customer-facing construction language, so this sits in the "ordinary software helper names" part of the procedure — but the published names are worth using instead of inventing any, and two of them are genuinely useful.

| Concept | What Corridor calls it | What published sources call it |
|---|---|---|
| The server-side row a cookie resolves to | `web_sessions`, "session" | NIST 800-63B-4: "session", "session secret", "session binding" (§5.1); SP 800-53 AC-12: "user session"; ASVS 5.0: **"stateful session"** or **"reference token"**, as against a self-contained token (a JWT) — the distinction is worth keeping, because ASVS 7.4.1's backend-invalidation requirement is written specifically for the stateful kind Corridor has |
| The emailed single-use secret | `sign_in_tokens`, "magic link" | NIST 800-63B-4 §3.1.3: **"out-of-band authenticator"**, the secret being an "authentication secret"; NIST 800-63A-4 §3.8: **"confirmation code"**, explicitly deliverable as "a secure (e.g., https) link containing a representation of the confirmation code"; ASVS: "out-of-band authentication requests, codes, or tokens". "Magic link" appears in no standard. Do not borrow NIST's neighbouring **"continuation code"** (800-63A-4 §3.9) — that is a different thing, a secret reconnecting one incomplete proofing session to another |
| The append-only attempt rows | `sign_in_attempts` | NIST 800-53 AU family: **"audit record"**; NIST 800-92: "log entry", "log data"; PCI DSS and CIS: **"audit log"**; CNIL: "journal", **"journaux de connexion"** (connection logs); ASVS frames it as authentication operations logged within security event logging |
| The backoff counter over those rows | `ATTEMPT_WINDOW`, `over_limit` | NIST 800-63B-4 §3.2.2: **"rate limiting (throttling)"**, the counter being the **"retry count"**; OWASP: "login throttling" |
| A `RetentionHold` stopping a sweep | "hold" | NIST SP 800-92 §2.3.2's **"log preservation"** is precisely this act — "keeping logs that normally would be discarded", "in support of incident handling or investigations". Corridor's hold and NIST's preservation are the same concept under two names; the note is recorded so a future reader does not invent a third |

No new Corridor term is proposed. ADR-0102 uses "sign-in record" as a plain collective for the three relations, which is descriptive rather than a coined label, and the module is named `sign_in_retention` after it.

## 6. Sources, and how far each was verified

| Source | Edition / date | How obtained |
|---|---|---|
| NIST SP 800-63B-4, *Authentication and Authenticator Management* | July 2025, final | Primary: official `nvlpubs.nist.gov` PDF, text extracted verbatim |
| NIST SP 800-63A-4, *Identity Proofing and Enrollment* | July 2025, final | Primary, same |
| NIST SP 800-92, *Guide to Computer Security Log Management* | September 2006, final | Primary, same |
| NIST SP 800-92r1 ipd, *Cybersecurity Log Management Planning Guide* | 11 October 2023, initial public draft, no final | Primary, same |
| NIST SP 800-53 Rev 5, controls AU-11 and AC-12 | Rev 5 | Primary, same |
| OWASP ASVS 5.0 | May 2025 | Primary: OWASP GitHub source |
| OWASP Session Management / Authentication / Forgot Password / Logging Cheat Sheets | current at 2026-09-11 | Primary: cheatsheetseries.owasp.org |
| CNIL Délibération n° 2021-122 (journalisation) | adopted 14 October 2021 | Primary: CNIL's own PDF, French text extracted verbatim |
| CNIL, *Sécurité : Tracer les opérations* | updated 14 March 2024 | Primary: cnil.fr |
| CIS Critical Security Controls v8 / v8.1, Safeguard 8.10 | v8 / v8.1 | Secondary reproductions, consistent |
| **PCI DSS v4.0.1, requirements 10.5.1 and 8.2.8** | v4.0.1 | **Secondary only.** PCI SSC gates the PDF behind a click-through terms acceptance, which was not accepted. Multiple consistent secondary reproductions; **verify against a copy of the standard before relying on these two figures** |
| GDPR Articles 5(1)(c) and 5(1)(e) | Regulation (EU) 2016/679 | Primary text |
