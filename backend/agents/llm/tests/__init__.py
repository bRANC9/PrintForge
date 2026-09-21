"""Colocated tests for the LLM layer (llm-provider scope).

These are unit tests: no database, no network. Ollama transport is stubbed by
monkeypatching ``OllamaProvider._post`` (so no HTTP request is made), and the
provider contract is exercised through a ``FakeProvider``.
"""
