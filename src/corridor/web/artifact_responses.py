"""Which responses hand a customer bytes, declared rather than inferred (#848).

``tests/test_artifact_authorization.py`` proves that every route answering with
artifact bytes refuses the wrong person, and to do that it first has to *find*
those routes. It used to find them by reading the media type written beside
each response construction and calling anything that was not ``text/html``,
``application/json`` or ``text/plain`` a file. Three things were wrong with
that, and all three were live:

* ``text/plain`` and ``application/json`` are artifact media types here.
  ``release_candidate.ARTIFACT_SUFFIXES`` configures ``.txt`` for the accepted
  change summary and the weekly coordination report and ``.json`` for the chase
  list, and ``artifact_downloads.MEDIA_TYPES`` serves those as ``text/plain``
  and ``application/json``. A text report is customer data; so is a JSON one.
* The one route that already serves ``text/plain`` --
  ``read_follow_up_request_content``, which hands back the exact retained
  content of a sent request (#837) -- was classified as an artifact route only
  because it spells its media type ``"text/plain; charset=utf-8"``, and the
  string with the charset on it happened not to match the string without.
  Dropping the parameter would have dropped the route out of the guard.
* A helper that picks its media type from a table writes no literal at all, so
  #830's downloads were invisible to the reading and were reached instead by
  naming their module by hand and treating *every* response it builds as a
  file.

So the classification is declared here and reconciled against the routes the
application actually registers, the way #909 reconciles the frontend receipt
registry against the router rather than trusting a second hand-written copy.
Nothing is inferred from a media type, and nothing the reconciliation cannot
resolve is skipped: an unclassified response construction, a declaration
naming a function that builds none, a name defined twice, a registered route
whose handler this reader cannot find, and a derived route set that disagrees
with ``ARTIFACT_BYTE_ROUTES`` are each a named failure.

"Artifact bytes" means the response body *is* the customer's retained or
rendered file -- a workbook, a PDF, a page image, a registered original, a
retained message, an approved package. A page describing one is not, and
neither is an act's JSON acknowledgement naming an artifact's id and digest:
those carry no retained bytes, and the authorization that governs them is
proved where the act is.
"""

from __future__ import annotations

from collections.abc import Mapping


#: A response class that says what it carries without anything else being
#: known: ``True`` for artifact bytes, ``False`` for a page. A class not
#: listed here has its kind declared per construction site below.
UNAMBIGUOUS_RESPONSE_KINDS: Mapping[str, bool] = {
    "FileResponse": True,
    "HTMLResponse": False,
    "RedirectResponse": False,
    "TemplateResponse": False,
}

#: Functions in ``corridor.web`` that build a response carrying artifact bytes
#: out of a class that could carry either, and what the bytes are. A route
#: reaches artifact bytes when its call path reaches one of these or an
#: unambiguously-artifact class above.
ARTIFACT_RESPONSE_BUILDERS: Mapping[str, str] = {
    "_pdf_response": "the retained PDF of a prepared or released report",
    "_xlsx_download": "the internal working workbook, rendered on request",
    "download_response": (
        "one retained candidate artifact, one sealed approved artifact, or the "
        "bundle of an approved package (#830)"
    ),
    "read_follow_up_request_content": (
        "the exact content a follow-up request was sent with (#837)"
    ),
}

#: Functions in ``corridor.web`` that build a response out of one of those same
#: classes and carry no artifact bytes, and why not. Declared for the same
#: reason as the list above: so that a construction belonging to neither list
#: fails the reconciliation instead of defaulting to "page".
PAGE_RESPONSE_BUILDERS: Mapping[str, str] = {
    "refusal_response": "a declared refusal's own sentence and status",
    "livez": "process liveness",
    "readyz": "readiness, as the operations capability observes it",
    "health": "the deployment's own health, component by component",
    "render_report": (
        "an acknowledgement naming the artifact a render retained: its id, its "
        "name and its digest, and none of its bytes"
    ),
    "release_report": (
        "an acknowledgement naming the release a human authorized: its id, the "
        "artifact's name and its digest, and none of its bytes"
    ),
}

#: The closed vocabulary of route names that answer with artifact bytes. Every
#: name must be one the application serves, and the set must be exactly what
#: reading the response builders above derives -- a new byte-serving route
#: fails the reconciliation until it is written down here, and a name whose
#: route stopped serving bytes fails it too.
ARTIFACT_BYTE_ROUTES: frozenset[str] = frozenset(
    {
        "download_candidate_artifact",
        "download_issue_artifact",
        "download_issue_bundle",
        "download_prepared_report",
        "download_released_report",
        "internal_report_workbook",
        "page_image",
        "preview_prepared_report",
        "read_follow_up_request_content",
        "source_document_original",
    }
)

#: Routes FastAPI registers on the application itself. They are the only
#: registered routes whose handler is not a function of ``corridor.web.app``,
#: and naming them is what lets "a registered route this reader cannot find in
#: the source" stay a failure rather than a silent exclusion.
FRAMEWORK_ROUTES: frozenset[str] = frozenset(
    {"openapi", "swagger_ui_html", "swagger_ui_redirect", "redoc_html"}
)
