"""冒烟测试：导入现有小说 → 检查结构化抽取质量。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy import func as F

from apps.api import importer
from apps.api.db import SessionLocal, init_db
from apps.api.models import Book, Chapter, Character, Foreshadow, StyleProfile, Volume, WorldSetting, jl

SRC = sys.argv[1] if len(sys.argv) > 1 else r"D:/AI/workbuddyData/2026-10-10-10-07-33/novel"

init_db()
db = SessionLocal()

# 清掉旧的同名书，避免重复导入
for b in db.scalars(select(Book)).all():
    if b.title and "躺平修仙" in b.title:
        db.delete(b)
db.commit()

res = importer.import_book(db, SRC)
print("== 导入结果 ==")
for k, v in res.items():
    print(f"  {k}: {v}")

book = db.get(Book, res["book_id"])
print("\n== 书 ==")
print(f"  书名: {book.title} | 类型: {book.genre[:40]} | 作者: {book.author}")
print(f"  梗概: {book.synopsis[:80]}")

print("\n== 分卷 ==")
for v in db.scalars(select(Volume).where(Volume.book_id == book.id).order_by(Volume.idx)):
    print(f"  卷{v.idx}《{v.name}》{v.start_ch}-{v.end_ch}章  outline {len(v.outline)}字")

print("\n== 人物 ==")
for c in db.scalars(select(Character).where(Character.book_id == book.id).limit(10)):
    print(f"  {c.name}（{c.role[:20]}） profile {len(c.profile)}字")

print("\n== 世界观 ==")
for w in db.scalars(select(WorldSetting).where(WorldSetting.book_id == book.id).limit(14)):
    print(f"  [{w.category}] {w.key}: {w.value[:40]}")

print("\n== 伏笔（前 8 条）==")
stats = {}
for f in db.scalars(select(Foreshadow).where(Foreshadow.book_id == book.id)):
    stats[f.status] = stats.get(f.status, 0) + 1
for f in db.scalars(select(Foreshadow).where(Foreshadow.book_id == book.id).limit(8)):
    print(f"  [{f.status}] 第{f.planted_ch}章 {f.content[:60]}")
print(f"  状态分布: {stats}")

sp = db.scalar(select(StyleProfile).where(StyleProfile.book_id == book.id))
print("\n== 风格 DNA ==")
print(f"  视角: {sp.pov} | 基调: {sp.tone[:40]}")
print(f"  禁用词({len(jl(sp.banned_words, []))}): {'、'.join(jl(sp.banned_words, [])[:12])}")
print(f"  写作准则: {len(sp.rules)} 字")

print("\n== 章节 ==")
chs = db.scalars(select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.idx)).all()
for c in chs[:5]:
    print(f"  第{c.idx:03d}章 {c.title}  {c.words}字")
print(f"  ... 共 {len(chs)} 章，合计 {sum(c.words for c in chs)} 字")

# 质检一章
from apps.api.orchestrator import gates
rep = gates.run_gates(chs[11].content, book, sp, [c.content for c in chs[8:11]])
print(f"\n== 质检第012章 ==  分数 {rep.score} / 通过 {rep.passed}")
print(f"  统计: {rep.stats}")
for i in rep.issues[:8]:
    print(f"  [{i.level}][{i.rule}] {i.message}")
db.close()
