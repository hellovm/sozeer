# -*- coding: utf-8 -*-
"""v0.17 A3 回归：剧本引用语法 DSL（评估报告缺口 1）。

缺口原文：
  "剧本引用语法 DSL 缺失：剧本层无法用 `@[角色/衍生]{台词}` 直接把说话人
   绑定资产与音色，改编时需要额外的名称匹配逻辑"

实现：apps/api/script_dsl.py
  * 解析 @[角色/衍生]{台词} → 结构化 utterances（与现有 schema 同构）
  * render 反向输出（可读稿），round-trip lossless
  * bind_assets 绑定 DramaAsset + voice_profile（衍生回退本体音色）
  * normalize_script 兼容旧 utterances JSON（向后兼容）
"""
import json
import os

import pytest

os.environ.setdefault("NOVELFORGE_SECRET", "test-secret-a3")


# ==================================================== 解析
def test_parse_basic():
    from apps.api import script_dsl as D
    frags = D.parse_script("@[陈长安]{山门虽远。}")
    assert len(frags) == 1
    f = frags[0]
    assert f["speaker"] == "陈长安" and f["form"] == ""
    assert f["text"] == "山门虽远。" and f["kind"] == "dialogue"


def test_parse_derivative_form():
    """★ 衍生形态：@[角色/形态]。"""
    from apps.api import script_dsl as D
    f = D.parse_script("@[陈长安/青年]{我还没有输。}")[0]
    assert f["speaker"] == "陈长安" and f["form"] == "青年"


def test_parse_form_with_dot_separator():
    from apps.api import script_dsl as D
    f = D.parse_script("@[大黄·神兽]{(低吼)}")[0]
    assert f["speaker"] == "大黄" and f["form"] == "神兽"


@pytest.mark.parametrize("who", ["旁白", "画外音", "解说", "narrator", "?", "", "VO"])
def test_narrator_detection(who):
    """★ 旁白/画外音/匿名 → voiceover（不需要口型）。"""
    from apps.api import script_dsl as D
    f = D.parse_script(f"@[{who}]{{测试。}}")[0]
    assert f["kind"] == "voiceover", f"{who!r} 应识别为 voiceover"


def test_empty_who_is_anonymous():
    """★ @[]{环境音} 空说话人 → 匿名（等同旁白），且必须被正则匹配到。"""
    from apps.api import script_dsl as D
    frags = D.parse_script("@[]{（脚步声）}")
    assert len(frags) == 1, f"空 who 未被匹配：{frags}"
    assert frags[0]["kind"] == "voiceover"
    assert frags[0]["text"] == "（脚步声）"


def test_multi_line_parse():
    from apps.api import script_dsl as D
    txt = """@[陈长安]{山门虽远。}
@[陈长安/青年]{我还没有输。}
@[旁白]{三年后。}"""
    frags = D.parse_script(txt)
    assert len(frags) == 3
    assert [f["kind"] for f in frags] == ["dialogue", "dialogue", "voiceover"]


def test_prose_preserved():
    """非 DSL 的普通文本不丢弃（保留原文）。"""
    from apps.api import script_dsl as D
    frags = D.parse_script("开场白。\n@[A]{台词。}\n结尾。")
    kinds = [f["kind"] for f in frags]
    assert "prose" in kinds
    assert len([k for k in kinds if k == "prose"]) == 2


def test_empty_input():
    from apps.api import script_dsl as D
    assert D.parse_script("") == []
    assert D.parse_script(None) == []


def test_no_dsl_returns_prose_only():
    from apps.api import script_dsl as D
    frags = D.parse_script("这是一段没有标记的文本。")
    assert len(frags) == 1 and frags[0]["kind"] == "prose"


# ==================================================== split_speaker
@pytest.mark.parametrize("who,expect", [
    ("陈长安", ("陈长安", "")),
    ("陈长安/青年", ("陈长安", "青年")),
    ("陈长安·青年", ("陈长安", "青年")),
    ("大黄/神兽", ("大黄", "神兽")),
    ("", ("", "")),
])
def test_split_speaker(who, expect):
    from apps.api import script_dsl as D
    assert D.split_speaker(who) == expect


# ==================================================== to_utterances
def test_to_utterances_schema():
    """★ 输出必须与现有 utterances schema 同构（下游零改动）。"""
    from apps.api import script_dsl as D
    utts = D.to_utterances("@[陈长安]{山门虽远。}\n@[旁白]{三年后。}")
    for u in utts:
        assert set(u.keys()) >= {"kind", "speaker", "text"}
    assert utts[0] == {"kind": "dialogue", "speaker": "陈长安", "text": "山门虽远。"}
    assert utts[1]["kind"] == "voiceover"


def test_to_utterances_form_in_speaker():
    """衍生形态写进 speaker（下游 _scene_ref_images 能解析）。"""
    from apps.api import script_dsl as D
    utts = D.to_utterances("@[陈长安/青年]{我还没有输。}")
    assert utts[0]["speaker"] == "陈长安（青年）"
    # 验证下游正则能解析
    import re
    m = re.match(r"^(.*?)[（(·](.*?)[）)]?$", utts[0]["speaker"])
    assert m and m.group(1).strip() == "陈长安" and m.group(2).strip() == "青年"


def test_to_utterances_include_prose():
    from apps.api import script_dsl as D
    utts = D.to_utterances("开场。\n@[A]{台词。}", include_prose=True)
    assert len(utts) == 2
    assert utts[0]["kind"] == "voiceover"      # prose → 旁白
    assert utts[1]["speaker"] == "A"


def test_to_utterances_skip_prose_by_default():
    from apps.api import script_dsl as D
    utts = D.to_utterances("开场。\n@[A]{台词。}")
    assert len(utts) == 1 and utts[0]["speaker"] == "A"


# ==================================================== render / round-trip
def test_render_script():
    from apps.api import script_dsl as D
    utts = [{"kind": "dialogue", "speaker": "陈长安", "text": "山门虽远。"},
            {"kind": "voiceover", "speaker": None, "text": "三年后。"}]
    txt = D.render_script(utts)
    assert "@[陈长安]{山门虽远。}" in txt
    assert "@[旁白]{三年后。}" in txt


def test_round_trip_lossless():
    """★ round-trip 无损（改编稿 ↔ 结构化数据可互转）。"""
    from apps.api import script_dsl as D
    txt = """@[陈长安]{山门虽远，此心可往。}
@[陈长安/青年]{我还没有输。}
@[大黄/神兽]{(低吼)}
@[旁白]{三年后，青云山下。}"""
    utts = D.to_utterances(txt)
    back = D.to_utterances(D.render_script(utts))
    assert back == utts, f"round-trip 不一致：\n{utts}\n{back}"


def test_render_scene():
    from apps.api import script_dsl as D
    scene = {"scene_description": "雨夜街头。",
             "utterances": [{"kind": "dialogue", "speaker": "A", "text": "到了。"}]}
    txt = D.render_scene(scene)
    assert "【场景】雨夜街头。" in txt
    assert "@[A]{到了。}" in txt


def test_render_scene_accepts_orm_like():
    """也接受 ORM 对象（utterances 是 JSON 字符串）。"""
    from apps.api import script_dsl as D

    class FakeScene:
        scene_description = "雪山山门。"
        utterances = json.dumps([{"kind": "dialogue", "speaker": "A", "text": "走。"}],
                                ensure_ascii=False)

    txt = D.render_scene(FakeScene())
    assert "雪山山门。" in txt and "@[A]{走。}" in txt


# ==================================================== normalize（向后兼容）
def test_normalize_legacy_json_string():
    """★ 向后兼容：老数据是 JSON 字符串 → 原样解析。"""
    from apps.api import script_dsl as D
    legacy = json.dumps([{"kind": "dialogue", "speaker": "A", "text": "老格式"}],
                        ensure_ascii=False)
    out = D.normalize_script(legacy)
    assert out == [{"kind": "dialogue", "speaker": "A", "text": "老格式"}]


def test_normalize_list_passthrough():
    from apps.api import script_dsl as D
    src = [{"kind": "dialogue", "speaker": "A", "text": "x"}]
    assert D.normalize_script(src) is src


def test_normalize_dsl_text():
    from apps.api import script_dsl as D
    out = D.normalize_script("@[A]{走。}")
    assert out[0]["speaker"] == "A"


def test_normalize_dirty_input():
    from apps.api import script_dsl as D
    for bad in [None, "", "   ", 123, {}, "[坏json"]:
        out = D.normalize_script(bad)
        assert isinstance(out, list), f"{bad!r} 未返回列表"


# ==================================================== 资产绑定
class _A:
    """轻量资产替身（避免依赖 ORM）。"""
    def __init__(self, id, name, parent_id="", form_name="", voice_profile=""):
        self.id = id
        self.name = name
        self.parent_id = parent_id
        self.form_name = form_name
        self.voice_profile = voice_profile


def test_bind_assets_base():
    from apps.api import script_dsl as D
    assets = [_A("a1", "陈长安", voice_profile='{"voice":"v1"}')]
    utts = [{"kind": "dialogue", "speaker": "陈长安", "text": "走。"}]
    out = D.bind_assets(utts, assets)
    assert out[0]["asset_id"] == "a1"
    assert out[0]["voice_profile"] == '{"voice":"v1"}'


def test_bind_assets_derivative_form():
    """★ 衍生形态精确匹配衍生行。

    真实约定（见 drama._scene_ref_images）：衍生行的 name 是显示名（如「陈长安青年」），
    靠 parent_id + form_name 挂到本体上。
    """
    from apps.api import script_dsl as D
    assets = [
        _A("a1", "陈长安", voice_profile='{"voice":"v1"}'),
        _A("a2", "陈长安青年", parent_id="a1", form_name="青年",
           voice_profile='{"voice":"v2"}'),
    ]
    utts = [{"kind": "dialogue", "speaker": "陈长安（青年）", "text": "走。"}]
    out = D.bind_assets(utts, assets)
    assert out[0]["asset_id"] == "a2"
    assert out[0]["form_name"] == "青年"
    assert out[0]["voice_profile"] == '{"voice":"v2"}'


def test_bind_assets_derivative_falls_back_to_parent_voice():
    """★ 衍生无自身声纹 → 回退本体音色（跨形态同声的关键）。"""
    from apps.api import script_dsl as D
    assets = [
        _A("a1", "陈长安", voice_profile='{"voice":"v1"}'),
        _A("a2", "陈长安青年", parent_id="a1", form_name="青年", voice_profile=""),
    ]
    utts = [{"kind": "dialogue", "speaker": "陈长安（青年）", "text": "走。"}]
    out = D.bind_assets(utts, assets)
    assert out[0]["asset_id"] == "a2"
    assert out[0]["voice_profile"] == '{"voice":"v1"}', "衍生应回退本体音色"


def test_bind_assets_unknown_speaker():
    """未匹配到资产 → 字段留空，不抛错。"""
    from apps.api import script_dsl as D
    out = D.bind_assets([{"kind": "dialogue", "speaker": "路人甲", "text": "喂。"}], [])
    assert out[0]["asset_id"] == ""


def test_bind_assets_narrator_untouched():
    from apps.api import script_dsl as D
    out = D.bind_assets([{"kind": "voiceover", "speaker": None, "text": "三年后。"}],
                        [_A("a1", "陈长安")])
    assert out[0]["asset_id"] == ""


def test_bind_assets_preserves_fields():
    """★ 原字段不被破坏（下游零改动）。"""
    from apps.api import script_dsl as D
    src = {"kind": "dialogue", "speaker": "陈长安", "text": "走。"}
    out = D.bind_assets([src], [_A("a1", "陈长安")])
    for k, v in src.items():
        assert out[0][k] == v


# ==================================================== 端到端
def test_dsl_to_tts_jobs_pipeline():
    """★ 端到端：DSL → utterances → voice_cast 的 TTS 作业装配。"""
    from apps.api import script_dsl as D, voice_cast as VC
    assets = [_A("a1", "陈长安", voice_profile="")]
    utts = D.bind_assets(D.to_utterances("@[陈长安]{山门虽远。}\n@[旁白]{三年后。}"),
                         assets)
    cast = {"陈长安": {"voice": "zh-CN-YunxiNeural", "rate": 1.0}}
    jobs = VC.build_scene_tts_jobs(utts, cast, mode="quality")
    assert jobs, "DSL 产出的 utterances 应能装配出 TTS 作业"


def test_module_contract():
    from apps.api import script_dsl as D
    for name in ("parse_script", "to_utterances", "render_script", "render_scene",
                 "bind_assets", "normalize_script", "split_speaker"):
        assert hasattr(D, name), f"缺 {name}"
        assert name in D.__all__


def test_drama_imports_dsl():
    """drama.py 的剧本落库路径必须接入 DSL 归一。"""
    import inspect
    from apps.api import drama
    src = inspect.getsource(drama)
    assert "script_dsl" in src, "drama.py 未接入 script_dsl"
    assert "normalize_script" in src
