"""Optional evidence, provenance, and assessment contracts for Solution outputs."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContentLocator(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "artifact",
        "page",
        "region",
        "paragraph",
        "table-cell",
        "line",
        "record",
        "url-fragment",
        "code-range",
    ]
    value: str = Field(min_length=1, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    citation_id: str = Field(min_length=1)
    artifact_id: str | None = None
    source_uri: str | None = None
    source_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    locator: ContentLocator
    quoted_text: str | None = Field(default=None, max_length=20_000)
    captured_at: datetime | None = None

    @model_validator(mode="after")
    def require_source(self) -> "Citation":
        if self.artifact_id is None and self.source_uri is None:
            raise ValueError("Citation requires artifact_id or source_uri")
        return self


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    produced_by: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    generation_id: str = Field(min_length=1)
    solution_release_id: str | None = None
    input_refs: tuple[str, ...] = ()
    invocation_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ConfidenceFactor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    value: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=2000)


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assessment_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=20_000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    confidence_factors: tuple[ConfidenceFactor, ...] = ()
    citation_refs: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    contradictions: tuple[str, ...] = ()
    provenance: Provenance

    @model_validator(mode="after")
    def require_confidence_basis(self) -> "Assessment":
        if self.confidence is not None and not self.confidence_factors:
            raise ValueError("Numeric confidence requires confidence_factors")
        return self


class EvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    citations: tuple[Citation, ...] = ()
    assessments: tuple[Assessment, ...] = ()

    @model_validator(mode="after")
    def validate_citation_refs(self) -> "EvidenceEnvelope":
        citation_ids = {citation.citation_id for citation in self.citations}
        if len(citation_ids) != len(self.citations):
            raise ValueError("Citation ids must be unique")
        unknown = {
            citation_ref
            for assessment in self.assessments
            for citation_ref in assessment.citation_refs
            if citation_ref not in citation_ids
        }
        if unknown:
            raise ValueError(f"Unknown citation refs: {', '.join(sorted(unknown))}")
        return self