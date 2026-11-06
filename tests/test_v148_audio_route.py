# -*- coding: utf-8 -*-
"""D14-r 音频策略路由回归测试。

依据 D18 第二轮实测（LTX i2v 跨镜头 F0 漂移 34.7% 明显换声），
确立的路由表：
  - dialogue → TTS（quality 档，跨镜头音色一致）/ LTX 原生（fast 档）
  - voiceover → 恒 TTS（不需要口型；t2v 台词不可靠，实测 -33~-40dB）
  - 空文本 → skip
"""
import pytest

from apps.api import audio_route as ar


# ---------------------------------------------------------------- classify
def test_classify_dialogue_is_tts():
    assert ar.classify_utterance({"kind": "dialogue", "text": "你好"}) == ar.CH_TTS


def test_classify_voiceover_is_tts():
    """旁白不需要口型，且 t2v 台词不可靠 → 恒 TTS。"""
    assert ar.classify_utterance({"kind": "voiceover", "text": "话说当年"}) == ar.CH_TTS


def test_classify_empty_text_is_skip():
    assert ar.classify_utterance({"kind": "dialogue", "text": "  "}) == ar.CH_SKIP
    assert ar.classify_utterance({"kind": "dialogue"}) == ar.CH_SKIP


def test_classify_unlabeled_with_text_falls_back_tts():
    """未标 kind 但有文本：宁可 TTS 可控，不可赌模型。"""
    assert ar.classify_utterance({"text": "喂"}) == ar.CH_TTS


# ---------------------------------------------------------------- split
def test_split_quality_mode_all_tts():
    """quality 档：所有台词都给 TTS，native 恒空（这正是为了跨镜头一致）。"""
    utts = [{"kind": "dialogue", "text": "A"},
            {"kind": "voiceover", "text": "B"}]
    r = ar.split_utterances(utts, mode=ar.MODE_QUALITY)
    assert len(r["tts"]) == 2
    assert r["native"] == []
    assert r["skip"] == []


def test_split_fast_mode_dialogue_native_voiceover_tts():
    """fast 档：画面内对白交给 LTX 原生，旁白仍走 TTS（t2v 台词不可靠）。"""
    utts = [{"kind": "dialogue", "speaker": "陈长安", "text": "剑来"},
            {"kind": "voiceover", "text": "那一日，风雪满山"}]
    r = ar.split_utterances(utts, mode=ar.MODE_FAST)
    assert len(r["native"]) == 1
    assert r["native"][0]["text"] == "剑来"
    assert len(r["tts"]) == 1
    assert r["tts"][0]["text"] == "那一日，风雪满山"


def test_split_skips_empty():
    utts = [{"kind": "dialogue", "text": ""}, {"kind": "dialogue", "text": "有效"}]
    r = ar.split_utterances(utts, mode=ar.MODE_QUALITY)
    assert len(r["tts"]) == 1
    assert len(r["skip"]) == 1


# ---------------------------------------------------------------- native lines
def test_native_dialogue_lines_quality_empty():
    """quality 档 video prompt 不得含台词——否则模型随机配音与 TTS 叠成双声。"""
    utts = [{"kind": "dialogue", "text": "剑来"}]
    assert ar.native_dialogue_lines(utts, mode=ar.MODE_QUALITY) == []


def test_native_dialogue_lines_fast_returns_dialogue_only():
    utts = [{"kind": "dialogue", "text": "剑来"},
            {"kind": "voiceover", "text": "旁白"}]
    lines = ar.native_dialogue_lines(utts, mode=ar.MODE_FAST)
    assert lines == ["剑来"]


# ---------------------------------------------------------------- plan
def test_plan_scene_audio_quality():
    utts = [{"kind": "dialogue", "speaker": "A", "text": "一"},
            {"kind": "voiceover", "speaker": None, "text": "二"}]
    plan = ar.plan_scene_audio(utts, mode=ar.MODE_QUALITY)
    assert plan["mode"] == ar.MODE_QUALITY
    assert plan["has_tts"] is True
    assert plan["has_native"] is False
    assert "一" in plan["tts_text"] and "二" in plan["tts_text"]
    assert plan["native_text"] == ""
    assert len(plan["channels"]) == 2
    assert all(c["channel"] == ar.CH_TTS for c in plan["channels"])
    assert any("TTS" in n for n in plan["notes"])


def test_plan_scene_audio_fast_routes_dialogue_native():
    utts = [{"kind": "dialogue", "speaker": "A", "text": "剑来"},
            {"kind": "voiceover", "text": "旁白"}]
    plan = ar.plan_scene_audio(utts, mode=ar.MODE_FAST)
    ch = {c["utt"]["text"]: c["channel"] for c in plan["channels"]}
    assert ch["剑来"] == ar.CH_NATIVE
    assert ch["旁白"] == ar.CH_TTS
    assert plan["native_text"] == "剑来"
    assert "旁白" in plan["tts_text"]


def test_plan_scene_audio_no_lines():
    plan = ar.plan_scene_audio([], mode=ar.MODE_QUALITY)
    assert plan["has_tts"] is False
    assert plan["has_native"] is False
    assert any("环境音" in n for n in plan["notes"])


# ---------------------------------------------------------------- mode coercion
def test_audio_plan_for_project_coercion():
    assert ar.audio_plan_for_project("") == ar.MODE_QUALITY
    assert ar.audio_plan_for_project("garbage") == ar.MODE_QUALITY
    assert ar.audio_plan_for_project("FAST") == ar.MODE_FAST
    assert ar.audio_plan_for_project("quality") == ar.MODE_QUALITY
