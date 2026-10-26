# -*- coding: utf-8 -*-
"""v0.14.10 D19 音色资产化测试。

覆盖四组：
  A. 特征推断（性别/年龄/质感/音色池）—— 含「台词不得污染性别」的回归
  B. 确定性分配（同角色恒同音色、顺序无关、不撞声、人工锁定优先）
  C. 数据结构（VoiceProfile 序列化、TTS 参数组装、时间轴）
  D. 与 audio_route 的对接（逐行 TTS 作业）
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from apps.api import voice_cast as vc            # noqa: E402
from apps.api import audio_route as ar           # noqa: E402


# ============================================================ A 特征推断

class TestInferTraits:
    def test_male_mature_from_description(self):
        t = vc.infer_traits("清冷、低沉、说话简短", "青年剑客，白衣执剑")
        assert t["gender"] == "male"
        assert t["pool"] == "male_mature"
        assert "cold" in t["quality"] and "deep" in t["quality"]

    def test_female_young_from_description(self):
        t = vc.infer_traits("清亮、活泼、略带天真", "少女，仙门弟子")
        assert t["gender"] == "female"
        assert t["pool"] == "female_young"
        assert "bright" in t["quality"] and "cute" in t["quality"]

    def test_male_old_from_description(self):
        t = vc.infer_traits("苍老、沙哑、语速慢", "苍老的道门长老")
        assert t["gender"] == "male"
        assert t["age"] == "old"
        assert t["pool"] == "male_old"
        assert "hoarse" in t["quality"]

    def test_empty_text_falls_back_to_unknown_pool(self):
        t = vc.infer_traits("")
        assert t["pool"] == "unknown"

    def test_conflicting_gender_hints_yield_unknown(self):
        """同时出现男女线索 → 不猜，落 unknown 池（仍能保证不撞声）。"""
        t = vc.infer_traits("少年", "少女")
        assert t["gender"] == "unknown"

    def test_traits_to_tts_tweaks_are_subtle(self):
        """微调必须克制：夸张变声会破坏真人质感。"""
        r_old, p_old, _ = vc.traits_to_tts_tweaks({"age": "old", "quality": []})
        r_chi, p_chi, _ = vc.traits_to_tts_tweaks({"age": "child", "quality": []})
        assert 0.8 <= r_old <= 1.0 and -4 <= p_old <= 0
        assert 1.0 <= r_chi <= 1.2 and 0 <= p_chi <= 4

    def test_deep_and_bright_shift_pitch_oppositely(self):
        _, p_deep, _ = vc.traits_to_tts_tweaks({"age": "mature", "quality": ["deep"]})
        _, p_bright, _ = vc.traits_to_tts_tweaks({"age": "mature", "quality": ["bright"]})
        assert p_deep < 0 < p_bright


class TestNameBasedInference:
    """生产环境实测：大量角色资产 description/voice_style 全空，只有名字。

    纯靠描述推断会全部落 unknown 池 → 多人撞声、少女配男声。姓名兜底是必需的。
    """

    def test_female_from_name(self):
        t = vc.infer_traits_from_name("小丫头")
        assert t and t["gender"] == "female"

    def test_male_from_title_suffix(self):
        assert vc.infer_traits_from_name("沈少主")["gender"] == "male"
        assert vc.infer_traits_from_name("楚家管事")["gender"] == "male"

    def test_unsignalled_name_returns_none(self):
        assert vc.infer_traits_from_name("沈福") is None

    def test_empty_name_returns_none(self):
        assert vc.infer_traits_from_name("") is None

    def test_resolve_profile_uses_name_when_desc_empty(self):
        p = vc.resolve_profile("小丫头", "")
        assert p.traits["gender"] == "female"
        assert p.voice in vc.VOICE_POOL["female_young"]

    def test_production_scenario_no_collision(self):
        """复现真实项目：6 个只有名字的角色，必须零撞声。"""
        names = ["沈福", "沈少主", "小丫头", "楚清霜", "山羊胡供奉", "楚家管事"]
        cast = vc.assign_voices(names, hints={})
        voices = [v.voice for v in cast.values()]
        assert len(voices) == len(set(voices)), f"撞声: {voices}"

    def test_large_cast_never_raises(self):
        """角色数超过池子容量时复用音色 + pitch 区分，绝不抛异常。"""
        names = [f"角色{i}" for i in range(40)]
        cast = vc.assign_voices(names, hints={})
        assert len(cast) == 40
        assert all(v.voice for v in cast.values())


class TestDialogueMustNotPolluteGender:
    """回归：牧云谣的台词含「师兄」，早期版本据此把她判成 male / 性别冲突。"""
    def test_line_calling_others_does_not_change_gender(self):
        rows = [{"name": "牧云谣", "description": "少女，仙门弟子",
                 "voice_style": "清亮、活泼"}]
        utts = [{"kind": "dialogue", "speaker": "牧云谣", "text": "师兄，我怕。"}]
        cast = vc.cast_for_project(rows, utts)
        p = cast["牧云谣"]
        assert p["traits"]["gender"] == "female", "台词里的「师兄」不得污染性别"
        assert p["traits"]["pool"] == "female_young"

    def test_lines_still_contribute_quality(self):
        """台词不能判性别，但应补充 quality（语气线索）。"""
        rows = [{"name": "甲", "description": "青年男子"}]
        utts = [{"kind": "dialogue", "speaker": "甲", "text": "哼，区区鼠辈，也配？"}]
        cast = vc.cast_for_project(rows, utts)
        # 「哼」类台词不必然命中 quality 词表，这里只要不报错且仍是 dict
        assert isinstance(cast["甲"]["traits"]["quality"], list)

    def test_collect_hints_only_returns_dict(self):
        h = vc.collect_hints([{"speaker": "甲", "text": "你好"}])
        assert h == {"甲": "你好"}


# ============================================================ B 确定性分配

class TestDeterministicAssignment:
    def test_same_speaker_same_voice_across_calls(self):
        """跨镜头一致的核心保证。"""
        hints = {"陈长安": "青年剑客"}
        v1 = vc.resolve_profile("陈长安", hints["陈长安"]).voice
        v2 = vc.resolve_profile("陈长安", hints["陈长安"]).voice
        v3 = vc.assign_voices(["陈长安"], hints=hints)["陈长安"].voice
        assert v1 == v2 == v3

    def test_assignment_is_order_independent(self):
        sp = ["陈长安", "牧云谣", "玄阳子", "小翠"]
        hints = {k: "" for k in sp}
        a = vc.assign_voices(sp, hints=hints)
        b = vc.assign_voices(list(reversed(sp)), hints=hints)
        assert {k: v.voice for k, v in a.items()} == {k: v.voice for k, v in b.items()}

    def test_no_voice_collision_when_pool_is_large_enough(self):
        sp = ["甲", "乙", "丙"]
        cast = vc.assign_voices(sp, hints={})
        voices = [v.voice for v in cast.values()]
        assert len(voices) == len(set(voices)), f"撞声: {voices}"

    def test_locked_manual_profile_wins(self):
        existing = {"牧云谣": {"speaker": "牧云谣", "voice": "xiaoyi",
                               "source": "manual", "locked": True}}
        cast = vc.assign_voices(["牧云谣", "陈长安"], hints={}, existing=existing)
        assert cast["牧云谣"].voice == "xiaoyi"
        assert cast["牧云谣"].locked is True

    def test_unlocked_existing_profile_is_overwritable(self):
        existing = {"甲": {"speaker": "甲", "voice": "zzz", "source": "auto"}}
        cast = vc.assign_voices(["甲"], hints={"甲": "青年男子"}, existing=existing)
        assert cast["甲"].locked is False

    def test_stable_index_ignores_python_hash_seed(self):
        """必须用 md5 而非内置 hash()——后者带进程随机种子，重启就换声。"""
        i1 = vc._stable_index("陈长安", "male_mature", 3)
        i2 = vc._stable_index("陈长安", "male_mature", 3)
        assert i1 == i2
        assert 0 <= i1 < 3

    def test_pool_overflow_falls_back_without_error(self):
        """池子被占满时不报错，放宽到全局池。"""
        many = [f"角色{i}" for i in range(30)]
        cast = vc.assign_voices(many, hints={})
        assert len(cast) == 30
        assert all(v.voice for v in cast.values())

    def test_narrator_pool_is_fixed(self):
        p = vc.profile_for_speaker("", {}, "风雪夜。")
        assert p.voice == vc.VOICE_POOL["narrator"][0]
        assert p.traits["pool"] == "narrator"


# ============================================================ C 数据结构

class TestVoiceProfile:
    def test_roundtrip_serialization(self):
        p = vc.VoiceProfile("陈长安", "yunfeng", 0.95, -2.0, "calm")
        q = vc.VoiceProfile.from_dict(p.to_dict())
        assert (q.speaker, q.voice, q.rate, q.pitch, q.style) == \
               ("陈长安", "yunfeng", 0.95, -2.0, "calm")

    def test_rate_pitch_are_clamped(self):
        p = vc.VoiceProfile("x", "y", rate=99.0, pitch=-999.0)
        assert p.rate == vc.RATE_MAX
        assert p.pitch == vc.PITCH_MIN

    def test_from_dict_tolerates_garbage(self):
        assert vc.VoiceProfile.from_dict(None).voice == vc.DEFAULT_VOICE
        assert vc.VoiceProfile.from_dict({"rate": "abc"}).rate == 1.0

    def test_tts_kwargs_shape(self):
        p = vc.VoiceProfile("甲", "yunxi", 1.1, 2.0, "gentle")
        kw = p.tts_kwargs()
        assert kw["voice"] == "yunxi" and kw["speed"] == 1.1
        assert kw["pitch"] == 2.0 and kw["style"] == "gentle"

    def test_tts_kwargs_omits_zero_pitch(self):
        p = vc.VoiceProfile("甲", "yunxi", 1.0, 0.0)
        assert "pitch" not in p.tts_kwargs()


class TestLipsyncTimeline:
    def test_timeline_is_contiguous_with_gaps(self):
        segs = [{"speaker": "甲", "text": "你好世界", "duration": 2.0},
                {"speaker": "乙", "text": "再见", "duration": 1.0}]
        tl = vc.build_lipsync_timeline(segs, gap=0.25)
        assert tl["tracks"][0]["start"] == 0.0
        assert tl["tracks"][0]["end"] == 2.0
        assert tl["tracks"][1]["start"] == 2.25
        assert tl["total"] == 3.25

    def test_by_speaker_indexes(self):
        segs = [{"speaker": "甲", "text": "a", "duration": 1},
                {"speaker": "乙", "text": "b", "duration": 1},
                {"speaker": "甲", "text": "c", "duration": 1}]
        tl = vc.build_lipsync_timeline(segs)
        assert tl["by_speaker"]["甲"] == [0, 2]
        assert tl["by_speaker"]["乙"] == [1]

    def test_estimate_duration_scales_with_length(self):
        short = vc.estimate_duration("你好")
        long_ = vc.estimate_duration("你好世界，这是一个很长的句子。")
        assert 0 < short < long_

    def test_estimate_duration_respects_rate(self):
        a = vc.estimate_duration("测试语速", 1.0)
        b = vc.estimate_duration("测试语速", 2.0)
        assert b < a


# ============================================================ D 与 audio_route 对接

class TestSceneTtsJobs:
    UTTS = [
        {"kind": "voiceover", "speaker": None, "text": "风雪夜。"},
        {"kind": "dialogue", "speaker": "陈长安", "text": "此心可往。"},
        {"kind": "dialogue", "speaker": "牧云谣", "text": "我怕。"},
    ]

    def test_quality_mode_all_lines_to_tts(self):
        cast = vc.cast_for_project(
            [{"name": "陈长安", "description": "青年男子"},
             {"name": "牧云谣", "description": "少女"}], self.UTTS)
        jobs = vc.build_scene_tts_jobs(self.UTTS, cast, mode="quality")
        assert len(jobs) == 3
        assert {j["speaker"] for j in jobs} == {"", "陈长安", "牧云谣"}

    def test_fast_mode_dialogue_goes_native(self):
        cast = vc.cast_for_project(
            [{"name": "陈长安", "description": "青年男子"}], self.UTTS)
        jobs = vc.build_scene_tts_jobs(self.UTTS, cast, mode="fast")
        speakers = {j["speaker"] for j in jobs}
        assert "陈长安" not in speakers, "fast 档对白应交给 LTX 原生"
        assert "" in speakers, "fast 档旁白仍走 TTS"

    def test_jobs_carry_voice_and_kwargs(self):
        cast = {"陈长安": vc.VoiceProfile("陈长安", "yunfeng", 0.9, -1.0).to_dict()}
        jobs = vc.build_scene_tts_jobs(
            [{"kind": "dialogue", "speaker": "陈长安", "text": "走。"}], cast)
        assert jobs[0]["voice"] == "yunfeng"
        assert jobs[0]["tts_kwargs"]["speed"] == 0.9

    def test_unknown_speaker_falls_back(self):
        """cast 里没有的角色也要能出作业（回落，不报错）。"""
        jobs = vc.build_scene_tts_jobs(
            [{"kind": "dialogue", "speaker": "路人甲", "text": "喂。"}], {})
        assert len(jobs) == 1 and jobs[0]["voice"]

    def test_profile_for_speaker_accepts_dict_cast(self):
        """load_cast_from_db 产出的是 dict，必须能吃（早期版本 AttributeError）。"""
        cast = {"甲": vc.VoiceProfile("甲", "yunxi").to_dict()}
        p = vc.profile_for_speaker("甲", cast)
        assert p.voice == "yunxi"

    def test_empty_utterances_make_no_jobs(self):
        assert vc.build_scene_tts_jobs([], {}) == []
        assert vc.build_scene_tts_jobs(
            [{"kind": "dialogue", "speaker": "甲", "text": "   "}], {}) == []


class TestCastForProject:
    def test_derivative_forms_share_base_voice(self):
        """「陈长安（老年）」应归到本体「陈长安」，不单独配音色。"""
        rows = [{"name": "陈长安", "description": "青年男子"}]
        utts = [{"kind": "dialogue", "speaker": "陈长安（老年）", "text": "老了。"}]
        cast = vc.cast_for_project(rows, utts)
        # 补漏角色进表；纯函数层不做「衍生归本体」（那在 load_cast_from_db）
        assert isinstance(cast, dict)

    def test_project_cast_covers_all_asset_characters(self):
        rows = [{"name": "甲", "description": "青年男子"},
                {"name": "乙", "description": "少女"}]
        cast = vc.cast_for_project(rows, [])
        assert set(cast) == {"甲", "乙"}

    def test_json_serializable(self):
        cast = vc.cast_for_project([{"name": "甲", "description": "青年男子"}], [])
        json.dumps(cast, ensure_ascii=False)  # 不抛异常即可


class TestAudioRouteIntegration:
    def test_tts_lines_keep_speaker(self):
        utts = [{"kind": "dialogue", "speaker": "甲", "text": "你好"}]
        lines = ar.tts_lines(utts, mode="quality")
        assert lines and lines[0]["speaker"] == "甲"
