from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from pandoracle.errors import ImportFailure
from pandoracle.models import (
    CanonicalFieldSpec,
    FieldSpec,
    SearchOperator,
    SemanticFieldSpec,
    SemanticType,
)
from pandoracle.normalize import BUILTIN_TYPE_IDS, NORMALIZER_VERSION, normalize, normalizer_id

SCHEMA_PLAN_VERSION = 1
CUSTOM_TYPE_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_ALIASES: dict[SemanticType, set[str]] = {
    SemanticType.EMAIL: {"email", "email_address", "e_mail", "mail"},
    SemanticType.PHONE: {
        "phone",
        "phone_number",
        "telephone",
        "mobile",
        "mobile_number",
        "contact_number",
        "телефон",
        "номер_телефона",
        "мобильный_телефон",
        "телефон_нөмірі",
    },
    SemanticType.DOMAIN: {"domain", "domain_name", "website_domain"},
    SemanticType.URL: {"url", "website", "web_url", "homepage"},
    SemanticType.USERNAME: {"username", "user_name", "login", "handle"},
    SemanticType.IP: {"ip", "ip_address", "ipv4", "ipv6"},
    SemanticType.DATE: {"date", "created_date", "updated_date", "дата"},
    SemanticType.DATE_OF_BIRTH: {
        "dob",
        "birth_date",
        "date_of_birth",
        "дата_рождения",
        "туған_күні",
    },
    SemanticType.PERSON_NAME: {
        "full_name",
        "person_name",
        "fio",
        "f_i_o",
        "фио",
        "ф_и_о",
        "полное_имя",
        "аты_жөні",
    },
}


@dataclass(frozen=True)
class TypeCandidate:
    type_id: str
    score: float
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"type_id": self.type_id, "score": self.score, "evidence": list(self.evidence)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TypeCandidate:
        return cls(
            type_id=str(value["type_id"]),
            score=float(value["score"]),
            evidence=tuple(str(item) for item in value.get("evidence", [])),
        )


@dataclass(frozen=True)
class FieldProposal:
    field_id: int
    source_name: str
    proposed_type: str
    candidates: tuple[TypeCandidate, ...]
    selected_type: str | None = None
    selection_source: str | None = None
    canonicalizer_id: str | None = None
    canonicalizer_version: int | None = None
    samples: tuple[str, ...] = ()

    @property
    def score(self) -> float:
        candidate = next(
            (item for item in self.candidates if item.type_id == self.proposed_type), None
        )
        return candidate.score if candidate is not None else 0.0

    @property
    def evidence(self) -> tuple[str, ...]:
        candidate = next(
            (item for item in self.candidates if item.type_id == self.proposed_type), None
        )
        return candidate.evidence if candidate is not None else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_id": self.field_id,
            "source_name": self.source_name,
            "proposed_type": self.proposed_type,
            "candidates": [item.to_dict() for item in self.candidates],
            "selected_type": self.selected_type,
            "selection_source": self.selection_source,
            "canonicalizer_id": self.canonicalizer_id,
            "canonicalizer_version": self.canonicalizer_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FieldProposal:
        return cls(
            field_id=int(value["field_id"]),
            source_name=str(value["source_name"]),
            proposed_type=str(value["proposed_type"]),
            candidates=tuple(TypeCandidate.from_dict(item) for item in value["candidates"]),
            selected_type=(str(value["selected_type"]) if value["selected_type"] else None),
            selection_source=(
                str(value["selection_source"]) if value["selection_source"] else None
            ),
            canonicalizer_id=(
                str(value["canonicalizer_id"]) if value["canonicalizer_id"] else None
            ),
            canonicalizer_version=(
                int(value["canonicalizer_version"])
                if value["canonicalizer_version"] is not None
                else None
            ),
        )


@dataclass(frozen=True)
class CustomTypeSpec:
    type_id: str
    label: str
    normalizer_id: str = "exact-text/v1"
    normalizer_version: int = 1
    default_operator: str = SearchOperator.EXACT.value

    def __post_init__(self) -> None:
        if CUSTOM_TYPE_ID.fullmatch(self.type_id) is None:
            raise ValueError("custom type ID must match [a-z][a-z0-9_]{0,63}")
        if self.type_id.upper() in BUILTIN_TYPE_IDS:
            raise ValueError("custom type ID conflicts with a built-in type")
        if not self.label.strip() or len(self.label) > 128:
            raise ValueError("custom type label must contain between 1 and 128 characters")
        if self.normalizer_id != "exact-text/v1" or self.normalizer_version != 1:
            raise ValueError("only exact-text/v1 custom types are supported")
        if self.default_operator not in {
            SearchOperator.EXACT.value,
            SearchOperator.TOKEN.value,
        }:
            raise ValueError("custom default operator must be EXACT or TOKEN")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CustomTypeSpec:
        return cls(
            type_id=str(value["type_id"]),
            label=str(value["label"]),
            normalizer_id=str(value["normalizer_id"]),
            normalizer_version=int(value["normalizer_version"]),
            default_operator=str(value["default_operator"]).upper(),
        )


@dataclass(frozen=True)
class SchemaPlan:
    headers: tuple[str, ...]
    recipe: dict[str, Any]
    fields: tuple[FieldProposal, ...]
    custom_types: tuple[CustomTypeSpec, ...] = ()
    confirmed: bool = False
    schema_plan_version: int = SCHEMA_PLAN_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_plan_version": self.schema_plan_version,
            "confirmed": self.confirmed,
            "headers": list(self.headers),
            "recipe": self.recipe,
            "fields": [field.to_dict() for field in self.fields],
            "custom_types": [item.to_dict() for item in self.custom_types],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SchemaPlan:
        version = int(value.get("schema_plan_version", -1))
        if version != SCHEMA_PLAN_VERSION:
            raise ImportFailure(
                "unsupported schema plan; regenerate it with 'pandoracle schema analyze'"
            )
        fields = tuple(FieldProposal.from_dict(item) for item in value["fields"])
        plan = cls(
            headers=tuple(str(item) for item in value["headers"]),
            recipe=dict(value["recipe"]),
            fields=fields,
            custom_types=tuple(CustomTypeSpec.from_dict(item) for item in value["custom_types"]),
            confirmed=bool(value["confirmed"]),
            schema_plan_version=SCHEMA_PLAN_VERSION,
        )
        validate_plan_shape(plan)
        return plan


def _canonical_header(header: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^\w]+", "_", header.strip().casefold())).strip("_")


def infer_header_type(header: str) -> SemanticType | None:
    canonical = _canonical_header(header)
    return next((kind for kind, aliases in _ALIASES.items() if canonical in aliases), None)


def infer_proposals(
    headers: Sequence[str], sample_rows: Sequence[Sequence[str]]
) -> tuple[FieldProposal, ...]:
    proposals: list[FieldProposal] = []
    for field_id, source_name in enumerate(headers):
        values = [
            row[field_id] for row in sample_rows if field_id < len(row) and row[field_id].strip()
        ]
        header_match = infer_header_type(source_name)
        candidates: list[TypeCandidate] = []
        ratios: dict[str, float] = {}
        if values:
            for kind in SemanticType:
                if kind in {SemanticType.UNKNOWN, SemanticType.USERNAME}:
                    continue
                ratio = sum(
                    normalize(kind, value).normalized is not None for value in values
                ) / len(values)
                ratios[kind.value] = ratio
                if ratio > 0:
                    candidates.append(
                        TypeCandidate(kind.value, ratio, (f"sample validator: {ratio:.0%}",))
                    )

        proposed = SemanticType.UNKNOWN.value
        if header_match is not None:
            ratio = ratios.get(header_match.value, 0.0)
            if not values or ratio >= 0.5:
                score = 0.95 if ratio >= 0.8 else 0.85
                evidence = [f"header alias: {_canonical_header(source_name)}"]
                if values:
                    evidence.append(f"sample validator: {ratio:.0%}")
                candidates = [item for item in candidates if item.type_id != header_match.value]
                candidates.append(TypeCandidate(header_match.value, score, tuple(evidence)))
                proposed = header_match.value
            else:
                candidates.append(
                    TypeCandidate(
                        SemanticType.UNKNOWN.value,
                        1.0,
                        ("header/value conflict; proposed untyped",),
                    )
                )
        elif values:
            eligible = [
                item
                for item in candidates
                if item.type_id not in {SemanticType.PHONE.value, SemanticType.PERSON_NAME.value}
            ]
            if eligible:
                best = max(eligible, key=lambda item: item.score)
                if best.score >= 0.9:
                    proposed = best.type_id

        if not any(item.type_id == proposed for item in candidates):
            candidates.append(TypeCandidate(proposed, 0.0, ("no reliable semantic evidence",)))
        candidates.sort(key=lambda item: (-item.score, item.type_id))
        proposals.append(
            FieldProposal(
                field_id=field_id,
                source_name=source_name,
                proposed_type=proposed,
                candidates=tuple(candidates),
                samples=tuple(value[:120] for value in values[:3]),
                canonicalizer_id=normalizer_id(proposed),
                canonicalizer_version=(
                    None if proposed == SemanticType.UNKNOWN.value else NORMALIZER_VERSION
                ),
            )
        )
    return tuple(proposals)


def make_draft_plan(
    headers: Sequence[str], sample_rows: Sequence[Sequence[str]], recipe: Mapping[str, Any]
) -> SchemaPlan:
    return SchemaPlan(tuple(headers), dict(recipe), infer_proposals(headers, sample_rows))


def confirm_plan(
    plan: SchemaPlan,
    selections: Mapping[int, str] | None = None,
    *,
    selection_source: str = "user_confirmed",
    custom_types: Sequence[CustomTypeSpec] | None = None,
) -> SchemaPlan:
    selections = selections or {}
    custom = tuple(custom_types) if custom_types is not None else plan.custom_types

    def confirmed_field(field: FieldProposal) -> FieldProposal:
        selected_type = selections.get(field.field_id, field.selected_type or field.proposed_type)
        return replace(
            field,
            selected_type=selected_type,
            selection_source=selection_source,
            canonicalizer_id=normalizer_id(selected_type),
            canonicalizer_version=(
                None if selected_type == SemanticType.UNKNOWN.value else NORMALIZER_VERSION
            ),
        )

    fields = tuple(confirmed_field(field) for field in plan.fields)
    result = replace(
        plan,
        fields=fields,
        custom_types=custom,
        confirmed=True,
    )
    validate_plan_shape(result)
    return result


def validate_plan_shape(plan: SchemaPlan) -> None:
    if len(plan.headers) != len(plan.fields):
        raise ImportFailure("schema plan field count does not match headers")
    for expected, field in enumerate(plan.fields):
        if field.field_id != expected or field.source_name != plan.headers[expected]:
            raise ImportFailure("schema plan fields do not match ordered headers")
    custom_ids = [item.type_id for item in plan.custom_types]
    if len(custom_ids) != len(set(custom_ids)):
        raise ImportFailure("schema plan contains duplicate custom type IDs")


def validate_plan_for_import(
    plan: SchemaPlan,
    headers: Sequence[str],
    recipe: Mapping[str, Any],
    available_custom_types: Mapping[str, CustomTypeSpec],
) -> list[FieldSpec]:
    validate_plan_shape(plan)
    if not plan.confirmed or any(field.selected_type is None for field in plan.fields):
        raise ImportFailure("schema plan is not fully confirmed")
    if tuple(headers) != plan.headers:
        raise ImportFailure("schema plan headers do not match the source")
    recipe_keys = ("format", "encoding", "delimiter", "quotechar", "escapechar", "doublequote")
    if any(plan.recipe.get(key) != recipe.get(key) for key in recipe_keys):
        raise ImportFailure("schema plan parse recipe does not match the source")

    declared = {item.type_id: item for item in plan.custom_types}
    for identifier, definition in declared.items():
        existing = available_custom_types.get(identifier)
        if existing is not None and existing != definition:
            raise ImportFailure(f"custom type conflicts with workspace definition: {identifier}")
    allowed = BUILTIN_TYPE_IDS | set(available_custom_types) | set(declared)
    fields: list[FieldSpec] = []
    for proposal in plan.fields:
        selected = str(proposal.selected_type)
        if selected not in allowed:
            raise ImportFailure(f"unknown semantic type in schema plan: {selected}")
        expected_canonicalizer = normalizer_id(selected)
        expected_version = None if expected_canonicalizer is None else NORMALIZER_VERSION
        if (
            proposal.canonicalizer_id != expected_canonicalizer
            or proposal.canonicalizer_version != expected_version
        ):
            raise ImportFailure(
                f"schema plan canonicalizer snapshot does not match semantic type: {selected}"
            )
        fields.append(
            FieldSpec(
                field_id=proposal.field_id,
                source_name=proposal.source_name,
                storage_name=f"f_{proposal.field_id:04d}",
                normalized_storage_name=f"n_{proposal.field_id:04d}",
                semantic_type=selected,
                inference_score=proposal.score,
                inference_evidence=proposal.evidence,
                normalizer_id=normalizer_id(selected),
                normalizer_version=(
                    None if selected == SemanticType.UNKNOWN.value else NORMALIZER_VERSION
                ),
                selection_source=proposal.selection_source or "user_confirmed",
            )
        )
    return fields


def build_layer_specs(
    plan: SchemaPlan,
    headers: Sequence[str],
    recipe: Mapping[str, Any],
    available_custom_types: Mapping[str, CustomTypeSpec],
) -> tuple[list[SemanticFieldSpec], list[CanonicalFieldSpec]]:
    from pandoracle.contracts import contract_for

    resolved = validate_plan_for_import(plan, headers, recipe, available_custom_types)
    contracts = {**available_custom_types, **{item.type_id: item for item in plan.custom_types}}
    semantic = [
        SemanticFieldSpec(
            field.field_id,
            field.source_name,
            field.storage_name,
            field.semantic_type,
            field.inference_score,
            field.inference_evidence,
            field.selection_source,
        )
        for field in resolved
    ]
    canonical = []
    for field in resolved:
        if field.normalizer_id is None or field.normalizer_version is None:
            continue
        contract = contract_for(field.semantic_type, contracts)
        canonical.append(
            CanonicalFieldSpec(
                field.field_id,
                field.storage_name,
                field.normalized_storage_name,
                field.semantic_type,
                str(field.normalizer_id),
                int(field.normalizer_version),
                tuple(item.value for item in contract.supported_operators),
                str(contract.default_operator.value),
                contract.tokenizer_id,
                contract.tokenizer_version,
                contract.ordering,
            )
        )
    return semantic, canonical


def semantic_fingerprint(fields: Sequence[SemanticFieldSpec], recipe: Mapping[str, Any]) -> str:
    return _fingerprint(
        {
            "fields": [
                {
                    "field_id": field.field_id,
                    "source_name": field.source_name,
                    "storage_name": field.storage_name,
                    "semantic_type": field.semantic_type,
                }
                for field in fields
            ],
            "recipe": dict(recipe),
        }
    )


def canonical_fingerprint(dataset_version_id: str, fields: Sequence[CanonicalFieldSpec]) -> str:
    return _fingerprint(
        {"dataset_version_id": dataset_version_id, "fields": [field.to_dict() for field in fields]}
    )


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_schema_document(path: Path) -> SchemaPlan:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ImportFailure(f"invalid schema file: {error}") from error
    if not isinstance(value, dict):
        raise ImportFailure("schema file must contain a JSON object")
    if "schema_plan_version" not in value:
        raise ImportFailure(
            "unsupported schema file; generate it with 'pandoracle schema analyze'"
        )
    try:
        return SchemaPlan.from_dict(value)
    except ImportFailure:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise ImportFailure(
            "invalid schema plan; regenerate it with 'pandoracle schema analyze'"
        ) from error
