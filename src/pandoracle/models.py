from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class SemanticType(StrEnum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    DOMAIN = "DOMAIN"
    URL = "URL"
    USERNAME = "USERNAME"
    IP = "IP"
    DATE = "DATE"
    DATE_OF_BIRTH = "DATE_OF_BIRTH"
    PERSON_NAME = "PERSON_NAME"
    UNKNOWN = "UNKNOWN"


class AccelerationKind(StrEnum):
    VALUE = "VALUE"
    TOKEN = "TOKEN"


class SearchOperator(StrEnum):
    AUTO = "AUTO"
    EXACT = "EXACT"
    TOKEN = "TOKEN"
    RANGE = "RANGE"


def type_id(value: str | SemanticType) -> str:
    return value.value if isinstance(value, SemanticType) else str(value)


class OperationStatus(StrEnum):
    PLANNED = "PLANNED"
    COPYING_RAW = "COPYING_RAW"
    ANALYZING = "ANALYZING"
    TRANSFORMING = "TRANSFORMING"
    INDEXING = "INDEXING"
    VALIDATING = "VALIDATING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


@dataclass(frozen=True)
class WorkspaceFormat:
    major: int = 1
    minor: int = 0


@dataclass(frozen=True)
class WorkspaceManifest:
    manifest_schema_version: int
    workspace_id: str
    format: WorkspaceFormat
    created_at: str
    created_by: str
    minimum_reader_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> WorkspaceManifest:
        required = {
            "manifest_schema_version",
            "workspace_id",
            "format",
            "created_at",
            "created_by",
            "minimum_reader_version",
        }
        if set(value) != required or not isinstance(value["format"], dict):
            raise ValueError("workspace manifest has an unknown or incomplete schema")
        return cls(
            manifest_schema_version=int(value["manifest_schema_version"]),
            workspace_id=str(value["workspace_id"]),
            format=WorkspaceFormat(**value["format"]),
            created_at=str(value["created_at"]),
            created_by=str(value["created_by"]),
            minimum_reader_version=str(value["minimum_reader_version"]),
        )


@dataclass(frozen=True)
class FieldSpec:
    field_id: int
    source_name: str
    storage_name: str
    normalized_storage_name: str
    semantic_type: str
    inference_score: float
    inference_evidence: tuple[str, ...] = ()
    normalizer_id: str | None = None
    normalizer_version: int | None = None
    selection_source: str = "user_confirmed"

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_type", type_id(self.semantic_type))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["inference_evidence"] = list(self.inference_evidence)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FieldSpec:
        return cls(
            field_id=int(value["field_id"]),
            source_name=str(value["source_name"]),
            storage_name=str(value["storage_name"]),
            normalized_storage_name=str(value["normalized_storage_name"]),
            semantic_type=str(value["semantic_type"]),
            inference_score=float(value["inference_score"]),
            inference_evidence=tuple(value["inference_evidence"]),
            normalizer_id=value["normalizer_id"],
            normalizer_version=value["normalizer_version"],
            selection_source=str(value["selection_source"]),
        )


@dataclass(frozen=True)
class SemanticFieldSpec:
    field_id: int
    source_name: str
    storage_name: str
    semantic_type: str
    inference_score: float
    inference_evidence: tuple[str, ...] = ()
    selection_source: str = "user_confirmed"

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_type", type_id(self.semantic_type))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["inference_evidence"] = list(self.inference_evidence)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SemanticFieldSpec:
        return cls(
            field_id=int(value["field_id"]),
            source_name=str(value["source_name"]),
            storage_name=str(value["storage_name"]),
            semantic_type=str(value["semantic_type"]),
            inference_score=float(value["inference_score"]),
            inference_evidence=tuple(value["inference_evidence"]),
            selection_source=str(value["selection_source"]),
        )


@dataclass(frozen=True)
class CanonicalFieldSpec:
    field_id: int
    storage_name: str
    canonical_storage_name: str
    semantic_type: str
    canonicalizer_id: str
    canonicalizer_version: int
    supported_operators: tuple[str, ...] = (SearchOperator.EXACT.value,)
    default_operator: str = SearchOperator.EXACT.value
    tokenizer_id: str | None = None
    tokenizer_version: int | None = None
    ordering: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_type", type_id(self.semantic_type))
        object.__setattr__(
            self, "supported_operators", tuple(str(item) for item in self.supported_operators)
        )
        if self.default_operator not in self.supported_operators:
            raise ValueError("default operator must be supported by the canonical field")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> CanonicalFieldSpec:
        field_id = int(value["field_id"])
        return cls(
            field_id=field_id,
            storage_name=str(value["storage_name"]),
            canonical_storage_name=str(value["canonical_storage_name"]),
            semantic_type=str(value["semantic_type"]),
            canonicalizer_id=str(value["canonicalizer_id"]),
            canonicalizer_version=int(value["canonicalizer_version"]),
            supported_operators=tuple(value["supported_operators"]),
            default_operator=str(value["default_operator"]),
            tokenizer_id=value["tokenizer_id"],
            tokenizer_version=(
                int(value["tokenizer_version"])
                if value["tokenizer_version"] is not None
                else None
            ),
            ordering=value["ordering"],
        )


@dataclass(frozen=True)
class FieldAccelerationPolicy:
    field_id: int
    semantic_type: str
    accelerations: tuple[AccelerationKind | str, ...] = ()
    proposal_source: str = "built_in"
    selection_source: str = "user_confirmed"

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_type", type_id(self.semantic_type))
        normalized = tuple(sorted({AccelerationKind(item) for item in self.accelerations}, key=str))
        object.__setattr__(self, "accelerations", normalized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "semantic_type": self.semantic_type,
            "accelerations": [item.value for item in self.accelerations],
            "proposal_source": self.proposal_source,
            "selection_source": self.selection_source,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FieldAccelerationPolicy:
        return cls(
            field_id=int(value["field_id"]),
            semantic_type=str(value["semantic_type"]),
            accelerations=tuple(str(item) for item in value["accelerations"]),
            proposal_source=str(value["proposal_source"]),
            selection_source=str(value["selection_source"]),
        )


@dataclass(frozen=True)
class RecordRef:
    dataset_version_id: str
    record_ordinal: int

    @property
    def external_id(self) -> str:
        return f"{self.dataset_version_id}:{self.record_ordinal}"


@dataclass(frozen=True)
class SearchClue:
    semantic_type: str | SemanticType
    value: str
    operator: SearchOperator | str = SearchOperator.AUTO

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_type", type_id(self.semantic_type))
        object.__setattr__(self, "operator", SearchOperator(self.operator))
        if not self.value.strip():
            raise ValueError("search clue value cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "semantic_type": self.semantic_type,
            "value": self.value,
            "operator": self.operator.value,
        }


@dataclass(frozen=True)
class SearchRequest:
    clues: tuple[SearchClue, ...]
    dataset: str | None = None
    limit: int = 50
    allow_expensive_scan: bool = False

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "clues": [clue.to_dict() for clue in self.clues],
            "limit": self.limit,
            "allow_expensive_scan": self.allow_expensive_scan,
        }
        if self.dataset is not None:
            value["dataset"] = self.dataset
        return value


@dataclass(frozen=True)
class Provenance:
    dataset_id: str
    dataset_name: str
    dataset_version_id: str
    source_sha256: str
    source_name: str
    record_ordinal: int
    field_id: int
    field_name: str
    original_value: str
    normalized_value: str
    semantic_type: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_type", type_id(self.semantic_type))

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return value


@dataclass(frozen=True)
class CandidateMatch:
    clue_index: int
    semantic_type: str
    mode: SearchOperator | str
    access: str
    provenances: tuple[Provenance, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "semantic_type", type_id(self.semantic_type))
        value = self.mode.value if isinstance(self.mode, SearchOperator) else str(self.mode)
        object.__setattr__(self, "mode", SearchOperator(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "clue_index": self.clue_index,
            "semantic_type": self.semantic_type,
            "mode": self.mode.value,
            "access": self.access,
            "fields": [item.to_dict() for item in self.provenances],
        }


@dataclass(frozen=True)
class SearchCandidate:
    ref: RecordRef
    dataset_id: str
    dataset_name: str
    dataset_version_id: str
    source_name: str
    source_sha256: str
    matches: tuple[CandidateMatch, ...]
    record: dict[str, str | None] = field(default_factory=dict)
    fields: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_ref": self.ref.external_id,
            "dataset_id": self.dataset_id,
            "dataset_name": self.dataset_name,
            "dataset_version_id": self.dataset_version_id,
            "source_name": self.source_name,
            "source_sha256": self.source_sha256,
            "matches": [item.to_dict() for item in self.matches],
            "record": self.record,
            "fields": list(self.fields),
        }


@dataclass(frozen=True)
class SearchResult:
    request: SearchRequest
    records: tuple[SearchCandidate, ...]
    truncated: bool
    execution: dict[str, Any]
    result_version: int = 1

    def to_dict(self, *, explain: bool = False) -> dict[str, Any]:
        value = {
            "result_version": self.result_version,
            "query": self.request.to_dict(),
            "records": [record.to_dict() for record in self.records],
            "truncated": self.truncated,
        }
        if explain:
            value["execution"] = self.execution
        return value
