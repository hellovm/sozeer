# -*- coding: utf-8 -*-
"""短剧成片导出（v0.19.5 F4）。

为什么做
--------
项目已有「剪映草稿导出」—— 但它产出的是 **.zip 草稿工程**，用户还得：
  ① 解压 ② 找到剪映草稿目录 ③ 放进去 ④ 打开剪映 ⑤ 手动导出成片

而作者真正想要的是：**一键拿到能直接播/能直接发的 mp4**。

本模块补齐这一环：
    分镜视频按序拼接 → 旁白混音 → 台词烧硬字幕 → **成片 mp4**

与剪映导出的分工
----------------
| 产物 | 适用 |
|---|---|
| 剪映草稿（已有） | 要**精修**：调色调、换配乐、加转场 |
| **成片 mp4（本模块）** | 要**快速验收/直接发布** |

设计要点
--------
1. **ffmpeg 优先、纯 Python 降级**：没有 ffmpeg 时至少把分镜视频按序拼起来
2. **字幕用 SRT + 烧录**：中文用系统 CJK 字体，杜绝方框
3. **时长以分镜 duration_seconds 为准**：素材长度不符时补/截（避免音画错位）
4. **全程不抛异常**：任何一步失败都带明确原因返回，绝不把导出任务打挂
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time

# 成片画布（与短剧默认 9:16 竖屏一致）
DEFAULT_W, DEFAULT_H = 720, 1280


def _which(name: str) -> str:
    return shutil.which(name) or ""


def ffmpeg_available() -> bool:
    return bool(_which("ffmpeg"))


# ----------------------------------------------------------------- 字幕
def _parse_utterances(raw) -> list[dict]:
    if isinstance(raw, str):
        try:
            v = json.loads(raw or "[]")
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return raw if isinstance(raw, list) else []


def build_subtitles(scenes: list, *, max_chars_per_line: int = 16) -> list[dict]:
    """按分镜时间轴生成字幕条目。

    规则：
      * 一个分镜的台词均分该分镜时长（简单可靠，避免逐字估算误差）
      * 旁白（voiceover）与对白都上字幕（短剧观众默认静音刷，字幕是刚需）
      * 对白带说话人前缀（「沈闲：」），旁白不带
    返回 [{"start","end","text","scene_idx"}, ...]（秒）
    """
    out: list[dict] = []
    t = 0.0
    for s in scenes:
        dur = float(getattr(s, "duration_seconds", 0) or 8)
        utts = [u for u in _parse_utterances(getattr(s, "utterances", ""))
                if (u.get("text") or "").strip()]
        if utts:
            step = dur / len(utts)
            for i, u in enumerate(utts):
                txt = (u.get("text") or "").strip()
                sp = (u.get("speaker") or "").strip()
                kind = (u.get("kind") or "dialogue")
                if kind == "dialogue" and sp:
                    txt = f"{sp}：{txt}"
                txt = _wrap(txt, max_chars_per_line)
                st = t + i * step
                out.append({"start": round(st, 3), "end": round(st + step, 3),
                            "text": txt, "scene_idx": getattr(s, "idx", 0)})
        t += dur
    return out


def _wrap(text: str, n: int) -> str:
    """按字数折行（中文无空格，按字符数切最稳）。字幕最多两行。"""
    t = (text or "").strip()
    if len(t) <= n:
        return t
    # 优先在标点处断
    cut = -1
    for i in range(min(n, len(t) - 1), max(n - 6, 1), -1):
        if t[i - 1] in "，。！？；、,.!?;":
            cut = i
            break
    if cut <= 0:
        cut = n
    line1, line2 = t[:cut], t[cut:]
    if len(line2) > n:                     # 第二行还超 → 截断加省略号
        line2 = line2[:n - 1] + "…"
    return line1 + "\n" + line2


def _srt_ts(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(subs: list[dict]) -> str:
    """字幕列表 → SRT 文本。"""
    lines = []
    for i, s in enumerate(subs, 1):
        lines.append(str(i))
        lines.append(f"{_srt_ts(s['start'])} --> {_srt_ts(s['end'])}")
        lines.append(s["text"])
        lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------- 字体
def find_cjk_font() -> str:
    """找一个可用中文字体（烧字幕必须，否则全是方框）。"""
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for c in candidates:
        if pathlib.Path(c).exists():
            return c
    # 兜底：fc-match 查（Linux 常见）
    fc = _which("fc-match")
    if fc:
        try:
            r = subprocess.run([fc, "-f", "%{file}", "sans-serif:lang=zh"],
                               capture_output=True, text=True, timeout=10)
            p = (r.stdout or "").strip()
            if p and pathlib.Path(p).exists():
                return p
        except Exception:
            pass
    return ""


# ----------------------------------------------------------------- 主流程
def _seg_path(scene, books_dir: pathlib.Path) -> str:
    """分镜视频的绝对路径（兼容 /api/files/ 前缀与相对路径）。"""
    vp = (getattr(scene, "video_path", "") or "").strip()
    if not vp:
        return ""
    rel = vp.split("/api/files/", 1)[-1].lstrip("/")
    p = books_dir / rel
    return str(p) if p.exists() else ""


def _probe_duration(path: str) -> float:
    """用 ffprobe 取时长（失败返回 0）。"""
    ff = _which("ffprobe")
    if not ff or not path:
        return 0.0
    try:
        r = subprocess.run(
            [ff, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30)
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def export_film(project, episode: int, scenes: list, books_dir: pathlib.Path,
                *, out_rel_dir: str = "drama_media",
                burn_subtitles: bool = True,
                fit_duration: bool = True,
                timeout: int = 1800) -> dict:
    """把一个 ep 的分镜合成成片 mp4。

    返回 {"ok", "path"（相对路径）, "seconds", "segments", "subtitles",
          "burned", "warnings": [...]}

    流水线：
      1. 收集有视频的分镜（按 idx 排序）
      2. 归一化每段（统一分辨率/帧率/编码；时长对齐 duration_seconds）
      3. concat 拼接
      4. 混入旁白音轨（若有）
      5. 烧录硬字幕（SRT）
    """
    warnings: list[str] = []
    segs = [s for s in scenes if (getattr(s, "video_path", "") or "").strip()]
    if not segs:
        return {"ok": False, "error": "该集还没有任何分镜视频——先批量生成视频再合成成片"}
    segs.sort(key=lambda s: getattr(s, "idx", 0))

    if not ffmpeg_available():
        return _fallback_concat(segs, books_dir, out_rel_dir, warnings)

    ffmpeg = _which("ffmpeg")
    subs = build_subtitles(scenes) if burn_subtitles else []
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="nf_film_"))
    try:
        # ---- 1) 归一化每段 ----
        norm_files = []
        for i, s in enumerate(segs):
            src = _seg_path(s, books_dir)
            if not src:
                warnings.append(f"第{i+1}镜视频文件缺失，已跳过")
                continue
            dur = float(getattr(s, "duration_seconds", 0) or 8)
            out = tmp / f"n{i:03d}.mp4"
            args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", src,
                    "-vf", (f"scale={DEFAULT_W}:{DEFAULT_H}:force_original_aspect_ratio=decrease,"
                            f"pad={DEFAULT_W}:{DEFAULT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25"),
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-an"]
            if fit_duration:
                # 素材比目标短 → 末帧定格补齐；比目标长 → 截断。
                # 为什么必须对齐：旁白与字幕都按 duration_seconds 铺时间轴，
                # 素材长度不符就会音画错位（短剧里非常明显）。
                real = _probe_duration(src)
                if real and real < dur - 0.3:
                    pad = dur - real
                    args += ["-vf", (f"scale={DEFAULT_W}:{DEFAULT_H}:force_original_aspect_ratio=decrease,"
                                     f"pad={DEFAULT_W}:{DEFAULT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25,"
                                     f"tpad=stop_mode=clone:stop_duration={pad:.3f}")]
                args += ["-t", f"{dur:.3f}"]
            args.append(str(out))
            r = subprocess.run(args, capture_output=True, timeout=timeout)
            if r.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                warnings.append(f"第{i+1}镜归一化失败，已跳过")
                continue
            norm_files.append(out)

        if not norm_files:
            return {"ok": False, "error": "所有分镜都处理失败，无法合片"}

        # ---- 2) concat 拼接 ----
        lst = tmp / "list.txt"
        lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in norm_files),
                       encoding="utf-8")
        merged = tmp / "merged.mp4"
        r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "concat", "-safe", "0", "-i", str(lst),
                            "-c", "copy", str(merged)],
                           capture_output=True, timeout=timeout)
        if r.returncode != 0 or not merged.exists():
            warnings.append("拼接失败，回退为逐段重编码拼接")
            r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                                "-f", "concat", "-safe", "0", "-i", str(lst),
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                                "-pix_fmt", "yuv420p", str(merged)],
                               capture_output=True, timeout=timeout)
            if r.returncode != 0:
                return {"ok": False, "error": "视频拼接失败（ffmpeg concat 报错）"}

        total = _probe_duration(str(merged))
        cur = merged

        # ---- 3) 旁白混音 ----
        audios = [(s, _seg_path(s, books_dir) or "") for s in segs]
        audio_files = []
        t_cursor = 0.0
        for s in segs:
            ap = (getattr(s, "audio_path", "") or "").strip()
            if ap:
                rel = ap.split("/api/files/", 1)[-1].lstrip("/")
                a = books_dir / rel
                if a.exists():
                    audio_files.append((t_cursor, str(a)))
            t_cursor += float(getattr(s, "duration_seconds", 0) or 8)
        if audio_files:
            mixed = tmp / "mixed.mp4"
            args = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(cur)]
            for _, ap in audio_files:
                args += ["-i", ap]
            # 用 adelay 把每条旁白放到它的起点，再 amix
            filters = []
            for i, (start, _) in enumerate(audio_files, start=1):
                ms = int(start * 1000)
                filters.append(f"[{i}:a]adelay={ms}|{ms},volume=1.0[a{i}]")
            mix_inputs = "".join(f"[a{i}]" for i in range(1, len(audio_files) + 1))
            filters.append(f"{mix_inputs}amix=inputs={len(audio_files)}:"
                           f"duration=first:dropout_transition=0[aout]")
            args += ["-filter_complex", ";".join(filters),
                     "-map", "0:v", "-map", "[aout]",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                     "-shortest", str(mixed)]
            r = subprocess.run(args, capture_output=True, timeout=timeout)
            if r.returncode == 0 and mixed.exists() and mixed.stat().st_size > 0:
                cur = mixed
            else:
                warnings.append("旁白混音失败，成片将不含配音")
        else:
            warnings.append("该集没有旁白音频，成片为无声视频")

        # ---- 4) 烧硬字幕 ----
        burned = False
        if subs:
            srt = tmp / "sub.srt"
            srt.write_text(to_srt(subs), encoding="utf-8")
            font = find_cjk_font()
            if not font:
                warnings.append("未找到中文字体，跳过字幕烧录")
            else:
                styled = tmp / "styled.ass"
                ok_ass = _write_ass(subs, styled, font)
                final = tmp / "final.mp4"
                if ok_ass:
                    # 用 ASS 而非 SRT：能精确控字体/描边/位置，且规避
                    # ffmpeg subtitles 滤镜对中文路径与样式参数的转义坑
                    esc = str(styled).replace("\\", "/").replace(":", "\\:")
                    r = subprocess.run(
                        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                         "-i", str(cur), "-vf", f"ass='{esc}'",
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                         "-c:a", "copy", "-pix_fmt", "yuv420p", str(final)],
                        capture_output=True, timeout=timeout)
                    if r.returncode == 0 and final.exists() and final.stat().st_size > 0:
                        cur = final
                        burned = True
                    else:
                        warnings.append("字幕烧录失败（成片无字幕）")

        # ---- 5) 落盘 ----
        dest_dir = books_dir / out_rel_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        fname = f"film_ep{episode}_{time.strftime('%Y%m%d-%H%M%S')}.mp4"
        dest = dest_dir / fname
        shutil.copy2(cur, dest)
        rel = f"{out_rel_dir}/{fname}"
        return {"ok": True, "path": rel, "seconds": round(_probe_duration(str(dest)), 2),
                "segments": len(norm_files), "subtitles": len(subs),
                "burned": burned, "warnings": warnings}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "合成超时（分镜过多或单段过长），请减少集内分镜或调大超时"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "warnings": warnings}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _write_ass(subs: list[dict], path: pathlib.Path, font_file: str) -> bool:
    """写 ASS 字幕（样式：底部居中、白字黑边、描边够粗便于竖屏阅读）。"""
    try:
        name = pathlib.Path(font_file).stem
        head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {DEFAULT_W}
PlayResY: {DEFAULT_H}
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: NF,{name},42,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,1,2,40,40,80,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
        lines = [head]
        for s in subs:
            txt = (s["text"] or "").replace("\n", "\\N")
            lines.append(
                f"Dialogue: 0,{_ass_ts(s['start'])},{_ass_ts(s['end'])},NF,,0,0,0,,{txt}")
        path.write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False


def _ass_ts(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    if cs == 100:
        cs = 99
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _fallback_concat(segs: list, books_dir: pathlib.Path, out_rel_dir: str,
                     warnings: list[str]) -> dict:
    """无 ffmpeg 时的降级：字节拼接（同编码 mp4 多数播放器能连播）。

    ★ 为什么保留这条路径：生产机可能没装 ffmpeg。此时**宁可给一个能看的
    粗剪**，也不要直接失败——用户至少能拿到素材顺序正确的文件。
    """
    parts = []
    for s in segs:
        p = _seg_path(s, books_dir)
        if p:
            try:
                parts.append(pathlib.Path(p).read_bytes())
            except Exception:
                pass
    if not parts:
        return {"ok": False, "error": "无 ffmpeg 且分镜视频不可读"}
    warnings.append("环境无 ffmpeg：已用字节拼接降级（无字幕、无混音），"
                    "建议在服务器安装 ffmpeg 以获得完整成片")
    dest_dir = books_dir / out_rel_dir
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"film_raw_{time.strftime('%Y%m%d-%H%M%S')}.mp4"
    (dest_dir / fname).write_bytes(b"".join(parts))
    return {"ok": True, "path": f"{out_rel_dir}/{fname}", "seconds": 0,
            "segments": len(parts), "subtitles": 0, "burned": False,
            "warnings": warnings, "degraded": True}


__all__ = ["export_film", "build_subtitles", "to_srt", "find_cjk_font",
           "ffmpeg_available", "DEFAULT_W", "DEFAULT_H"]
