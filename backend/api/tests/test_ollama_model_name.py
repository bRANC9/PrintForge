"""Unit tests for the strict Ollama model-name validation."""

from __future__ import annotations

import pytest

from api.serializers import OllamaModelNameSerializer

ACCEPTED = [
    "qwen3-coder:30b",
    "llama3.1:8b",
    "library/llama3:8b",
    "mxbai-embed-large",
    "a",
    "a.b_c-d:e/f",
    "..a",  # a dotted segment that is not a traversal
]

REJECTED = [
    "",  # empty
    "   ",  # whitespace-only
    "a" * 201,  # too long
    "/absolute/path",  # leading slash
    "trailing/",  # trailing slash
    "a//b",  # empty segment
    "a/./b",  # "." segment
    "a/../b",  # ".." segment
    "../../etc/passwd",  # traversal
    "a/../../b",  # traversal
    ".",  # "." alone
    "..",  # ".." alone
    "/",  # slash alone
    "a/..",  # trailing ".."
    "bad name",  # space
    "na;me",  # disallowed char
    "na\\me",  # disallowed char
    "na?me",  # disallowed char
]


@pytest.mark.parametrize("name", ACCEPTED)
def test_accepted_model_names(name):
    serializer = OllamaModelNameSerializer(data={"name": name})

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["name"] == name


@pytest.mark.parametrize("name", REJECTED)
def test_rejected_model_names(name):
    serializer = OllamaModelNameSerializer(data={"name": name})

    assert not serializer.is_valid()
    assert "name" in serializer.errors
