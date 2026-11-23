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

from . import platforms
from . import probe as JY


def _now_us():
    return int(time.time() * 1_000_000)


def _write(path, text):
    """原子写（先临时文件再 replace）—— 见 `paths.atomic_write` 的说明。"""
    from . import paths
    paths.atomic_write(path, text)


def _body_name():
    """草稿正文的文件名。

    Windows（剪映专业版 11.x）是 `draft_content.json`；macOS 11.8 是
    `draft_info.json`，而且分了「草稿根」与 `Timelines/<id>/` 两份。写错名字
    的后果是静默的：目录建出来了、列表里也登记了，点进去才发现草稿是空的。
    """
    return "draft_info.json" if platforms.is_mac() else "draft_content.json"


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

    # 目录建出来之后，任何一步失败（磁盘满、模板 json 坏了、资源复制失败）
    # 都要把这条目录删掉。留着一条缺 draft_meta_info 的半成品，剪映列表里
    # 就是一条点不开的草稿 —— 用户不知道它是哪来的，也不敢删。
    try:
        # 1) 内容
        draft = json.loads(src.read_text(encoding="utf-8"))
        draft["id"] = str(uuid.uuid4()).upper()
        draft["name"] = new_name
        draft["path"] = str(dst).replace("\\", "/")
        draft["create_time"] = _now_us()
        draft["update_time"] = _now_us()
        if mutate:
            mutate(draft)
        _write(dst / "draft_content.json",
               json.dumps(draft, ensure_ascii=False))

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
        _write(dst / "draft_meta_info.json",
               json.dumps(meta, ensure_ascii=False, indent=1))

        # 4) 封面 + 时长
        _cover(dst / "draft_cover.jpg", cover_png)

        # 5) 空标记文件，剪映建草稿时会带这几个
        _write(dst / "draft_biz_config.json", "")
        _write(dst / "draft_settings",
               "[General]\ndraft_create_time=%d\ndraft_last_edit_time=%d\n"
               "real_edit_seconds=0\nreal_edit_keys=0\n"
               % (int(time.time()), int(time.time())))

        return {"ok": True, "folder": str(dst),
                "files": sorted(p.name for p in dst.iterdir())}
    except Exception:
        shutil.rmtree(dst, ignore_errors=True)
        raise


def install_mac_from_source(src_folder, draft, new_name, mutate=None, root=None,
                            snapshot_dir=None):
    """macOS 布局：以**源草稿目录为骨架**生成一份新草稿。

    为什么不能沿用 Windows 那套 `draft_content.json`：macOS 剪映 11.8 的草稿
    是三层结构，磁盘上根本没有 `draft_content.json` 这个文件 ——

        <草稿>/
          draft_info.json          正文（密文；打开时按当前时间线镜像一份）
          draft_meta_info.json     草稿元信息（密文）
          draft_settings           创建/最后编辑时间（明文 ini）
          timeline_layout.json     哪条时间线在台前（明文）
          Resources/ common_attachment/ subdraft/ …   素材与复合片段
          Timelines/
            project.json           主时间线 id + 时间线清单（明文）
            <timeline_id>/draft_info.json   这条时间线的正文（密文）
            <timeline_id>/template.tmp      空骨架（**明文**）

    所以写回照 Windows 那套写 `draft_content.json`，剪映连草稿都认不出来。
    这里整目录复制源草稿（Resources / subdraft / Timelines 全带上，复合片段
    的子草稿因此天然跟着走），再把正文改写成**明文** `draft_info.json` ——
    11.7+ 的剪映自己会读明文并在保存时重新加密（社区实现 easy-jianying 在
    macOS 上用的就是这条路）。

    时间线 id 保持原样：它只在草稿目录内自洽（project.json ↔ Timelines/<id>/
    ↔ draft_info.json.id），换 id 反而要多改三处引用，得不偿失。
    """
    src_folder = Path(src_folder)
    root = Path(root) if root else JY.draft_root()
    dst = root / new_name
    if dst.exists():
        return {"ok": False, "error": "同名草稿已存在：%s" % dst}
    if not src_folder.is_dir():
        # 送入之后在剪映里把原草稿删了/改名了。旧文案是「不是 macOS 布局」，
        # 用户看到只会以为插件坏了 —— 真正的原因跟操作完全对不上。
        return {"ok": False,
                "error": "送入时的那份剪映草稿已经不在原处（%s）。"
                         "写回需要它的目录骨架（Timelines/Resources/subdraft）。"
                         "请在剪映里重新打开那份草稿再「送入」一次，"
                         "或先把它从回收站恢复。" % src_folder}
    proj = src_folder / "Timelines" / "project.json"
    if not proj.exists():
        return {"ok": False,
                "error": "源草稿缺少 Timelines/project.json（不是 mac 剪映的草稿结构）：%s"
                         % src_folder}
    try:
        pj = json.loads(proj.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": "读不了 Timelines/project.json：%r" % exc}
    tid = str(pj.get("main_timeline_id") or "")
    tl_src = src_folder / "Timelines" / tid
    if not tid or not tl_src.is_dir():
        return {"ok": False, "error": "源草稿的主时间线目录不存在：%s" % (tid or "（空）")}

    now_us = _now_us()
    # 先建到隐藏的暂存目录，全部写完之后再 rename 成正式名字。
    # 为什么：整目录复制 + 四份正文 + 元信息要好几百毫秒到几秒，中途关窗口、
    # 崩溃、磁盘满都会在剪映的草稿根里留下一个**半成品目录** —— 剪映下次启动
    # 扫到它会列出一条点不开的草稿，用户不知道那是什么，也不敢删。
    # 同一分区内 rename 是原子的：要么没有目录，要么是一份完整的。
    stage = root / (".cp-staging-%s" % uuid.uuid4().hex[:10])
    # .locked 是剪映自己开的标记（复制过来等于宣称草稿正被占用）；
    # .backup 是旧草稿的崩溃回滚点，带进新草稿只会在出问题时把旧内容救回来；
    # 根目录的 draft_info.json.bak 是**旧密文的备份**，留着等于给剪映留了一个
    # 「恢复成没调色那版」的入口。
    skip = shutil.ignore_patterns(".locked", ".backup", "*.pb_*.bak",
                                  "draft_info.json.bak")
    # 源草稿通常正开在剪映里（刚调完色就是这么用的），它随时可能重写自己的
    # 文件：复制到一半某个文件被换掉 → copytree 抛 FileNotFoundError。
    # 这不是「草稿坏了」，重试一次就好 —— 不重试的话用户看到的是
    # 「复制源草稿目录失败：FileNotFoundError: ...」，完全无从下手。
    last = None
    for attempt in range(2):
        try:
            shutil.copytree(src_folder, stage, ignore=skip, symlinks=True)
            last = None
            break
        except Exception as exc:
            last = exc
            shutil.rmtree(stage, ignore_errors=True)
            if attempt == 0:
                time.sleep(0.35)
    if last is not None:
        return {"ok": False,
                "error": "复制源草稿目录失败：%s: %s（剪映可能正在写这份草稿，"
                         "稍等一两秒再点一次写回）" % (type(last).__name__, last)}

    published = False
    try:
        d = json.loads(json.dumps(draft))
        d.pop("_pb_lut_count", None)
        d["path"] = str(dst).replace("\\", "/")
        d["create_time"] = now_us
        d["update_time"] = now_us
        if mutate:
            mutate(d)
        body = json.dumps(d, ensure_ascii=False)

        # 正文写三处：草稿根的 draft_info.json 是「打开时那份镜像」，
        # Timelines/<id>/draft_info.json 才是时间线本体，template-2.tmp 是
        # 剪映落盘时留的第二份拷贝。三份以前是同一份密文的三个副本，
        # 现在统一写成同一份明文，避免谁把旧版盖回来。
        tl = stage / "Timelines" / tid
        for target in (stage / "draft_info.json", tl / "draft_info.json",
                       stage / "template-2.tmp", tl / "template-2.tmp"):
            # 原子写：剪映可能正开着另一份草稿在读这些文件，截断再写会读到半截。
            _write(target, body)
        for stale in (tl / "draft_info.json.bak",):
            try:
                stale.unlink()
            except OSError:
                pass

        # 元信息：源草稿那份是密文，这边按同样的字段写一份明文（draft_id 换新，
        # 否则剪映列表里两条草稿是同一个 id）。
        # 注意路径字段要写**最终目录**（dst），不能写暂存目录。
        draft_id = str(uuid.uuid4()).upper()
        meta = _mac_meta(src_folder, dst, root, new_name, draft_id, now_us)
        _write(stage / "draft_meta_info.json",
               json.dumps(meta, ensure_ascii=False, indent=1))

        ts = int(now_us / 1_000_000)
        _write(stage / "draft_settings",
               "[General]\ncloud_last_modify_platform=mac\n"
               "draft_create_time=%d\ndraft_last_edit_time=%d\n"
               "enable10bit=false\nreal_edit_keys=0\nreal_edit_seconds=0\n"
               % (ts, ts))
        if not (stage / "draft_cover.jpg").exists():
            _cover(stage / "draft_cover.jpg", None)

        # 全部写完，一次 rename 到位：剪映的草稿根里从此只可能出现
        # 「根本没有」或「完整的一份」，不会有半成品。
        try:
            os.replace(str(stage), str(dst))
        except OSError:
            # Windows 上 os.replace 对目录会失败（目标不存在时 rename 才行）。
            # 若另一个窗口恰好抢先建了同名草稿，失败方只能清自己的暂存目录，
            # 绝不能碰 dst —— 那已经是对方发布的完整草稿。
            if dst.exists():
                shutil.rmtree(stage, ignore_errors=True)
                return {"ok": False, "error": "同名草稿已存在（另一个写回可能刚完成）：%s"
                                              % dst}
            os.rename(str(stage), str(dst))
        published = True

        # 登记进草稿列表。mac 剪映首页读的是 root_meta_info.json，不登记的话
        # 目录在、草稿不在 —— 用户只会看到「写回完成」却找不到新草稿。
        # 所以这里登记失败**不能**当成成功：以前它只把结果塞在返回值里，
        # 上层没人看，于是「写回成功 + 列表里没有」这种最迷惑的状态能一路走通。
        reg = register_in_root_meta(dst, new_name, draft_id)
        if not reg.get("ok"):
            return {"ok": False, "folder": str(dst), "draft_id": draft_id,
                    "timeline_id": tid, "register": reg,
                    "error": "新草稿目录建好了，但没能登记进剪映的草稿列表"
                             "（%s）。剪映首页不会显示它。\n"
                             "目录：%s\n"
                             "处理：确认剪映草稿目录可写后重新写回；"
                             "或直接在剪映里用「导入草稿」选这个目录。"
                             % (reg.get("error") or "未知原因", dst)}
        return {"ok": True, "folder": str(dst), "draft_id": draft_id,
                "timeline_id": tid, "register": reg,
                "files": sorted(p.name for p in dst.iterdir())}
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        if published:
            shutil.rmtree(dst, ignore_errors=True)
        raise


def _mac_meta(src_folder, dst, root, name, draft_id, now_us):
    """新草稿的 draft_meta_info：源草稿那份是密文，字段照抄、值换新。

    读不到源元信息（剪映没开、读取器不在）也不中止：这份文件是草稿列表的
    冗余信息，根目录的 root_meta_info.json 才是索引。缺字段时按最小集写。
    """
    meta = {}
    try:
        from . import macdraft
        raw = macdraft.decrypt(str(Path(src_folder) / "draft_meta_info.json"))
        meta = json.loads(raw) if raw.strip() else {}
    except Exception:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update({
        "draft_id": draft_id,
        "draft_name": name,
        "draft_fold_path": str(dst).replace("\\", "/"),
        "draft_root_path": str(root).replace("\\", "/"),
        "draft_cover": "draft_cover.jpg",
        "draft_materials": [],
        "tm_draft_create": now_us,
        "tm_draft_modified": now_us,
        "tm_draft_removed": 0,
        "draft_removable_storage_device": "",
        "draft_is_invisible": False,
    })
    return meta


def register_in_root_meta(folder, name, draft_id=None):
    """把新草稿登记进 root_meta_info.json（剪映的草稿列表就是读这份）。

    这份是明文。不登记的话，剪映不一定会主动扫目录把新文件夹收进来 ——
    先登记，测试才只有一个变量（明文 vs 密文）。

    并发安全（2026-11-23）：整段「读—改—写」在跨进程锁里做，写盘走
    `paths.atomic_write`。以前固定用 `root_meta_info.json.tmp` 当临时名，两个
    窗口同时登记时，一边 os.replace 之后另一边直接 ENOENT 崩掉；而且没有锁，
    后写的那份会把先写的条目整个盖掉（实测 8 并发只剩 1~2 条）。
    """
    from . import paths
    # 只**新增**条目，绝不删别人的（见 `_register_locked` 里的过滤条件只按
    # 本目录路径去重）。这条不是洁癖：实测（真机 11.8，剪映开着草稿）把
    # 别人的条目从这份索引里去掉之后，剪映会自己做一次对账，把「索引里没有」
    # 的草稿目录整份删掉（日志里是 `delete_draft_external`）——
    # 也就是说，索引不是只读缓存，删条目等于删草稿。
    root = JY.draft_root()
    p = root / "root_meta_info.json"
    if not p.exists():
        # 剪映自己会建这份索引；它不在，多半是草稿根指错了（或还没启动过剪映）。
        return {"ok": False, "error": "没有 root_meta_info.json（草稿根：%s）" % root}
    try:
        with paths.FileLock(p):
            return _register_locked(p, root, folder, name, draft_id)
    except TimeoutError as exc:
        return {"ok": False, "error": "草稿索引正被另一个窗口占用，登记超时（%s）" % exc}
    except OSError as exc:
        # 数据目录不可写（权限、磁盘满）时连锁都建不出来 —— 别让它冒成 traceback。
        return {"ok": False, "error": "建不了登记锁：%s: %s"
                                      % (type(exc).__name__, exc)}


def _register_locked(p, root, folder, name, draft_id):
    from . import paths
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": "读不了 root_meta_info：%r" % exc}
    if not isinstance(d, dict):
        return {"ok": False, "error": "root_meta_info 不是 JSON 对象，拒绝改写"}

    store = d.get("all_draft_store")
    if not isinstance(store, list):
        store = []
    folder_s = str(Path(folder)).replace("\\", "/")
    store = [e for e in store if isinstance(e, dict) and
             str(e.get("draft_fold_path", "")).replace("\\", "/") != folder_s]
    tpl = next((e for e in store if e.get("draft_materials") is not None), {})
    e = json.loads(json.dumps(tpl)) if tpl else {}
    e.update({
        "draft_id": draft_id or str(uuid.uuid4()).upper(),
        "draft_name": name,
        "draft_fold_path": folder_s,
        "draft_root_path": str(root).replace("\\", "/"),
        "draft_cover": str(Path(folder) / "draft_cover.jpg").replace("\\", "/"),
        # macOS 的正文叫 draft_info.json；写成 draft_content.json 的话，
        # 剪映列表里那条草稿是「点不开」的（索引指向不存在的文件）。
        "draft_json_file": str(Path(folder) / _body_name()).replace("\\", "/"),
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

    bak = p.with_name("root_meta_info.json.pb_%s_%d.bak"
                      % (time.strftime("%Y%m%d_%H%M%S"), os.getpid()))
    try:
        shutil.copy2(p, bak)
    except Exception:
        bak = None
    # 备份只留最近一份。这份文件就躺在剪映的草稿根目录里，用户看得见；
    # 每次登记都留一份，往返几十次之后根目录里就是几十个 .bak。
    for old in sorted(p.parent.glob("root_meta_info.json.pb_*.bak"),
                      key=lambda f: f.name)[:-1]:
        try:
            old.unlink()
        except OSError:
            pass
    try:
        paths.atomic_write(p, json.dumps(d, ensure_ascii=False))
    except Exception as exc:
        return {"ok": False, "error": "写不进 root_meta_info：%s: %s"
                                      % (type(exc).__name__, exc)}
    return {"ok": True, "entries": len(store),
            "backup": str(bak) if bak else ""}


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
                     template_dir=None, *, segment_luts=None, src_folder=None):
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
    src_folder : 源草稿目录。macOS 必传 —— 那边要整目录克隆骨架（Resources /
                 Timelines / subdraft），否则写出来的草稿剪映打不开。
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

    if platforms.is_mac():
        # macOS 上不可能靠一份「明文模板目录」装出草稿：骨架（Resources /
        # Timelines / subdraft / 各种 attachment）全在源草稿目录里，缺一样
        # 剪映就认为这份草稿不完整。所以整目录克隆，只改正文。
        if not src_folder:
            return {"ok": False,
                    "error": "macOS 写回需要源草稿目录（用来克隆骨架），"
                             "调用方没有传 src_folder"}
        try:
            r = install_mac_from_source(src_folder, src_draft, new_name,
                                        mutate=mutate, root=root)
        except Exception as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        r["mode"] = "clone"
        return r

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


if __name__ == "__main__":
    import sys
    tpl = (Path(JY.user_data_dir()) / "Presets" / "Combination" / "Presets"
           / "我的预设1" / "preset_draft")
    name = sys.argv[1] if len(sys.argv) > 1 else "PB测试_明文草稿"
    r = install_from_template(tpl, name)
    print(json.dumps(r, ensure_ascii=False, indent=1))
