from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from pandoracle.models import SearchOperator, SemanticType, type_id
from pandoracle.normalize import NORMALIZER_VERSION, normalizer_id


class CustomContract(Protocol):
    @property
    def normalizer_id(self) -> str: ...

    @property
    def normalizer_version(self) -> int: ...

    @property
    def default_operator(self) -> str: ...


@dataclass(frozen=True)
class SemanticContract:
    type_id: str
    canonicalizer_id: str | None
    canonicalizer_version: int | None
    supported_operators: tuple[SearchOperator, ...]
    default_operator: SearchOperator | None
    tokenizer_id: str | None = None
    tokenizer_version: int | None = None
    ordering: str | None = None


def builtin_contract(identifier: str | SemanticType) -> SemanticContract:
    value = type_id(identifier)
    canonicalizer = normalizer_id(value)
    if value == SemanticType.UNKNOWN.value:
        return SemanticContract(value, None, None, (), None)
    if value == SemanticType.PERSON_NAME.value:
        return SemanticContract(
            value,
            canonicalizer,
            NORMALIZER_VERSION,
            (SearchOperator.EXACT, SearchOperator.TOKEN),
            SearchOperator.TOKEN,
            tokenizer_id="unicode-whitespace/v1",
            tokenizer_version=1,
        )
    if value in {SemanticType.DATE.value, SemanticType.DATE_OF_BIRTH.value}:
        return SemanticContract(
            value,
            canonicalizer,
            NORMALIZER_VERSION,
            (SearchOperator.EXACT, SearchOperator.RANGE),
            SearchOperator.EXACT,
            ordering="binary-lexical/v1",
        )
    return SemanticContract(
        value,
        canonicalizer,
        NORMALIZER_VERSION,
        (SearchOperator.EXACT,),
        SearchOperator.EXACT,
    )


def contract_for(
    identifier: str | SemanticType,
    custom_types: Mapping[str, CustomContract] | None = None,
) -> SemanticContract:
    value = type_id(identifier)
    if value in {item.value for item in SemanticType}:
        return builtin_contract(value)
    custom = (custom_types or {}).get(value)
    if custom is None:
        raise ValueError(f"unknown semantic type: {value}")
    default = SearchOperator(custom.default_operator)
    supported = (SearchOperator.EXACT, SearchOperator.TOKEN)
    if default not in supported:
        raise ValueError(f"unsupported custom default operator: {default.value}")
    return SemanticContract(
        value,
        custom.normalizer_id,
        custom.normalizer_version,
        supported,
        default,
        tokenizer_id="unicode-whitespace/v1",
        tokenizer_version=1,
    )
