"""Testes da normalização de telefone — Onda 2."""
from __future__ import annotations

import pytest

from app.services.phone import looks_like_brazilian_mobile, normalize_phone


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+5500000000000", "+5500000000000"),
        ("+5500000000000", "+5500000000000"),
        ("+5500000000000", "+5500000000000"),
        ("5500000000000", "+5500000000000"),
        ("+5500000000000", "+5500000000000"),
        ("+5500000000000", "+5500000000000"),
        ("+5500000000000", "+5500000000000"),
    ],
)
def test_normalize_aceita(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "", "12", "999"])
def test_normalize_rejeita(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_phone(raw)


def test_looks_like_brazilian_mobile():
    assert looks_like_brazilian_mobile("+5500000000000") is True
    assert looks_like_brazilian_mobile("+5500000000000") is False  # fixo
    assert looks_like_brazilian_mobile("12345") is False
