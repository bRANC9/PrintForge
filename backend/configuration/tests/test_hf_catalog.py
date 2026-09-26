"""Tests for the Hugging Face GGUF catalog (quant selection + real sizes).

The catalog decides what a pull actually downloads, so a wrong pick is a
multi-gigabyte mistake: a 27B model offered in F16 is >50 GB. These tests pin
the parsing and the "never suggest full precision" rule against the real
filenames Hugging Face repos use.
"""

from __future__ import annotations

import pytest

from configuration.services import (
    _FITS_CONSUMER_GPU_BYTES,
    _is_usable_quant,
    _pick_quant,
    _quant_label,
    search_huggingface_models,
)

GB = 10**9


def quant_of(filename: str) -> str:
    """The quant label the parser extracts from a GGUF filename."""
    return _quant_label(filename[: -len(".gguf")]) if filename.lower().endswith(".gguf") else ""


@pytest.mark.parametrize(
    "filename, expected",
    [
        ("Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf", "Q4_K_M"),
        ("model-Q8_0.gguf", "Q8_0"),
        ("Qwen3.8-27B-iMatrix-NVFP4-MTP.gguf", "NVFP4-MTP"),
        ("gemma-3-12b-it-MXFP4-Q6_K.gguf", "MXFP4"),
        ("model-IQ3_XXS.gguf", "IQ3_XXS"),
        ("DeepSeek-R1-Distill-Qwen-14B-F16.gguf", "F16"),
        ("llama-3.1-8B-exl2-8_0.gguf", "exl2-8_0"),
        ("UD-IQ3_XXS-model.gguf", "UD-IQ3_XXS"),
        ("unsloth-model-UD-IQ3_XXS.gguf", "UD-IQ3_XXS"),
        ("model-Q4_K_M-00001-of-00002.gguf", "Q4_K_M"),
    ],
)
def test_quant_label_is_found_anywhere_in_the_filename(filename: str, expected: str) -> None:
    """Repos put the quant anywhere; the old "just before .gguf" rule missed them."""
    assert quant_of(filename) == expected


def test_a_27b_repo_ships_nvfp4_not_f16_as_the_pull_candidate() -> None:
    """The measured live case: only an NVFP4 build plus an F16 sibling.

    The catalog used to report ``F16`` (the quant token it could parse) and
    offered a >50 GB download instead of the 17 GB NVFP4 file that exists.
    """
    files = [
        {"quant": "NVFP4", "file": "Qwen3.8-27B-NVFP4-MTP.gguf", "size": 17 * GB},
        {"quant": "F16", "file": "Qwen3.8-27B-F16.gguf", "size": 54 * GB},
    ]

    chosen = _pick_quant(files)

    assert chosen is not None
    assert chosen["quant"] == "NVFP4"
    assert chosen["size"] == 17 * GB


def test_full_precision_and_1_2_bit_quants_are_never_picked() -> None:
    assert not _is_usable_quant("F16")
    assert not _is_usable_quant("BF16")
    assert not _is_usable_quant("IQ1_S")
    assert not _is_usable_quant("IQ2_XXS")
    assert _is_usable_quant("Q4_K_M")
    assert _is_usable_quant("NVFP4-MTP")

    assert _pick_quant([{"quant": "F16", "file": "m-F16.gguf", "size": 54 * GB}]) is None
    assert _pick_quant([{"quant": "IQ1_S", "file": "m-IQ1_S.gguf", "size": 4 * GB}]) is None


def test_preference_order_wins_over_size() -> None:
    files = [
        {"quant": "Q8_0", "file": "a.gguf", "size": 34 * GB},
        {"quant": "Q4_K_M", "file": "b.gguf", "size": 19 * GB},
        {"quant": "Q2_K", "file": "c.gguf", "size": 10 * GB},
    ]

    assert _pick_quant(files)["quant"] == "Q4_K_M"


def test_unknown_quant_falls_back_to_the_largest_that_fits_a_consumer_gpu() -> None:
    files = [
        {"quant": "WEIRD_8BIT", "file": "a.gguf", "size": 12 * GB},
        {"quant": "ANOTHER", "file": "b.gguf", "size": 40 * GB},
    ]

    chosen = _pick_quant(files)

    assert chosen["quant"] == "WEIRD_8BIT"
    assert chosen["size"] <= _FITS_CONSUMER_GPU_BYTES


def test_search_reports_sizes_and_never_offers_full_precision(monkeypatch) -> None:
    """End-to-end shape of the catalog payload the advisor UI consumes."""
    search_response = [
        {
            "id": "someone/Qwen3.8-27B-GGUF",
            "downloads": 100,
            "likes": 10,
        }
    ]
    detail_response = {
        "siblings": [
            {"rfilename": "Qwen3.8-27B-NVFP4-MTP.gguf", "size": 17 * GB},
            {"rfilename": "Qwen3.8-27B-F16.gguf", "size": 54 * GB},
        ]
    }

    def fake_remote_json(url: str, **kwargs) -> object:  # noqa: ANN003
        return search_response if "/api/models?" in url else detail_response

    monkeypatch.setattr("configuration.services._remote_json", fake_remote_json)

    models = search_huggingface_models("Qwen3.8", limit=5)

    assert len(models) == 1
    entry = models[0]
    assert entry["pull_name"] == "hf.co/someone/Qwen3.8-27B-GGUF:NVFP4-MTP"
    # Sizes are reported in 1024-based units, like the rest of the app.
    assert entry["size_human"] == "15.8 GB"
    # The F16 sibling is not offered as a quantization choice.
    assert "F16" not in entry["quants"]
    assert entry["quant_sizes"]["NVFP4-MTP"] == "15.8 GB"


def test_search_offers_no_pull_when_only_full_precision_exists(monkeypatch) -> None:
    search_response = [{"id": "someone/Big-GGUF", "downloads": 1, "likes": 0}]
    detail_response = {"siblings": [{"rfilename": "Big-F16.gguf", "size": 60 * GB}]}

    def fake_remote_json(url: str, **kwargs) -> object:  # noqa: ANN003
        return search_response if "/api/models?" in url else detail_response

    monkeypatch.setattr("configuration.services._remote_json", fake_remote_json)

    entry = search_huggingface_models("Big", limit=5)[0]

    assert entry["pull_name"] == ""
    assert entry["quants"] == []
