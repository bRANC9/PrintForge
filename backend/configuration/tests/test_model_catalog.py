"""Tests for the curated model catalog and the VRAM-based recommender."""

from __future__ import annotations

import pytest

from configuration import model_catalog


def test_estimate_memory_grows_with_quant_and_context():
    small = model_catalog.estimate_memory_gb(8.0, "Q3_K_M")
    large = model_catalog.estimate_memory_gb(8.0, "Q8_0")
    long_ctx = model_catalog.estimate_memory_gb(8.0, "Q4_K_M", context=32768)

    assert small < large
    assert long_ctx > model_catalog.estimate_memory_gb(8.0, "Q4_K_M")


def test_estimate_rejects_unknown_quant():
    with pytest.raises(ValueError, match="quantization"):
        model_catalog.estimate_memory_gb(7.0, "Q9_K_XL")


def test_recommend_marks_small_models_as_fitting():
    results = model_catalog.recommend(8)
    by_name = {model["name"]: model for model in results}

    assert by_name["llama3.2:1b"]["fits"] is True
    assert by_name["llama3.1:8b"]["fits"] is True
    # A 70B model cannot fit in 8 GB.
    assert by_name["llama3.1:70b"]["fits"] is False


def test_recommend_prefers_a_comfortable_quant():
    # 8 GB comfortably runs an 8B at Q4_K_M (a "good" quant), so it should rank
    # above a 14B that only fits at Q3_K_M.
    names = [model["name"] for model in model_catalog.recommend(8) if model["fits"]]
    assert names.index("llama3.1:8b") < names.index("qwen2.5-coder:14b")


def test_recommend_filters_by_category():
    results = model_catalog.recommend(24, categories=["embedding"])
    assert results
    assert all("embedding" in model["categories"] for model in results)


def test_models_can_belong_to_multiple_categories():
    coding = {model["name"] for model in model_catalog.recommend(24, categories=["coding"])}
    chat = {model["name"] for model in model_catalog.recommend(24, categories=["chat"])}
    vision = {model["name"] for model in model_catalog.recommend(24, categories=["vision"])}

    # A coder is also a chat model; a multimodal model is chat + vision.
    assert "qwen2.5-coder:7b" in coding
    assert "qwen2.5-coder:7b" in chat
    assert "gemma3:12b" in chat
    assert "gemma3:12b" in vision

    by_name = {model["name"]: model for model in model_catalog.recommend(24)}
    assert by_name["qwen2.5-coder:7b"]["categories"] == ["coding", "chat"]
    assert by_name["gemma3:12b"]["categories"] == ["chat", "vision"]


def test_recommend_includes_installed_models_not_in_catalog():
    installed = [{"name": "custom/model:latest", "size": 4 * 1024**3}]

    results = model_catalog.recommend(8, installed=installed)
    extra = next(model for model in results if model["name"] == "custom/model:latest")

    assert extra["installed"] is True
    assert extra["fits"] is True
    assert extra["estimated_gb"] == 4.0


def test_recommend_marks_installed_by_exact_tag():
    installed = [{"name": "llama3.1:8b", "size": 5 * 1024**3}]

    results = model_catalog.recommend(8, installed=installed)
    model = next(item for item in results if item["name"] == "llama3.1:8b")

    assert model["installed"] is True


def test_recommend_does_not_mark_other_tags_of_the_same_family():
    # An installed ``qwen2.5-coder:1.5b`` must not mark ``qwen2.5-coder:7b``.
    installed = [{"name": "qwen2.5-coder:1.5b", "size": 1 * 1024**3}]

    results = model_catalog.recommend(8, installed=installed)
    installed_names = {item["name"] for item in results if item["installed"]}

    assert "qwen2.5-coder:1.5b" in installed_names
    assert "qwen2.5-coder:7b" not in installed_names


def test_recommendation_tiers_pick_the_biggest_comfortable_model():
    tiers = {tier["vram_gb"]: tier["model"] for tier in model_catalog.recommendation_tiers()}

    assert set(tiers) == set(model_catalog.VRAM_TIERS)
    # A 4 GB card should be told about a small model, not a 70B one.
    assert tiers[4] is not None
    assert tiers[4]["params_b"] <= 8
    # More VRAM never recommends a smaller model than less VRAM.
    assert tiers[48]["params_b"] >= tiers[16]["params_b"]
