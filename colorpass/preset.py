"""从剪映的**明文副本**里取出工程内容（片段路径、入出点、轨道位置）。

为什么有这条路
--------------
剪映 6.0 起的 draft_content.json 是密文（本机 11.7.0，熵 7.95）。
但剪映自己会写**明文**的草稿副本，而且它自己会读回去：

  1. `Presets/Combination/Presets/<名字>/preset_draft/draft_content.json`
     —— 时间轴里选中片段 → 右键「保存为预设」写出来的。
        全量 JSON，含 `materials.videos[].path`（绝对路径）与
        `tracks[].segments[]`（source/target 时间范围）。

  2. `Projects/com.lveditor.draft/<名字>/subdraft/**/draft_content.json`
     —— 复合片段（嵌套草稿）的明文副本。

这不是什么偏门手段：凌萌盒（LmBox 彩虹桥）官方文档对「高于 6.0 的高版本
剪映草稿」给的就是这个办法 —— 先让用户在剪映里右键「保存为预设」，
再读这棵树。所以「剪映工程转 PR」那类工具，也没有破解加密。

本模块只做只读解析，不改剪映的任何文件。

只保留读草稿所需的最小解析
--------------------------
「浏览剪映预设库 / 复合片段目录」那整套能力（扫目录、按素材去重、给界面
的一句话摘要）已经删掉了 —— 瘦身后只剩「读草稿 → 一级调色 → 写回」，
读草稿这条链只用得到下面两个函数（`jy_workflow` 直接调）。
"""
import json
from pathlib import Path


def _load(p):
    """读一份 JSON；读不出来就返回 None（调用方自己决定怎么退）。"""
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return None


def _clips_of(draft, where, depth=0):
    """把一份草稿 JSON 里的片段全部摊平。

    嵌套复合（materials.drafts[].draft）会递归进去 —— 剪映把一个复合片段
    存成「内嵌一整个草稿」，不递归就会漏掉里面的东西。
    """
    out = []
    if not isinstance(draft, dict) or depth > 6:
        return out
    mats = draft.get("materials") or {}

    vids = {}
    for v in mats.get("videos") or []:
        if isinstance(v, dict) and v.get("id"):
            vids[v["id"]] = v

    for track_index, tr in enumerate(draft.get("tracks") or []):
        if not isinstance(tr, dict) or tr.get("type") != "video":
            continue
        for segment_index, sg in enumerate(tr.get("segments") or []):
            if not isinstance(sg, dict):
                continue
            mid = sg.get("material_id")
            v = vids.get(mid) or {}
            src = sg.get("source_timerange") or {}
            tgt = sg.get("target_timerange") or {}
            out.append({
                "track_index": track_index,
                "segment_index": segment_index,
                "segment_id": sg.get("id"),
                "path": str(v.get("path") or "").replace("\\", "/"),
                "name": v.get("material_name") or v.get("name") or "",
                "src_start": (src.get("start") or 0) / 1e6,
                "src_dur": (src.get("duration") or 0) / 1e6,
                "tl_start": (tgt.get("start") or 0) / 1e6,
                "tl_dur": (tgt.get("duration") or 0) / 1e6,
                "where": where,
                "nested": depth > 0,
            })

    # 内嵌复合
    for d in mats.get("drafts") or []:
        if isinstance(d, dict) and isinstance(d.get("draft"), dict):
            out += _clips_of(d["draft"], where, depth + 1)
    return out
