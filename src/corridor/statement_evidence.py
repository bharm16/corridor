"""Verify one cited statement against its registered project page.

Statement writers validate before persisting and publication readers validate
again before exposing provenance.  This module owns that shared fact without
making either reader depend on the other's implementation.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import DocPage, Document
from corridor.statement_values import CitedStatementEvidence, StatementRefusal
from corridor.verify import quote_appears_on, threshold_for


def validate_cited_statement_evidence(
    session: Session, evidence: CitedStatementEvidence, project_id: int
) -> None:
    """Refuse a citation that cannot be verified inside the stated project."""
    if evidence.page_no < 1 or not evidence.quote.strip():
        raise StatementRefusal("cited Evidence needs a page and quote")
    document = session.get(Document, evidence.document_id)
    if document is None or document.project_id != project_id:
        raise StatementRefusal("cited Evidence belongs to another project")
    page = session.scalar(
        select(DocPage).where(
            DocPage.document_id == document.id,
            DocPage.page_no == evidence.page_no,
        )
    )
    if page is None:
        raise StatementRefusal("cited Evidence page is not registered")
    if not quote_appears_on(
        evidence.quote, page.text, threshold_for(page.text_source)
    ):
        raise StatementRefusal(
            "cited Evidence quote was not found on its registered page"
        )
