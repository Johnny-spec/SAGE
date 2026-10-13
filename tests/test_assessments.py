import pytest
from pydantic import ValidationError

from sage.assessments import (
    Assessment,
    Citation,
    ConfidenceFactor,
    ContentLocator,
    EvidenceEnvelope,
    Provenance,
)


def _provenance() -> Provenance:
    return Provenance(
        produced_by="contract-risk-skill",
        implementation_version="1.0.0",
        generation_id="gen_1",
        solution_release_id="contract-review@1.0.0",
    )


def test_evidence_envelope_links_assessment_to_precise_citation() -> None:
    citation = Citation(
        citation_id="cit_1",
        artifact_id="art_1",
        locator=ContentLocator(kind="page", value="12"),
        quoted_text="Payment is due in 30 days.",
    )
    assessment = Assessment(
        assessment_id="asm_1",
        status="risk",
        summary="Payment timing requires review.",
        confidence=0.8,
        confidence_factors=(
            ConfidenceFactor(
                name="source-quality",
                value=1.0,
                reason="Quoted directly from the uploaded artifact.",
            ),
        ),
        citation_refs=(citation.citation_id,),
        provenance=_provenance(),
    )

    envelope = EvidenceEnvelope(citations=(citation,), assessments=(assessment,))

    assert envelope.assessments[0].citation_refs == ("cit_1",)


def test_evidence_envelope_rejects_unknown_citation_ref() -> None:
    with pytest.raises(ValidationError, match="Unknown citation refs"):
        EvidenceEnvelope(
            assessments=(
                Assessment(
                    assessment_id="asm_1",
                    status="unknown",
                    summary="Insufficient evidence.",
                    citation_refs=("missing",),
                    provenance=_provenance(),
                ),
            )
        )


def test_numeric_confidence_requires_explainable_factors() -> None:
    with pytest.raises(ValidationError, match="confidence_factors"):
        Assessment(
            assessment_id="asm_1",
            status="risk",
            summary="Potential issue.",
            confidence=0.9,
            provenance=_provenance(),
        )