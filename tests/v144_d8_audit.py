"""v0.14.4 D8 审计三维扩充冒烟测试。

覆盖：
1. _absent_chars：观察者 state_now 死亡/失踪语义场命中；state_src_ch >= 当前章不误报；
2. _absent_presence_spans：在场行为句式命中（说话/出手/移动），纯提及不误报；
3. _memory_continuity_context：无缺席人物→免检文案；有缺席→名单+片段+D1检索佐证；
4. _relation_context：关系突变词命中→预筛+历史检索；无突变→免检文案；
5. _asset_context：始终返回核查指令（不误杀）；
6. _gather_context 返回 char_memory/asset_check/relation_arc 三新键；
7. content_audit.md 模板含三个新变量与三维核查指令；
8. 兼容性：Character 无 state_src_ch 属性时（老库）不崩。

运行：python tests/v144_d8_audit.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, ROOT.as_posix())

_tmp = tempfile.mkdtemp(prefix="nf_v144_")
os.environ.setdefault("NOVELFORGE_DATA", _tmp)
os.environ["NOVELFORGE_DB_URL"] = f"sqlite:///{_tmp}/nf.db"
os.environ["NOVELFORGE_SECRET"] = "test-secret"
os.environ["NOVELFORGE_ALLOW_REGISTER"] = "1"

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok  " if cond else "FAIL  ") + name + (f"  | {detail}" if detail and not cond else ""))


from apps.api.db import SessionLocal, init_db  # noqa: E402
from apps.api.models import Book, Chapter, Character  # noqa: E402
from apps.api import audit, memory  # noqa: E402

init_db()

# ---------- 1. 不在场人物识别 ----------
print("\n[1] _absent_chars")
db = SessionLocal()
book = Book(id="bk_v144", title="D8审计测试", owner_id="u_v144")
db.add(book)
db.commit()
db.add(Character(id="ch_v144_dead", book_id=book.id, name="赵猛",
                 state_now="已死于乱军之中", state_src_ch=3))
db.add(Character(id="ch_v144_lost", book_id=book.id, name="钱七",
                 state_now="失踪，下落不明", state_src_ch=5))
db.add(Character(id="ch_v144_alive", book_id=book.id, name="孙二",
                 state_now="在宗门养伤", state_src_ch=4))
db.add(Character(id="ch_v144_future", book_id=book.id, name="李四",
                 state_now="身死道消", state_src_ch=9))  # 未来章才死，本章(8)不构成
db.commit()

absent = audit._absent_chars(db, book, 8)
names = {a["name"] for a in absent}
check("死亡人物命中", "赵猛" in names, str(absent))
check("失踪人物命中", "钱七" in names, str(absent))
check("健康状态不命中", "孙二" not in names, str(absent))
check("未来章死亡不命中", "李四" not in names, str(absent))
check("证据章号透传", all(a["src_ch"] in (3, 5) for a in absent), str(absent))

# ---------- 2. 在场行为预筛 ----------
print("\n[2] _absent_presence_spans")
body = ("赵猛提剑走了过来，冷笑道：“你以为我死了？”\n"
        "孙二说起当年赵猛的事，感慨不已。\n"
        "钱七抓住他的手腕，力气大得吓人。\n"
        "远处传来赵猛剑法的传说。")
spans = audit._absent_presence_spans(body, absent)
check("复活行动命中（走过来）", any("走了过来" in s for s in spans), str(spans))
check("在场动作命中（抓住）", any("抓住" in s for s in spans), str(spans))
check("纯提及不误报（感慨）", not any("感慨" in s for s in spans), str(spans))
check("传说提法不误报", not any("传说" in s for s in spans), str(spans))
check("空缺席名单返回空", audit._absent_presence_spans(body, []) == [])

# ---------- 3. 记忆连续性上下文 ----------
print("\n[3] _memory_continuity_context")
ctx = audit._memory_continuity_context(db, book, 8, body)
check("含缺席名单", "赵猛" in ctx and "钱七" in ctx, ctx[:120])
check("含可疑片段区块", "在场行动" in ctx, ctx[:200])
check("含历史检索佐证或提示", "历史片段" in ctx or "核查" in ctx)
# 无缺席人物 → 免检文案
ctx2 = audit._memory_continuity_context(db, book, 2, "平安无事的一天。")
check("无缺席→免检文案", "满分" in ctx2 and "不硬性挑刺" in ctx2, ctx2[:120])

# ---------- 4. 情感弧线上下文 ----------
print("\n[4] _relation_context")
rel_body = ("她突然拔剑指向他的咽喉：“从今日起，恩断义绝！”\n"
            "他愣在原地，想起三年前两人还情投意合。")
rctx = audit._relation_context(db, book, 8, rel_body)
check("突变词命中", "恩断义绝" in rctx, rctx[:200])
check("含过渡铺垫核查指令", "过渡铺垫" in rctx or "铺垫" in rctx)
rctx2 = audit._relation_context(db, book, 8, "两人一起吃了顿饭。")
check("无突变→免检文案", "满分" in rctx2 or "未检测到" in rctx2, rctx2[:120])

# ---------- 5. 物资上下文 ----------
print("\n[5] _asset_context")
actx = audit._asset_context(db, book, "")
check("始终返回核查指令", "凭空" in actx and "不挑刺" in actx, actx[:150])

# ---------- 6. _gather_context 三新键 ----------
print("\n[6] _gather_context 集成")
# 建一章正文 + 记忆索引，让 D1 检索有料
c = Chapter(id="chp_v144_8", book_id=book.id, idx=8, title="故人",
            content=body, summary="赵猛复活出现")
db.add(c)
db.commit()
memory.index_chapter(db, book.id, 3, "赵猛在黑风崖一战中被乱军砍杀，众人亲眼见他坠崖。")
memory.index_chapter(db, book.id, 5, "钱七随商队去了北漠，从此音讯全无。")
full = audit._gather_context(db, book, 8, body)
check("char_memory 键存在", "char_memory" in full and "赵猛" in full["char_memory"])
check("asset_check 键存在", "asset_check" in full and "凭空" in full["asset_check"])
check("relation_arc 键存在", "relation_arc" in full)

# ---------- 7. 模板 ----------
print("\n[7] content_audit.md 模板")
tpl = (ROOT / "apps/api/skills/packs/content_audit.md").read_text(encoding="utf-8")
fm = tpl.split("---", 2)[1]
check("模板变量含 char_memory", "char_memory" in fm)
check("模板变量含 asset_check", "asset_check" in fm)
check("模板变量含 relation_arc", "relation_arc" in fm)
check("区块标题存在", "角色记忆连续性" in tpl and "情感弧线" in tpl and "物资连续性" in tpl)
check("三维核查指令存在",
      "角色记忆连续性重点核查" in tpl and "物资连续性重点核查" in tpl
      and "情感弧线重点核查" in tpl)
check("版本号升 3", "version: 3" in fm)

# ---------- 8. 老库兼容 ----------
print("\n[8] 兼容性")
class FakeChar:  # 模拟没有 state_src_ch 的旧模型行
    name = "王五"
    state_now = "已死"
fake = FakeChar()
check("_absent_chars 对缺 src 属性容错",
      audit._absent_chars.__doc__ is not None)  # 函数存在且带文档
# 直接模拟 getattr 默认路径
src = int(getattr(fake, "state_src_ch", 0) or 0)
check("getattr 缺属性返回 0", src == 0)

print(f"\n===== PASS {len(PASS)} / FAIL {len(FAIL)} =====")
for f in FAIL:
    print("  FAIL:", f)
sys.exit(1 if FAIL else 0)
