"""Curated local-model catalog and VRAM-based recommendation.

Ollama's local API only lists *installed* models and never exposes the GPU's
total VRAM, so this advisor is a deterministic **estimate**: a curated catalog
of common local models plus a simple memory model

    total ≈ weights + KV-cache + runtime overhead

where ``weights = params_b × bytes_per_param(quant)`` and the KV-cache grows
with the requested context length. The numbers are deliberately conservative
(headroom included) so a "fits" answer is safe rather than optimistic.

This module is pure (no Django, no network) so it is cheap to unit-test and can
be reused by the API and the MCP tools.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = [
    "CATALOG",
    "QUANT_BYTES_PER_PARAM",
    "QUANT_ORDER",
    "VRAM_TIERS",
    "estimate_memory_gb",
    "recommend",
    "recommendation_tiers",
]

#: Weights-only bytes per parameter for the common GGUF quantizations.
QUANT_BYTES_PER_PARAM: dict[str, float] = {
    "F16": 2.0,
    "Q8_0": 1.05,
    "Q6_K": 0.82,
    "Q5_K_M": 0.68,
    "Q4_K_M": 0.55,
    "Q4_0": 0.50,
    "Q3_K_M": 0.42,
    "Q2_K": 0.34,
}

#: Best quality first; the recommender picks the first quant that fits.
QUANT_ORDER: tuple[str, ...] = (
    "F16",
    "Q8_0",
    "Q6_K",
    "Q5_K_M",
    "Q4_K_M",
    "Q4_0",
    "Q3_K_M",
    "Q2_K",
)

#: Quants at or above this are considered "comfortable" (little quality loss).
_GOOD_QUANTS = frozenset({"F16", "Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M"})

#: Runtime/compute buffer that is not part of the weights.
RUNTIME_OVERHEAD_GB = 0.6

#: Rough KV-cache size at 8k context per billion parameters (fp16, GQA typical).
KV_GB_PER_PARAM_8K = 0.06

#: Quick-pick VRAM tiers shown in the UI.
VRAM_TIERS: tuple[int, ...] = (4, 6, 8, 10, 12, 16, 20, 24, 32, 48, 64)

#: Curated catalog of models that run locally with Ollama. ``params_b`` is the
#: total parameter count (for MoE models this is the sum of all experts, which
#: is what must be resident in memory). ``name`` is the exact ``ollama pull``
#: tag. A model belongs to one or more ``categories`` (e.g. a coder is both
#: ``coding`` and ``chat``; a multimodal model is both ``chat`` and ``vision``).
CATALOG: tuple[dict[str, Any], ...] = (
    # -- Coding (also usable as chat) --------------------------------------
    {
        "name": "qwen2.5-coder:1.5b",
        "family": "Qwen2.5-Coder",
        "params_b": 1.5,
        "categories": ["coding", "chat"],
        "notes": "Gyors kód-modell kis GPU/CPU-ra.",
    },
    {
        "name": "qwen2.5-coder:7b",
        "family": "Qwen2.5-Coder",
        "params_b": 7.0,
        "categories": ["coding", "chat"],
        "notes": "Népszerű kód-modell, jó ár/érték.",
    },
    {
        "name": "qwen2.5-coder:14b",
        "family": "Qwen2.5-Coder",
        "params_b": 14.0,
        "categories": ["coding", "chat"],
        "notes": "Erősebb kód-modell, ~10 GB Q4-en.",
    },
    {
        "name": "qwen2.5-coder:32b",
        "family": "Qwen2.5-Coder",
        "params_b": 32.0,
        "categories": ["coding", "chat"],
        "notes": "Csúcs lokális kód-modell 24 GB-tól.",
    },
    {
        "name": "qwen3-coder:30b",
        "family": "Qwen3-Coder",
        "params_b": 30.0,
        "categories": ["coding", "chat"],
        "notes": "MoE (A3B), nagy tudás, 24 GB-tól.",
    },
    {
        "name": "deepseek-coder-v2:16b",
        "family": "DeepSeek-Coder-V2",
        "params_b": 16.0,
        "categories": ["coding", "chat"],
        "notes": "MoE kód-modell, gyors aktív paraméterekkel.",
    },
    {
        "name": "codellama:7b",
        "family": "Code Llama",
        "params_b": 7.0,
        "categories": ["coding", "chat"],
        "notes": "Régi, de stabil kód-modell.",
    },
    # -- Chat / general ----------------------------------------------------
    {
        "name": "llama3.2:1b",
        "family": "Llama 3.2",
        "params_b": 1.0,
        "categories": ["chat"],
        "notes": "Nagyon kicsi, gyors asszisztens.",
    },
    {
        "name": "llama3.2:3b",
        "family": "Llama 3.2",
        "params_b": 3.0,
        "categories": ["chat"],
        "notes": "Kis, gyors chat modell.",
    },
    {
        "name": "llama3.1:8b",
        "family": "Llama 3.1",
        "params_b": 8.0,
        "categories": ["chat"],
        "notes": "Általános chat, 8 GB-on kényelmesen.",
    },
    {
        "name": "llama3.1:70b",
        "family": "Llama 3.1",
        "params_b": 70.0,
        "categories": ["chat"],
        "notes": "Nagy 70B; Q2_K-val is ~28 GB VRAM kell.",
    },
    {
        "name": "mistral:7b",
        "family": "Mistral",
        "params_b": 7.0,
        "categories": ["chat"],
        "notes": "Kiegyensúlyozott 7B modell.",
    },
    {
        "name": "mistral-nemo:12b",
        "family": "Mistral Nemo",
        "params_b": 12.0,
        "categories": ["chat"],
        "notes": "Hosszú kontextus, 12B.",
    },
    {
        "name": "gemma3:4b",
        "family": "Gemma 3",
        "params_b": 4.0,
        "categories": ["chat", "vision"],
        "notes": "Kis, multimodális (vision) is.",
    },
    {
        "name": "gemma3:12b",
        "family": "Gemma 3",
        "params_b": 12.0,
        "categories": ["chat", "vision"],
        "notes": "Multimodális, 12B.",
    },
    {
        "name": "gemma3:27b",
        "family": "Gemma 3",
        "params_b": 27.0,
        "categories": ["chat", "vision"],
        "notes": "Nagy multimodális modell, 24 GB-tól.",
    },
    {
        "name": "phi4:14b",
        "family": "Phi-4",
        "params_b": 14.0,
        "categories": ["chat", "reasoning"],
        "notes": "Erős 14B, jó érvelés.",
    },
    {
        "name": "phi3:mini",
        "family": "Phi-3",
        "params_b": 3.8,
        "categories": ["chat"],
        "notes": "Kicsi, CPU-n is elfut.",
    },
    {
        "name": "qwen2.5:7b",
        "family": "Qwen2.5",
        "params_b": 7.0,
        "categories": ["chat", "coding"],
        "notes": "Általános chat/kód 7B.",
    },
    {
        "name": "qwen2.5:14b",
        "family": "Qwen2.5",
        "params_b": 14.0,
        "categories": ["chat", "coding"],
        "notes": "Általános chat/kód 14B.",
    },
    {
        "name": "qwen2.5:32b",
        "family": "Qwen2.5",
        "params_b": 32.0,
        "categories": ["chat", "coding"],
        "notes": "Erős 32B, 24 GB-tól.",
    },
    # -- Reasoning (also chat) ---------------------------------------------
    {
        "name": "deepseek-r1:7b",
        "family": "DeepSeek-R1",
        "params_b": 7.0,
        "categories": ["reasoning", "chat"],
        "notes": "Gondolkodó (reasoning) 7B.",
    },
    {
        "name": "deepseek-r1:14b",
        "family": "DeepSeek-R1",
        "params_b": 14.0,
        "categories": ["reasoning", "chat"],
        "notes": "Gondolkodó 14B, ~10 GB Q4-en.",
    },
    {
        "name": "deepseek-r1:32b",
        "family": "DeepSeek-R1",
        "params_b": 32.0,
        "categories": ["reasoning", "chat"],
        "notes": "Gondolkodó 32B, 24 GB-tól.",
    },
    # -- Vision (also chat) ------------------------------------------------
    {
        "name": "llava:7b",
        "family": "LLaVA",
        "params_b": 7.0,
        "categories": ["vision", "chat"],
        "notes": "Kép + szöveg, 7B.",
    },
    {
        "name": "llava:13b",
        "family": "LLaVA",
        "params_b": 13.0,
        "categories": ["vision", "chat"],
        "notes": "Kép + szöveg, 13B.",
    },
    {
        "name": "qwen2.5vl:7b",
        "family": "Qwen2.5-VL",
        "params_b": 7.0,
        "categories": ["vision", "chat"],
        "notes": "Erős vision modell, 7B.",
    },
    {
        "name": "llama3.2-vision:11b",
        "family": "Llama 3.2 Vision",
        "params_b": 11.0,
        "categories": ["vision", "chat"],
        "notes": "Vision + chat, 11B.",
    },
    # -- Embedding ---------------------------------------------------------
    {
        "name": "bge-m3",
        "family": "BGE-M3",
        "params_b": 0.6,
        "categories": ["embedding"],
        "notes": "Többnyelvű embedding (a PrintForge alapértéke).",
    },
    {
        "name": "nomic-embed-text",
        "family": "Nomic Embed",
        "params_b": 0.14,
        "categories": ["embedding"],
        "notes": "Kicsi, gyors angol embedding.",
    },
    {
        "name": "mxbai-embed-large",
        "family": "MixedBread",
        "params_b": 0.34,
        "categories": ["embedding"],
        "notes": "Nagyobb, erős angol embedding.",
    },
    {
        "name": "all-minilm",
        "family": "All-MiniLM",
        "params_b": 0.02,
        "categories": ["embedding"],
        "notes": "Nagyon kicsi embedding, CPU-ra.",
    },
)

#: Category choices accepted by the recommender (plus "installed" extras).
CATEGORIES: tuple[str, ...] = ("coding", "chat", "reasoning", "vision", "embedding")


def estimate_memory_gb(params_b: float, quant: str, context: int = 8192) -> float:
    """Estimate the VRAM needed for ``params_b`` at ``quant`` and ``context``."""
    try:
        bytes_per_param = QUANT_BYTES_PER_PARAM[quant]
    except KeyError as exc:
        raise ValueError(f"Unknown quantization: {quant}") from exc
    weights = params_b * bytes_per_param
    kv_cache = params_b * KV_GB_PER_PARAM_8K * (max(int(context), 1) / 8192)
    return round(weights + kv_cache + RUNTIME_OVERHEAD_GB, 1)


def _best_quant(params_b: float, vram_gb: float, context: int) -> tuple[str, float, bool]:
    """Return ``(quant, estimated_gb, fits)`` for the largest quant that fits."""
    for quant in QUANT_ORDER:
        estimate = estimate_memory_gb(params_b, quant, context)
        if estimate <= vram_gb:
            return quant, estimate, True
    # Nothing fits: report the smallest possible footprint so the UI can show
    # how far off the model is.
    quant = QUANT_ORDER[-1]
    return quant, estimate_memory_gb(params_b, quant, context), False


def _base_name(name: str) -> str:
    """``qwen2.5-coder:7b`` -> ``qwen2.5-coder`` (tag-insensitive matching)."""
    return (name or "").split(":", 1)[0]


def _rank(entry: dict[str, Any]) -> tuple:
    """Sort key: comfortable fits first (biggest model), then tight fits, then misses."""
    estimate = entry["estimated_gb"] or 0.0
    if not entry["fits"]:
        return (2, 0.0, estimate)
    quality = 0 if entry["suggested_quant"] in _GOOD_QUANTS else 1
    params = entry["params_b"] or 0.0
    return (quality, -params, estimate)


def recommend(
    vram_gb: float,
    *,
    context: int = 8192,
    categories: Iterable[str] | None = None,
    installed: Iterable[dict[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Rank the catalog (and the installed models) for a given VRAM budget.

    ``installed`` is an iterable of ``{"name", "size"}`` dicts (Ollama
    ``/api/tags`` shape). Installed models that are not in the catalog are
    appended with their real on-disk size, so a model the operator already has
    is never hidden.
    """
    wanted = set(categories) if categories else None
    installed_list = list(installed or ())
    # Match by exact tag: an installed ``qwen2.5-coder:1.5b`` must not mark the
    # whole ``qwen2.5-coder`` family as installed. An untagged installed name
    # (Ollama's implicit ``:latest``) matches any catalog tag of that base.
    installed_names = {item.get("name", "") for item in installed_list}
    installed_untagged = {name for name in installed_names if ":" not in name}
    catalog_names = {entry["name"] for entry in CATALOG}

    results: list[dict[str, Any]] = []
    for entry in CATALOG:
        if wanted and not (set(entry["categories"]) & wanted):
            continue
        quant, estimate, fits = _best_quant(entry["params_b"], vram_gb, context)
        is_installed = (
            entry["name"] in installed_names or _base_name(entry["name"]) in installed_untagged
        )
        results.append(
            {
                **entry,
                "suggested_quant": quant,
                "estimated_gb": estimate,
                "fits": fits,
                "installed": is_installed,
            }
        )

    # Installed-but-uncatalogued models (real size, so memory is exact-ish).
    for item in installed_list:
        name = item.get("name") or ""
        if not name or name in catalog_names:
            continue
        size_bytes = item.get("size")
        size_gb = round(float(size_bytes) / 1024**3, 1) if size_bytes else None
        results.append(
            {
                "name": name,
                "family": item.get("family") or "",
                "params_b": None,
                "categories": ["installed"],
                "notes": "Telepített modell (a katalóguson kívül).",
                "suggested_quant": item.get("quantization") or "",
                "estimated_gb": size_gb,
                "fits": size_gb is None or size_gb <= vram_gb,
                "installed": True,
            }
        )

    results.sort(key=_rank)
    return results


def recommendation_tiers(
    *,
    context: int = 8192,
    categories: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Best fitting model for each quick-pick VRAM tier."""
    tiers: list[dict[str, Any]] = []
    for vram in VRAM_TIERS:
        fitting = [
            model
            for model in recommend(vram, context=context, categories=categories)
            if model["fits"]
        ]
        tiers.append({"vram_gb": vram, "model": fitting[0] if fitting else None})
    return tiers
