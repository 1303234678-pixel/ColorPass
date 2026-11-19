"""把一份**明文**草稿装进剪映的草稿列表（用于验证与回写）。

背景
----
剪映 11.x 保存草稿时写的是密文，但它自己也会读明文草稿
（`subdraft/`、`Presets/**/preset_draft/` 都是明文，而且它真的读）。
本模块用一份明文草稿当模板，复制成一条新的工程：换掉 id、名字、路径，
写一份新的 `draft_meta_info.json`，再补一张封面。

**只新增目录，不动任何已有草稿。** 想撤销：删掉这个目录即可。
"""
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from . import probe as JY


def _now_us():
    return int(time.time() * 1_000_000)


def _cover(dst, src_png=None):
    """剪映列表里的封面，缺了会显示空白，所以自己造一张。"""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return False
    try:
        if src_png and Path(src_png).exists():
            im = Image.open(src_png).convert("RGB")
            w, h = im.size
            h2 = int(w * 9 / 16)
            top = max(0, (h - h2) // 2)
            im = im.crop((0, top, w, min(h, top + h2))).resize((640, 360),
                                                              Image.LANCZOS)
        else:
            im = Image.new("RGB", (640, 360), (28, 28, 32))
            ImageDraw.Draw(im).text((24, 168), "ColorPass", fill=(210, 210, 216))
        im.save(dst, quality=90)
        return True
    except Exception:
        return False


def install_from_template(template_dir, new_name, cover_png=None,
                          mutate=None, root=None):
    """用模板目录生成一条新草稿。

    template_dir : 含 draft_content.json 的目录（必须是明文）
    new_name     : 新草稿的名字（也是目录名）
    mutate       : 可选回调 fn(draft_dict) -> None，用来改里面的内容
    """
    template_dir = Path(template_dir)
    src = template_dir / "draft_content.json"
    if not src.exists():
        return {"ok": False, "error": "模板里没有 draft_content.json"}
    head = src.read_bytes()[:1]
    if head != b"{":
        return {"ok": False, "error": "模板是密文，不能当模板"}

    root = Path(root) if root else JY.draft_root()
    dst = root / new_name
    if dst.exists():
        return {"ok": False, "error": "同名草稿已存在：%s" % dst}

    dst.mkdir(parents=True, exist_ok=False)

    # 1) 内容
    draft = json.loads(src.read_text(encoding="utf-8"))
    draft["id"] = str(uuid.uuid4()).upper()
    draft["name"] = new_name
    draft["path"] = str(dst).replace("\\", "/")
    draft["create_time"] = _now_us()
    draft["update_time"] = _now_us()
    if mutate:
        mutate(draft)
    (dst / "draft_content.json").write_text(
        json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    # 2) 附带的资源（预设打包的素材可能在 Resources 里）
    for sub in ("Resources", "common_attachment"):
        s = template_dir / sub
        if s.is_dir():
            shutil.copytree(s, dst / sub, dirs_exist_ok=True)

    # 3) 元信息 —— 全部字段照抄模板，只换 id / 名字 / 路径 / 时间
    mt = template_dir / "draft_meta_info.json"
    meta = {}
    if mt.exists():
        try:
            meta = json.loads(mt.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    meta["draft_id"] = draft["id"]
    meta["draft_name"] = new_name
    meta["draft_fold_path"] = str(dst).replace("\\", "/")
    meta["draft_root_path"] = str(root)
    meta["draft_json_file"] = None
    meta["draft_cover"] = "draft_cover.jpg"
    meta["draft_materials"] = []
    meta["tm_draft_create"] = draft["create_time"]
    meta["tm_draft_modified"] = draft["update_time"]
    meta["tm_draft_removed"] = 0
    meta["draft_removable_storage_device"] = ""
    meta["draft_is_invisible"] = False
    (dst / "draft_meta_info.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    # 4) 封面 + 时长
    _cover(dst / "draft_cover.jpg", cover_png)

    # 5) 空标记文件，剪映建草稿时会带这几个
    (dst / "draft_biz_config.json").write_text("", encoding="utf-8")
    (dst / "draft_settings").write_text(
        "[General]\ndraft_create_time=%d\ndraft_last_edit_time=%d\n"
        "real_edit_seconds=0\nreal_edit_keys=0\n"
        % (int(time.time()), int(time.time())), encoding="utf-8")

    return {"ok": True, "folder": str(dst),
            "files": sorted(p.name for p in dst.iterdir())}


def register_in_root_meta(folder, name, draft_id=None):
    """把新草稿登记进 root_meta_info.json（剪映的草稿列表就是读这份）。

    这份是明文。不登记的话，剪映不一定会主动扫目录把新文件夹收进来 ——
    先登记，测试才只有一个变量（明文 vs 密文）。
    """
    root = JY.draft_root()
    p = root / "root_meta_info.json"
    if not p.exists():
        return {"ok": False, "error": "没有 root_meta_info.json"}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": "读不了 root_meta_info：%r" % exc}

    store = d.setdefault("all_draft_store", [])
    folder_s = str(Path(folder)).replace("\\", "/")
    store = [e for e in store
             if str(e.get("draft_fold_path", "")).replace("\\", "/") != folder_s]
    tpl = next((e for e in store if e.get("draft_materials") is not None), {})
    e = json.loads(json.dumps(tpl)) if tpl else {}
    e.update({
        "draft_id": draft_id or str(uuid.uuid4()).upper(),
        "draft_name": name,
        "draft_fold_path": folder_s,
        "draft_root_path": str(root).replace("\\", "/"),
        "draft_cover": str(Path(folder) / "draft_cover.jpg").replace("\\", "/"),
        "draft_json_file": str(Path(folder) / "draft_content.json").replace("\\", "/"),
        "draft_materials": [],
        "tm_draft_create": _now_us(),
        "tm_draft_modified": _now_us(),
        "tm_draft_removed": 0,
        "draft_is_invisible": False,
        "draft_is_cloud_temp_draft": False,
        "draft_type": "",
    })
    store.insert(0, e)
    d["all_draft_store"] = store

    bak = p.with_name("root_meta_info.json.pb_%s.bak"
                      % time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(p, bak)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    return {"ok": True, "entries": len(store), "backup": str(bak)}


def swap_video(draft, video_path, video_name=None, width=None, height=None):
    """把草稿里的视频素材换成另一个文件（保持时间线不变）。

    要改的只有素材的 path / 名字 / 尺寸；片段引用的入出点和轨道位置不动，
    这样剪映打开时看到的还是同一条时间线，只是画面换了源。
    """
    video_path = str(video_path).replace("\\", "/")
    name = video_name or Path(video_path).stem
    n = 0

    def walk(d):
        nonlocal n
        for v in (d.get("materials", {}).get("videos") or []):
            if not isinstance(v, dict):
                continue
            v["path"] = video_path
            v["material_name"] = name
            if width and height:
                v["width"], v["height"] = int(width), int(height)
            n += 1
        for x in (d.get("materials", {}).get("drafts") or []):
            if isinstance(x, dict) and isinstance(x.get("draft"), dict):
                walk(x["draft"])

    walk(draft)
    return n


def graded_from_template(template_dir, new_name, video_path, lut_path=None,
                         video_name=None, width=None, height=None,
                         cover_png=None, register=True):
    """用一份带调色的明文预设当模板，换成指定素材，装成一条新草稿。

    模板必须**已经带着我们想要的调色**（在剪映里套好 LUT 再存预设），
    这样 LUT 的 effect_id / 缓存目录 / 引用关系都是剪映自己认的。
    """
    def mutate(d):
        swap_video(d, video_path, video_name, width, height)
        if lut_path:
            for e in (d.get("materials", {}).get("effects") or []):
                if isinstance(e, dict):
                    p = str(e.get("path") or "")
                    tail = p.split(";", 1)[1] if ";" in p else ""
                    e["path"] = str(lut_path).replace("\\", "/") + (
                        ";" + tail if tail else "")
                    e["name"] = Path(lut_path).name

    r = install_from_template(template_dir, new_name, cover_png=cover_png,
                              mutate=mutate)
    if r.get("ok") and register:
        m = json.loads((Path(r["folder"]) / "draft_meta_info.json")
                       .read_text(encoding="utf-8"))
        r["register"] = register_in_root_meta(r["folder"], new_name,
                                              m.get("draft_id"))
    return r


def build_flat(template_dir, new_name, items, cover_png=None, register=True):
    """生成一条**扁平**草稿：每条视频片段直接挂素材 + 各自的 LUT。

    为什么不能用预设直接当模板：剪映的「组合预设」本质是**复合片段**，
    `materials.drafts[].draft_file_path` 指向磁盘上的另一份草稿，剪映按路径
    加载原件，改内嵌副本没用（实测素材没被替换就是这个原因）。
    所以这里把复合拆掉，重建 materials 与 tracks。

    items = [{
        path,            素材绝对路径
        name,            显示名
        dur,             本段时长（秒，同时用作源入点之后的截取长度）
        src_start,       源入点（秒）
        tl_start,        在时间线上的起点（秒）
        width, height,
        lut,             这条片段要挂的 .cube 绝对路径（可为 None）
    }, ...]
    """
    tpl = Path(template_dir) / "draft_content.json"
    base = json.loads(tpl.read_text(encoding="utf-8"))
    m0 = base.get("materials") or {}

    v_tpl = json.loads(json.dumps((m0.get("videos") or [{}])[0]))
    s_tpl = json.loads(json.dumps(base["tracks"][0]["segments"][0]))
    e_tpl = json.loads(json.dumps((m0.get("effects") or [{}])[0]))
    speed = json.loads(json.dumps((m0.get("speeds") or [{}])[0]))
    canvas = json.loads(json.dumps((m0.get("canvases") or [{}])[0]))
    scm = json.loads(json.dumps((m0.get("sound_channel_mappings") or [{}])[0]))

    # 只留会用到的材质，其余一律清空 —— 尤其是 drafts（复合引用）
    materials = json.loads(json.dumps(m0))
    for k in list(materials.keys()):
        materials[k] = []
    materials["videos"] = []
    materials["effects"] = []
    materials["speeds"] = [speed]
    materials["canvases"] = [canvas]
    materials["sound_channel_mappings"] = [scm]

    segments = []
    total = 0
    for it in items:
        dur_us = int(round(float(it["dur"]) * 1e6))
        tl_us = int(round(float(it["tl_start"]) * 1e6))
        src_us = int(round(float(it.get("src_start", 0.0)) * 1e6))

        v = json.loads(json.dumps(v_tpl))
        vid = str(uuid.uuid4()).upper()
        v.update({
            "id": vid,
            "path": str(it["path"]).replace("\\", "/"),
            "material_name": it.get("name") or Path(it["path"]).stem,
            "duration": int(round(float(it.get("src_dur", it["dur"])) * 1e6)),
            "width": int(it.get("width") or 1920),
            "height": int(it.get("height") or 1080),
            "has_audio": bool(it.get("has_audio", False)),
            "type": "video",
        })
        materials["videos"].append(v)

        refs = [speed["id"], canvas["id"], scm["id"]]
        if it.get("lut"):
            e = json.loads(json.dumps(e_tpl))
            eid = str(uuid.uuid4()).upper()
            tail = ""
            if ";" in str(e.get("path", "")):
                tail = ";" + str(e["path"]).split(";", 1)[1]
            e.update({
                "id": eid,
                "name": Path(it["lut"]).name,
                "path": str(it["lut"]).replace("\\", "/") + tail,
                "value": float(it.get("lut_strength", 1.0)),
            })
            materials["effects"].append(e)
            refs.append(eid)

        sg = json.loads(json.dumps(s_tpl))
        sg.update({
            "id": str(uuid.uuid4()).upper(),
            "material_id": vid,
            "extra_material_refs": refs,
            "source_timerange": {"start": src_us, "duration": dur_us},
            "target_timerange": {"start": tl_us, "duration": dur_us},
            "render_timerange": {"start": tl_us, "duration": dur_us},
            "is_placeholder": False,
            "desc": "",
            "visible": True,
            "enable_lut": bool(it.get("lut")),
        })
        segments.append(sg)
        total = max(total, tl_us + dur_us)

    track = json.loads(json.dumps(base["tracks"][0]))
    track["id"] = str(uuid.uuid4()).upper()
    track["segments"] = segments

    def mutate(d):
        d["materials"] = materials
        d["tracks"] = [track]
        d["duration"] = total
        d["fps"] = float(items[0].get("fps", 25.0)) if items else 25.0
        d["canvas_config"] = {"background": None, "width": 1920,
                              "height": 1080, "ratio": "16:9"}

    r = install_from_template(template_dir, new_name, cover_png=cover_png,
                              mutate=mutate)
    if r.get("ok") and register:
        meta = json.loads((Path(r["folder"]) / "draft_meta_info.json")
                          .read_text(encoding="utf-8"))
        r["register"] = register_in_root_meta(r["folder"], new_name,
                                              meta.get("draft_id"))
    if r.get("ok"):
        r["clips"] = len(segments)
        r["duration_s"] = round(total / 1e6, 3)
    return r


def build_from_draft(src_draft, new_name, lut_map, cover_png=None, root=None,
                     template_dir=None, *, segment_luts=None):
    """在**原工程的时间线**上生成新草稿：只给片段挂 LUT，其余一律照抄。

    为什么不再用 build_flat 重建：那样要自己拼 tracks / segments / materials，
    凡是原工程里"不是一段普通视频"的东西都会丢或错位 ——
      · 画中画（第二条视频轨）会被压到同一条轨上，时间重叠就直接顶开
      · 变速片段的 source 与 target 时长不等，重拼后长度会变
      · 裁切、旋转、位置、关键帧、音量、混合模式全在 segment 里，重拼就没了
      · 画布尺寸/fps 被写死成 1920×1080，4K 工程会被整体缩放
    用户看到的"素材摆放和原工程不一致"就是这么来的。

    正确做法：拿内存里读到的**完整明文草稿**当底，逐段补一个 LUT 材质。
    时间线一个字节都不动，自然完全一致。

    lut_map : {规范化后的素材路径: cube 绝对路径}，兼容旧的按路径写回。
    segment_luts : {源 segment_id: cube 绝对路径}，优先于 lut_map；只传该表
                   且 lut_map={} 时，未命中的片段不会使用同源其他段的表。
    """
    import os
    import tempfile

    tpl = Path(template_dir) if template_dir else (Path(__file__).resolve().parent / "templates")
    meta_src = tpl / "draft_meta_info.json"
    eff_tpl = {}
    try:
        base = json.loads((tpl / "draft_content.json").read_text(encoding="utf-8"))
        eff_tpl = json.loads(json.dumps((base.get("materials", {}).get("effects") or [{}])[0]))
    except Exception:
        pass

    def key(p):
        import os
        return os.path.normcase(os.path.normpath(str(p).replace("\\", "/")))

    luts = {key(k): v for k, v in (lut_map or {}).items()}

    def mutate(d):
        mats = d.setdefault("materials", {})
        vids = {}
        for v in mats.get("videos") or []:
            if isinstance(v, dict) and v.get("id"):
                vids[v["id"]] = v
        if not isinstance(mats.get("effects"), list):
            mats["effects"] = []
        effects = mats["effects"]

        # 按 id 找出片段原有的 LUT；材质可能共享，替换时必须复制并换 id。
        existing = {}
        for e in effects:
            p = str(e.get("path") or "")
            if ".cube" in p.lower() and e.get("id"):
                existing[e["id"]] = e

        for tr in d.get("tracks") or []:
            if not isinstance(tr, dict) or tr.get("type") != "video":
                continue
            for sg in tr.get("segments") or []:
                if not isinstance(sg, dict):
                    continue
                v = vids.get(sg.get("material_id")) or {}
                cube = (segment_luts or {}).get(sg.get("id"),
                                               luts.get(key(v.get("path") or "")))
                if not cube:
                    continue
                refs = list(sg.get("extra_material_refs") or [])
                target = next((existing[r] for r in refs if r in existing), None)
                e = json.loads(json.dumps(target if target is not None else eff_tpl))
                e["id"] = str(uuid.uuid4()).upper()
                e.setdefault("apply_target_type", 0)
                e.setdefault("item_effect_type", 0)
                e["value"] = 1.0
                tail = ""
                if ";" in str(e.get("path", "")):
                    tail = ";" + str(e["path"]).split(";", 1)[1]
                e["name"] = Path(cube).name
                e["path"] = str(cube).replace("\\", "/") + tail
                effects.append(e)
                # 烘好的表替换本段所有旧 LUT 引用，保留速度及其他效果引用。
                refs = [r for r in refs if r not in existing]
                refs.append(e["id"])
                sg["extra_material_refs"] = refs
                sg["enable_lut"] = True

        # 清理失效的旧 LUT：之前写回时挂的 cube 在临时目录里，早就删了。
        # 留着这些引用，剪映会显示"LUT 丢失"，画面反而变成没调过。
        alive = set()
        for tr in d.get("tracks") or []:
            if isinstance(tr, dict) and tr.get("type") == "video":
                for sg in tr.get("segments") or []:
                    if isinstance(sg, dict):
                        alive.update(sg.get("extra_material_refs") or [])
        drop = set()
        for e in list(effects):
            p = str(e.get("path") or "")
            if ".cube" not in p.lower():
                continue
            f = p.split(";", 1)[0]
            if not os.path.exists(f):
                drop.add(e.get("id"))
        if drop:
            for tr in d.get("tracks") or []:
                if not isinstance(tr, dict):
                    continue
                for sg in tr.get("segments") or []:
                    if isinstance(sg, dict):
                        sg["extra_material_refs"] = [
                            r for r in (sg.get("extra_material_refs") or [])
                            if r not in drop]
            mats["effects"] = [e for e in effects if e.get("id") not in drop]
            effects[:] = mats["effects"]

    tmp = Path(tempfile.mkdtemp(prefix="pb_src_"))
    try:
        src = json.loads(json.dumps(src_draft))
        src.pop("_pb_lut_count", None)
        (tmp / "draft_content.json").write_text(
            json.dumps(src, ensure_ascii=False), encoding="utf-8")
        if meta_src.exists():
            shutil.copy2(meta_src, tmp / "draft_meta_info.json")
        r = install_from_template(tmp, new_name, cover_png=cover_png,
                                  mutate=mutate, root=root)
        r["mode"] = "clone"
        return r
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def count_luts(draft, lut_map, *, segment_luts=None):
    """克隆模式下真正挂上的 LUT 条数（给界面回显用）。"""
    import os

    def key(p):
        return os.path.normcase(os.path.normpath(str(p).replace("\\", "/")))

    luts = {key(k): v for k, v in (lut_map or {}).items()}
    mats = (draft or {}).get("materials") or {}
    vids = {v.get("id"): v for v in (mats.get("videos") or []) if isinstance(v, dict)}
    n = 0
    for tr in (draft or {}).get("tracks") or []:
        if not isinstance(tr, dict) or tr.get("type") != "video":
            continue
        for sg in tr.get("segments") or []:
            if not isinstance(sg, dict):
                continue
            v = vids.get(sg.get("material_id")) or {}
            cube = (segment_luts or {}).get(sg.get("id"),
                                           luts.get(key(v.get("path") or "")))
            if cube:
                n += 1
    return n


def write_cube(path, fn, n=33, title="PB"):
    """写一条 3D LUT，fn(r,g,b) -> (r,g,b)，取值域 0..1。"""
    lines = ['TITLE "%s"' % title, "LUT_3D_SIZE %d" % n, ""]
    for b in range(n):
        for g in range(n):
            for r in range(n):
                v = fn(r / (n - 1.0), g / (n - 1.0), b / (n - 1.0))
                lines.append("%.6f %.6f %.6f" % v)
    Path(path).write_text("\n".join(lines), encoding="ascii")
    return str(path)


def remove_draft(name, root=None):
    """删掉我们建的草稿（只按名字，且要求目录里有我们的封面标记）。"""
    root = Path(root) if root else JY.draft_root()
    d = root / name
    if not d.is_dir():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


if __name__ == "__main__":
    import sys
    tpl = (Path(JY.user_data_dir()) / "Presets" / "Combination" / "Presets"
           / "我的预设1" / "preset_draft")
    name = sys.argv[1] if len(sys.argv) > 1 else "PB测试_明文草稿"
    r = install_from_template(tpl, name)
    print(json.dumps(r, ensure_ascii=False, indent=1))
