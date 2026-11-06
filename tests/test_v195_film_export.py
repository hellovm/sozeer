# -*- coding: utf-8 -*-
"""v0.19.5 F4 回归：短剧成片导出（拼接 + 混音 + 硬字幕）。

与「剪映草稿导出」的分工
----------------------
| 产物 | 适用 |
|---|---|
| 剪映草稿（已有） | 要**精修**：调色、配乐、转场 |
| **成片 mp4（本模块）** | 要**快速验收/直接发布** |

本文件覆盖：字幕生成/折行/SRT、CJK 字体探测、ffmpeg 缺失降级、
以及**用真实 ffmpeg 合成**（有 ffmpeg 的环境才跑，否则 skip）。
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile

import pytest

os.environ.setdefault("NOVELFORGE_DATA", tempfile.mkdtemp(prefix="nf_test_f4_"))
os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-f4")

HAS_FFMPEG = bool(shutil.which("ffmpeg"))


class S:
    """轻量分镜替身。"""
    def __init__(self, idx, video_path="", dur=8, utts=None, audio=""):
        self.idx = idx
        self.video_path = video_path
        self.duration_seconds = dur
        self.utterances = json.dumps(utts or [], ensure_ascii=False)
        self.audio_path = audio


# ============================================================ 模块契约
def test_module_contract():
    from apps.api import film_export as fe
    for n in ("export_film", "build_subtitles", "to_srt", "find_cjk_font",
              "ffmpeg_available", "DEFAULT_W", "DEFAULT_H"):
        assert hasattr(fe, n), f"缺 {n}"


def test_default_canvas_is_vertical():
    """短剧默认竖屏 9:16。"""
    from apps.api import film_export as fe
    assert fe.DEFAULT_H > fe.DEFAULT_W, "短剧应为竖屏"


# ============================================================ 字幕生成
def test_build_subtitles_basic():
    from apps.api import film_export as fe
    scenes = [
        S(1, dur=4, utts=[{"kind": "dialogue", "speaker": "沈闲", "text": "退婚？退得好。"}]),
        S(2, dur=4, utts=[{"kind": "voiceover", "speaker": None, "text": "三年后。"}]),
    ]
    subs = fe.build_subtitles(scenes)
    assert len(subs) == 2
    assert subs[0]["start"] == 0 and subs[0]["end"] == 4
    assert subs[1]["start"] == 4 and subs[1]["end"] == 8
    assert "沈闲：" in subs[0]["text"], "对白应带说话人"
    assert "：" not in subs[1]["text"], "旁白不该带说话人前缀"


def test_build_subtitles_multi_utt_split():
    """一个分镜多条台词 → 均分时长。"""
    from apps.api import film_export as fe
    scenes = [S(1, dur=6, utts=[
        {"kind": "dialogue", "speaker": "A", "text": "一。"},
        {"kind": "dialogue", "speaker": "B", "text": "二。"},
        {"kind": "dialogue", "speaker": "A", "text": "三。"},
    ])]
    subs = fe.build_subtitles(scenes)
    assert len(subs) == 3
    assert abs(subs[0]["start"] - 0) < 0.01
    assert abs(subs[1]["start"] - 2) < 0.01
    assert abs(subs[2]["start"] - 4) < 0.01


def test_build_subtitles_skips_empty():
    from apps.api import film_export as fe
    scenes = [S(1, dur=4, utts=[{"kind": "dialogue", "speaker": "A", "text": "  "}])]
    assert fe.build_subtitles(scenes) == []


def test_build_subtitles_bad_json():
    """脏 utterances 不能崩。"""
    from apps.api import film_export as fe
    s = S(1, dur=4)
    s.utterances = "{坏 json"
    assert fe.build_subtitles([s]) == []
    s.utterances = None
    assert fe.build_subtitles([s]) == []


def test_wrap_long_text():
    """长台词要折行（竖屏最多两行）。"""
    from apps.api import film_export as fe
    long_text = "这是一句非常长的台词" * 4
    scenes = [S(1, dur=8, utts=[{"kind": "voiceover", "text": long_text}])]
    subs = fe.build_subtitles(scenes, max_chars_per_line=16)
    assert "\n" in subs[0]["text"], "长文本未折行"
    lines = subs[0]["text"].split("\n")
    assert len(lines) <= 2, f"折行超两行：{lines}"


def test_wrap_short_text_unchanged():
    from apps.api import film_export as fe
    scenes = [S(1, dur=4, utts=[{"kind": "voiceover", "text": "短句。"}])]
    subs = fe.build_subtitles(scenes, max_chars_per_line=16)
    assert "\n" not in subs[0]["text"]


# ============================================================ SRT
def test_to_srt_format():
    from apps.api import film_export as fe
    subs = [{"start": 0.0, "end": 2.5, "text": "第一句"},
            {"start": 2.5, "end": 5.0, "text": "第二句"}]
    srt = fe.to_srt(subs)
    assert "1\n00:00:00,000 --> 00:00:02,500" in srt
    assert "2\n00:00:02,500 --> 00:00:05,000" in srt
    assert "第一句" in srt


def test_to_srt_hours():
    from apps.api import film_export as fe
    srt = fe.to_srt([{"start": 3661.5, "end": 3662.0, "text": "x"}])
    assert "01:01:01,500" in srt, f"小时/分秒换算错误：{srt}"


def test_to_srt_negative_clamped():
    from apps.api import film_export as fe
    srt = fe.to_srt([{"start": -1, "end": 1, "text": "x"}])
    assert "00:00:00,000" in srt


# ============================================================ 字体
def test_find_cjk_font_returns_str():
    from apps.api import film_export as fe
    r = fe.find_cjk_font()
    assert isinstance(r, str)


@pytest.mark.skipif(not pathlib.Path("/usr/share/fonts").exists(),
                    reason="非 Linux 字体目录")
def test_cjk_font_exists_on_linux():
    """★ Linux 上必须能找到中文字体（否则字幕全是方框）。"""
    from apps.api import film_export as fe
    f = fe.find_cjk_font()
    assert f, "未找到任何中文字体，字幕会显示方框"
    assert pathlib.Path(f).exists()


# ============================================================ 降级路径
def test_no_video_returns_error():
    from apps.api import film_export as fe
    r = fe.export_film(None, 1, [S(1, video_path="")], pathlib.Path(tempfile.mkdtemp()))
    assert r["ok"] is False
    assert "视频" in r["error"]


def test_fallback_when_no_ffmpeg(monkeypatch, tmp_path):
    """★ 无 ffmpeg 时降级为字节拼接（宁可给粗剪，也不要直接失败）。"""
    from apps.api import film_export as fe
    monkeypatch.setattr(fe, "ffmpeg_available", lambda: False)
    media = tmp_path / "drama_media"
    media.mkdir()
    (media / "a.mp4").write_bytes(b"\x00" * 100)
    (media / "b.mp4").write_bytes(b"\x01" * 100)
    scenes = [S(1, "drama_media/a.mp4"), S(2, "drama_media/b.mp4")]
    r = fe.export_film(None, 1, scenes, tmp_path)
    assert r["ok"] is True and r.get("degraded") is True
    assert r["warnings"], "降级应带警告"
    assert (tmp_path / r["path"]).exists()


def test_missing_files_skipped(tmp_path):
    """分镜文件缺失 → 跳过并警告，不整体失败。"""
    from apps.api import film_export as fe
    if not fe.ffmpeg_available():
        pytest.skip("需要 ffmpeg")
    scenes = [S(1, "drama_media/不存在.mp4")]
    r = fe.export_film(None, 1, scenes, tmp_path)
    assert r["ok"] is False        # 全部缺失 → 无法合成


def test_never_raises_on_bad_input():
    """★ 导出绝不能抛异常（任务会挂）。"""
    from apps.api import film_export as fe
    for bad in ([], None, [S(1, video_path="x.mp4")]):
        try:
            r = fe.export_film(None, 1, bad or [], pathlib.Path(tempfile.mkdtemp()))
            assert isinstance(r, dict) and "ok" in r
        except Exception as e:
            pytest.fail(f"入参 {bad!r} 抛异常：{type(e).__name__}: {e}")


# ============================================================ ★ 真实合成（需 ffmpeg）
@pytest.mark.skipif(not HAS_FFMPEG, reason="需要 ffmpeg")
def test_real_synthesis(tmp_path):
    """★★ 用真实 ffmpeg 合成：3 段 → 1 个成片，验证时长与字幕烧录。"""
    from apps.api import film_export as fe
    media = tmp_path / "drama_media"
    media.mkdir()
    for i, c in enumerate(("red", "green", "blue"), 1):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", f"color=c={c}:s=320x568:d=2:r=25",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media / f"seg{i}.mp4")],
            capture_output=True, timeout=60, check=True)
    scenes = [
        S(1, "drama_media/seg1.mp4", 2, [{"kind": "dialogue", "speaker": "沈闲", "text": "退婚？退得好。"}]),
        S(2, "drama_media/seg2.mp4", 2, [{"kind": "voiceover", "speaker": None, "text": "三年后。"}]),
        S(3, "drama_media/seg3.mp4", 2, [{"kind": "dialogue", "speaker": "柳小姐", "text": "保重。"}]),
    ]
    r = fe.export_film(None, 1, scenes, tmp_path)
    assert r["ok"] is True, f"合成失败：{r.get('error')} {r.get('warnings')}"
    assert r["segments"] == 3
    assert r["subtitles"] == 3
    out = tmp_path / r["path"]
    assert out.exists() and out.stat().st_size > 1000
    # 时长应≈6 秒（3×2s）
    assert 5.0 < r["seconds"] < 7.5, f"成片时长异常：{r['seconds']}s"
    assert r["burned"] is True, "应烧录硬字幕"
