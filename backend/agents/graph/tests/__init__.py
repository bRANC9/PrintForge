"""Colocated tests for the agent workflow (agent-orchestrator scope).

Everything here is deterministic: the LLM provider and the CAD backend are
fakes, and RAG retrieval is injected, so the suite needs no network, no Ollama
and no OpenSCAD.
"""
