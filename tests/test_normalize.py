import pytest

import pandoracle.normalize as normalize_module
from pandoracle.models import SemanticType
from pandoracle.normalize import NormalizedValue, classify_query, normalize, normalize_with


def test_normalizers_are_idempotent_for_canonical_values() -> None:
    cases = {
        SemanticType.EMAIL: "John@Example.COM",
        SemanticType.PHONE: "+38 (050) 123-45-67",
        SemanticType.DOMAIN: "Example.COM.",
        SemanticType.URL: "HTTPS://Example.COM:443/path#fragment",
        SemanticType.USERNAME: "@SomeUser",
        SemanticType.IP: "2001:0db8::1",
        SemanticType.DATE: "14.02.1990",
        SemanticType.PERSON_NAME: "  Ли   Вадим Владимирович  ",
    }
    for semantic_type, source in cases.items():
        first = normalize(semantic_type, source)
        assert first.original == source
        assert first.normalized is not None
        second = normalize(semantic_type, first.normalized)
        assert second.normalized == first.normalized


def test_phone_does_not_invent_country_context() -> None:
    local = normalize(SemanticType.PHONE, "050 123 45 67")
    international = normalize(SemanticType.PHONE, "+380 50 123 45 67")
    assert local.normalized == "0501234567"
    assert international.normalized == "+380501234567"
    assert local.normalized != international.normalized
    assert local.warnings


def test_phone_canonicalizes_unicode_decimal_digits() -> None:
    assert normalize(SemanticType.PHONE, "+٣٨٠ ٥٠ ١٢٣ ٤٥٦٧").normalized == "+380501234567"


def test_idn_domain_and_ipv6_url_are_canonical() -> None:
    assert normalize(SemanticType.DOMAIN, "пример.рф").normalized == "xn--e1afmkfd.xn--p1ai"
    assert (
        normalize(SemanticType.URL, "HTTPS://[2001:0db8::1]:443/a#fragment").normalized
        == "https://[2001:db8::1]/a"
    )


def test_query_classifier_prefers_structured_types() -> None:
    assert classify_query("John@Example.COM") == [(SemanticType.EMAIL, "john@example.com")]
    assert classify_query("192.0.2.1") == [(SemanticType.IP, "192.0.2.1")]
    assert classify_query("Ли Вадим Владимирович") == [
        (SemanticType.PERSON_NAME, "ли вадим владимирович")
    ]


def test_invalid_value_is_not_indexable() -> None:
    assert normalize(SemanticType.EMAIL, "not an email").normalized is None
    assert normalize(SemanticType.IP, "999.1.1.1").normalized is None


def test_normalize_with_does_not_invoke_unrelated_normalizers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = SemanticType.PHONE.value

    def unexpected(value: str) -> NormalizedValue:
        raise AssertionError(f"unrelated normalizer received {value!r}")

    for identifier in normalize_module._NORMALIZERS:
        if identifier != selected:
            monkeypatch.setitem(normalize_module._NORMALIZERS, identifier, unexpected)

    assert normalize_with("phone/v1", 1, "+7 (700) 000-00-42").normalized == "+77000000042"


@pytest.mark.parametrize(
    ("canonicalizer_id", "value", "expected"),
    (
        ("email/v1", "John@Example.COM", "john@example.com"),
        ("phone/v1", "+38 (050) 123-45-67", "+380501234567"),
        ("domain/v1", "Example.COM.", "example.com"),
        ("url/v1", "HTTPS://Example.COM:443/path#fragment", "https://example.com/path"),
        ("username/v1", "@SomeUser", "someuser"),
        ("ip/v1", "2001:0db8::1", "2001:db8::1"),
        ("date/v1", "14.02.1990", "1990-02-14"),
        ("person-name/v1", "  Ли   Вадим Владимирович  ", "ли вадим владимирович"),
        ("exact-text/v1", "  Synthetic-A  ", "synthetic-a"),
    ),
)
def test_cached_canonicalizer_dispatch_preserves_results(
    canonicalizer_id: str, value: str, expected: str
) -> None:
    result = normalize_with(canonicalizer_id, 1, value)
    assert result.original == value
    assert result.normalized == expected
    assert result.normalizer_id == canonicalizer_id


def test_cached_canonicalizer_dispatch_is_immutable() -> None:
    with pytest.raises(TypeError):
        normalize_module._CANONICALIZERS_BY_ID["replacement/v1"] = normalize_module.normalize_email


def test_normalize_with_preserves_validation_errors() -> None:
    with pytest.raises(
        ValueError,
        match=r"^unsupported canonicalizer version: phone/v1/v2$",
    ):
        normalize_with("phone/v1", 2, "+77000000042")
    with pytest.raises(ValueError, match=r"^unsupported canonicalizer: missing/v1$"):
        normalize_with("missing/v1", 1, "value")
