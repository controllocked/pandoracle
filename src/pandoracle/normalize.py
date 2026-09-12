from __future__ import annotations

import ipaddress
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from urllib.parse import SplitResult, urlsplit, urlunsplit

from pandoracle.models import SemanticType, type_id

NORMALIZER_VERSION = 1
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True)
class NormalizedValue:
    original: str
    normalized: str | None
    normalizer_id: str
    normalizer_version: int = NORMALIZER_VERSION
    warnings: tuple[str, ...] = ()


def _text(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip())


def normalize_email(value: str) -> NormalizedValue:
    original = value
    candidate = _text(value)
    if not _EMAIL.fullmatch(candidate):
        return NormalizedValue(original, None, "email/v1", warnings=("invalid email",))
    local, domain = candidate.rsplit("@", 1)
    try:
        ascii_domain = domain.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return NormalizedValue(original, None, "email/v1", warnings=("invalid domain",))
    return NormalizedValue(original, f"{local.casefold()}@{ascii_domain}", "email/v1")


def normalize_phone(value: str) -> NormalizedValue:
    original = value
    candidate = _text(value)
    has_international_prefix = candidate.startswith("+") or candidate.startswith("00")
    digits = "".join(
        str(unicodedata.decimal(character)) for character in candidate if character.isdecimal()
    )
    if candidate.startswith("00"):
        digits = digits[2:]
    if not 7 <= len(digits) <= 15:
        return NormalizedValue(original, None, "phone/v1", warnings=("invalid digit count",))
    normalized = f"+{digits}" if has_international_prefix else digits
    warning = () if has_international_prefix else ("country context not inferred",)
    return NormalizedValue(original, normalized, "phone/v1", warnings=warning)


def normalize_domain(value: str) -> NormalizedValue:
    original = value
    candidate = _text(value).rstrip(".")
    try:
        ascii_domain = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return NormalizedValue(original, None, "domain/v1", warnings=("invalid domain",))
    labels = ascii_domain.split(".")
    valid_labels = all(
        1 <= len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    )
    valid_tld = len(labels[-1]) >= 2 or labels[-1].startswith("xn--")
    if len(ascii_domain) > 253 or len(labels) < 2 or not valid_labels or not valid_tld:
        return NormalizedValue(original, None, "domain/v1", warnings=("invalid domain",))
    return NormalizedValue(original, ascii_domain, "domain/v1")


def normalize_url(value: str) -> NormalizedValue:
    original = value
    candidate = _text(value)
    try:
        parsed = urlsplit(candidate)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return NormalizedValue(original, None, "url/v1", warnings=("invalid HTTP URL",))
    try:
        host_ip = ipaddress.ip_address(hostname)
        host_for_url = f"[{host_ip.compressed}]" if host_ip.version == 6 else host_ip.compressed
    except ValueError:
        host_for_url = hostname
    if port is None or (parsed.scheme.lower(), port) in {("http", 80), ("https", 443)}:
        netloc = host_for_url
    else:
        netloc = f"{host_for_url}:{port}"
    normalized = urlunsplit(
        SplitResult(parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
    )
    return NormalizedValue(original, normalized, "url/v1")


def normalize_username(value: str) -> NormalizedValue:
    original = value
    candidate = _text(value)
    if candidate.startswith("@"):
        candidate = candidate[1:]
    if not candidate or any(character.isspace() for character in candidate):
        return NormalizedValue(original, None, "username/v1", warnings=("invalid username",))
    return NormalizedValue(original, candidate.casefold(), "username/v1")


def normalize_ip(value: str) -> NormalizedValue:
    original = value
    try:
        candidate = ipaddress.ip_address(_text(value))
    except ValueError:
        return NormalizedValue(original, None, "ip/v1", warnings=("invalid IP",))
    return NormalizedValue(original, candidate.compressed, "ip/v1")


def normalize_date(value: str) -> NormalizedValue:
    original = value
    candidate = _text(value)
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(candidate, pattern).date()
            return NormalizedValue(original, parsed.isoformat(), "date/v1")
        except ValueError:
            continue
    return NormalizedValue(original, None, "date/v1", warnings=("ambiguous or invalid date",))


def normalize_person_name(value: str) -> NormalizedValue:
    """Conservatively normalize a name for exact matching, never fuzzy matching."""
    original = value
    candidate = " ".join(_text(value).split())
    if not candidate or len(candidate) > 512:
        return NormalizedValue(original, None, "person-name/v1", warnings=("invalid name",))
    allowed_punctuation = {"-", "'", "’", "."}
    has_letter = False
    for character in candidate:
        category = unicodedata.category(character)
        if category.startswith("L"):
            has_letter = True
        elif not (
            category.startswith("M") or character.isspace() or character in allowed_punctuation
        ):
            return NormalizedValue(
                original,
                None,
                "person-name/v1",
                warnings=("name contains unsupported characters",),
            )
    if not has_letter:
        return NormalizedValue(original, None, "person-name/v1", warnings=("invalid name",))
    return NormalizedValue(original, candidate.casefold(), "person-name/v1")


def normalize_exact_text(value: str) -> NormalizedValue:
    original = value
    candidate = _text(value)
    if not candidate:
        return NormalizedValue(original, None, "exact-text/v1", warnings=("empty value",))
    return NormalizedValue(original, candidate.casefold(), "exact-text/v1")


_NORMALIZERS = {
    SemanticType.EMAIL.value: normalize_email,
    SemanticType.PHONE.value: normalize_phone,
    SemanticType.DOMAIN.value: normalize_domain,
    SemanticType.URL.value: normalize_url,
    SemanticType.USERNAME.value: normalize_username,
    SemanticType.IP.value: normalize_ip,
    SemanticType.DATE.value: normalize_date,
    SemanticType.DATE_OF_BIRTH.value: normalize_date,
    SemanticType.PERSON_NAME.value: normalize_person_name,
}
_CANONICALIZERS_BY_ID: Mapping[str, Callable[[str], NormalizedValue]] = MappingProxyType(
    {
        "email/v1": normalize_email,
        "phone/v1": normalize_phone,
        "domain/v1": normalize_domain,
        "url/v1": normalize_url,
        "username/v1": normalize_username,
        "ip/v1": normalize_ip,
        "date/v1": normalize_date,
        "person-name/v1": normalize_person_name,
        "exact-text/v1": normalize_exact_text,
    }
)
BUILTIN_TYPE_IDS = frozenset(item.value for item in SemanticType)


def normalize(semantic_type: str | SemanticType, value: str) -> NormalizedValue:
    identifier = type_id(semantic_type)
    if identifier == SemanticType.UNKNOWN.value:
        return NormalizedValue(value, None, "none/v1")
    normalizer = _NORMALIZERS.get(identifier)
    if normalizer is not None:
        return normalizer(value)
    return normalize_exact_text(value)


def normalizer_id(semantic_type: str | SemanticType) -> str | None:
    identifier = type_id(semantic_type)
    if identifier == SemanticType.UNKNOWN.value:
        return None
    normalizer = _NORMALIZERS.get(identifier)
    return normalizer("").normalizer_id if normalizer is not None else "exact-text/v1"


def normalize_with(
    canonicalizer_id: str, canonicalizer_version: int, value: str
) -> NormalizedValue:
    """Apply a canonicalizer snapshot without consulting index policy."""
    if canonicalizer_version != 1:
        raise ValueError(
            f"unsupported canonicalizer version: {canonicalizer_id}/v{canonicalizer_version}"
        )
    function = _CANONICALIZERS_BY_ID.get(canonicalizer_id)
    if function is None:
        raise ValueError(f"unsupported canonicalizer: {canonicalizer_id}")
    return function(value)


def classify_query(value: str) -> list[tuple[SemanticType, str]]:
    ordered: list[SemanticType]
    candidate = _text(value)
    if "://" in candidate:
        ordered = [SemanticType.URL]
    elif "@" in candidate and "." in candidate.rsplit("@", 1)[-1]:
        ordered = [SemanticType.EMAIL]
    else:
        try:
            ipaddress.ip_address(candidate)
            ordered = [SemanticType.IP]
        except ValueError:
            if normalize_date(candidate).normalized is not None:
                ordered = [SemanticType.DATE]
            elif any(character.isspace() for character in candidate) and (
                normalize_person_name(candidate).normalized is not None
            ):
                ordered = [SemanticType.PERSON_NAME]
            elif normalize_phone(candidate).normalized is not None:
                ordered = [SemanticType.PHONE]
            elif "." in candidate and " " not in candidate:
                ordered = [SemanticType.DOMAIN, SemanticType.USERNAME]
            else:
                ordered = [SemanticType.USERNAME]
    results = []
    for semantic_type in ordered:
        normalized = normalize(semantic_type, value).normalized
        if normalized is not None:
            results.append((semantic_type, normalized))
    return results
