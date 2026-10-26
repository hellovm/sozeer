"""Mock 云端 LLM：实现 OpenAI 兼容协议（含 SSE 流式），用于端到端验证编排内核。

按 prompt 内容判断该返回什么：任务单 JSON / 登记 JSON / 正文。
"""
import json
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

BRIEF = {
    "title": "守纹那点事",
    "goal": "沈闲查守纹含义并遮掩左肋云纹，被迫接下后山外围巡察",
    "beat": ["起：柴院早起发现云纹", "承：用社畜逻辑敷衍钱多多",
             "转：任务堂被迫接巡察", "钩：夜里云纹发烫"],
    "payoff": ["沈家旧纹家徽/素袍老者闪回"],
    "plant": ["云纹夜里发烫", "任务堂后山巡察无人敢接"],
    "coolpoint": "躺着也涨修为的误伤式爽点",
    "characters": ["沈闲", "钱多多", "楚清霜"],
    "constraint": "沈闲仍在灵海境，不得突破真元境",
    "hook": "断在云纹发烫、大爷沉默的瞬间",
}

SUMMARY = {
    "summary": "沈闲早起发现左肋浮出青色云纹，用打工人话术敷衍钱多多的追问。去任务堂交差时被管事塞了无人敢接的后山外围巡察，想推却推不掉。夜里云纹发烫，大爷罕见沉默。楚清霜禁足中托人送来一句话，沈闲欠她的人情还没还。",
    "planted": [
        {"content": "云纹夜里发烫，似与后山界碑呼应", "target_vol": 2},
        {"content": "任务堂后山巡察月例翻倍却无人敢接", "target_vol": 2},
    ],
    "payoff": [{"content": "沈家旧纹家徽"}],
    "characters": [{"name": "沈闲", "state": "灵海境，左肋云纹遮掩中，被迫接后山巡察"},
                   {"name": "钱多多", "state": "不知情，仍苦练"}],
    "chapter_title_suggest": "",
}

BODY = """# 第013章 守纹那点事

天没亮我就醒了。

不是自然醒，是左肋那块皮，烫。

我掀开衣服看了一眼。青色的云纹趴在肋骨上，像谁用蘸了墨的指甲轻轻划了一道，线条细，走势却很硬。

我按了两下。它在。按不掉。

【崽，】大爷的声音有点飘，【你别老动它。】

"它自己长出来的。"

【它认得你。】大爷说，【你不认得它，这就是麻烦。】

我把衣服拉下来，坐在床沿发了一会儿呆。

按我在上辈子的经验，身上突然多出来的东西只有两种：一种是你花钱买的，另一种是你还不清的。

这个明显是第二种。

——

钱多多蹲在院子里刷牙，看见我出来，含着一口白沫子含糊道："兄弟你脸色不对啊，昨儿又去后山了？"

"没。"

"那你脸白得跟纸似的。"

"我这是底子好，"我说，"白得健康。"

他漱了口，凑过来压低声音："我跟你说个事。任务堂那边贴了新榜，后山外围巡察，月例翻倍。"

"翻倍多少？"

"两颗灵石。"

我心动了半秒。

就半秒。因为下一句他补上了："——但那活儿半年没人敢接。"

"为什么？"

钱多多看了我一眼，那眼神像看一个还没收到裁员通知的同事。

"绕禁地走一圈，"他说，"上个月接的两个，回来一个，另一个名儿还挂在榜上没擦。"

我把那半秒的心动掐死了。

"我不接。"

——

话是这么说，交差的时候我还是被点了名。

任务堂的管事姓刘，脸上褶子多得能夹住灵石。他把册子摊开，手指头在纸上一路划拉，最后停在我的名字上。

"沈闲。"

"在。"

"大长老说过，记名弟子不免任务。"

"我记着呢。"

"后山外围巡察，"刘管事抬起眼皮看我，"月例翻倍。你接不接？"

院子里安静了一瞬。

我脑子里飞快过了三个方案：装病、装傻、装死。

装病得躺三天，柴院没炭，冻也冻出病来。装傻我这月份表现太亮眼，装不像。装死代价太大。

"接，"我说，"但我想问一句。"

"问。"

"上一个接的人，"我说，"回来那个，现在在哪儿？"

刘管事的手指头在册子上顿住了。

他没回答。

——

夜里，左肋又开始烫。

比早上更烫，烫得我整条胳膊都发麻。我坐在黑暗里，听见自己的心跳一下一下撞着耳朵。

【大爷。】

没人应。

【大爷？】

过了很久，它才出声，声音哑得不像它：

【……崽，你别去后山。】

"我不去。"

【你答应我。】

我应了一声。

窗外的月光落在地上，白得像铺了一层盐。我盯着那片白，忽然发现左肋的云纹不烫了——

它在往一个方向，一下一下地跳。

像有人在碑的那头，敲了三下。
"""


def pick(prompt: str) -> str:
    if "章前任务单" in prompt or '"coolpoint"' in prompt:
        return json.dumps(BRIEF, ensure_ascii=False)
    if "伏笔管理员" in prompt or '"planted"' in prompt:
        return json.dumps(SUMMARY, ensure_ascii=False)
    if "定点改写" in prompt or "只替换" in prompt:
        # 返回被要求改写的原文（末尾追加一句），模拟模型照做
        m = re.search(r"# 原文\n(.*?)\n# 必须修掉", prompt, re.S)
        return (m.group(1).strip() if m else "") + "\n\n（改写完成）"
    return BODY


@app.post("/v1/chat/completions")
async def completions(req: Request):
    d = await req.json()
    prompt = "".join(m.get("content", "") for m in d.get("messages", []))
    text = pick(prompt)
    tin = min(6000, len(prompt) // 2)
    tout = len(text) // 2

    if d.get("stream"):
        def gen():
            step = max(12, len(text) // 60)
            for i in range(0, len(text), step):
                piece = text[i:i + step]
                yield "data: " + json.dumps(
                    {"choices": [{"delta": {"content": piece}}]}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps(
                {"choices": [{"delta": {}}], "usage": {"prompt_tokens": tin, "completion_tokens": tout}}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse({
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": tin, "completion_tokens": tout},
        "model": d.get("model", "mock"),
    })


@app.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}
