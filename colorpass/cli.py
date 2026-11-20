"""统一入口：一个命令做完「送入 / 取回 / 写回 / 锁」，并且先过使用前闸门。

    python -m colorpass.cli gate               只做前置条件检查（不动任何东西）
    python -m colorpass.cli status             秒级状态：剪映开没开、哪份草稿、上次送入
    python -m colorpass.cli check              看当前草稿状态（要扫剪映内存，慢）
    python -m colorpass.cli audit              全项体检：结构/素材/时长/风险
    python -m colorpass.cli send [--speed=bake|slot|source]
    python -m colorpass.cli pull
    python -m colorpass.cli write [--name 新草稿名]
    python -m colorpass.cli lock [on|off]
    python -m colorpass.cli roundtrip [--speed=bake|slot|source]
    python -m colorpass.cli reset              丢掉「没走完的那次送入」
    python -m colorpass.cli clean [--draft-id] 清变速预渲染缓存
    python -m colorpass.cli gc [--apply]       回收过期缓存（默认只报告，不动手）
    python -m colorpass.cli doctor             环境与兼容性报告（JSON，贴 issue 用）

前置条件是硬的（用户要求，也是实测后果）：
  · **送入**要求剪映在跑且目标草稿确实打开（明文只在它内存里）、达芬奇在跑
    且是**空白工程**（否则会改掉工程帧率、并用绝对帧号覆盖已有片段）
  · **写回**只要求剪映进程在、达芬奇脚本接口连得上 —— 它按「送入时的快照」
    重建草稿，不读剪映内存，所以你中途怎么剪都不影响这次往返
任一不满足就停，不写一个字。

一次送入、之后随便改：送入那一刻的明文草稿整份存进
%LOCALAPPDATA%\\ColorPass\\runs\\<草稿ID>\\snapshot\\，写回永远按这一版走。

面板调用的就是这些子命令，所以命令行与面板永远同一条链路。
"""
import argparse
import json
import os
import sys

from . import draftio, paths


def _positive_int(text):
    """argparse 的 type：只收 ≥ 1 的整数。

    --keep-days 0 / --max-mb 0 看着像「不限制」，在 plan() 里却是「全都超过
    上限」—— 会把除活动草稿外的缓存一次删光。宁可当场报错。
    """
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("要一个整数，收到 %r" % text)
    if v < 1:
        raise argparse.ArgumentTypeError("必须 ≥ 1，收到 %d" % v)
    return v


def _gate_out(rep, e):
    """把 Gate 异常变成结构化输出，返回退出码。"""
    rep.update(e.as_dict())
    rep["error"] = e.msg
    print(json.dumps({"ok": False, "gate": e.code, "error": e.msg,
                      "detail": e.detail}, ensure_ascii=False, indent=1,
                     default=str))
    return 4


def _draft_out(action, e):
    """草稿读不出来 / 内存版本对不上：结构化输出 + 可执行建议，别冒 traceback。

    面板只看得见 stdout 的最后一段 JSON；直接抛异常的话用户只看到
    「未完成（退出码 1）」，完全不知道要「关掉草稿再打开」。
    """
    if isinstance(e, draftio.StaleDraft):
        code = "stale_draft"
    elif isinstance(e, draftio.NoPlaintext):
        # 草稿认出来了，只是明文没了 —— 处理办法（关掉重开）和「没打开草稿」
        # 不一样，面板要按这个码换一套说明。
        code = "no_plaintext"
    else:
        code = "no_draft"
    print(json.dumps({"ok": False, "action": action, "gate": code,
                      "error": str(e)}, ensure_ascii=False, indent=1,
                     default=str))
    return 3


def _preflight(mode, fresh=False):
    """mode: both | davinci | jianying

    只有送入会自动建工程；取回/写回必须落在**已有**工程上，别替用户新建
    一个空工程（那样就找不到要取回的那条时间线了）。

    fresh=True：「这份草稿打开之后又被编辑过」就直接拦（只看磁盘时间，
    不扫内存，几十毫秒出结果）。只有送入要 —— 写回按快照重建，不受影响。
    """
    from . import preflight
    if mode == "both":
        return preflight.both(auto_project=True, require_fresh=fresh)
    if mode == "davinci":
        r, proj, info = preflight.daVinci(require_blank=False,
                                          auto_project=False)
        return {"davinci": info, "resolve": r, "project": proj}
    return {"jianying": preflight.jianying(require_fresh=fresh)}


def cmd_gate(a):
    try:
        info = _preflight("both")
    except Exception as e:
        from . import preflight
        if isinstance(e, preflight.Gate):
            return _gate_out({}, e)
        raise
    try:
        from . import retime
        ff = retime.available()
    except Exception:
        ff = False
    print(json.dumps({"ok": True, "status": _describe(info),
                      "detail": {**{k: v for k, v in info.items()
                                    if k not in ("resolve", "project")},
                                 "ffmpeg": ff},
                      "hint": ("" if ff else
                               "找不到 ffmpeg：变速只能用 slot/source（预渲染模式 "
                               "bake 不可用）")},
                     ensure_ascii=False, indent=1, default=str))
    return 0


def _describe(info):
    from . import preflight
    return preflight.describe(info)


def cmd_check(a):
    try:
        draft, cur, fresh = draftio.read(allow_stale=True)
    except (draftio.NoDraft, draftio.StaleDraft) as e:
        # 别处失败都是「JSON + 退出码」，check 却打印中文感叹号、stale 还给 2。
        # 面板/脚本按「最后一段 JSON」解析时什么都拿不到，等于这条命令没法被
        # 程序使用。统一成 ok/gate/error 结构，退出码 3（与 _draft_out 一致）。
        print(json.dumps({
            "ok": False, "action": "check",
            "gate": ("stale_draft" if isinstance(e, draftio.StaleDraft)
                     else ("no_plaintext" if isinstance(e, draftio.NoPlaintext)
                           else "no_draft")),
            "error": str(e),
        }, ensure_ascii=False, indent=1, default=str))
        return 3
    feats = draftio.feature_counts(draft)
    rep = {
        "draft": cur.get("name"), "draft_id": draft.get("id"),
        "duration_us": draft.get("duration"),
        "layers": draftio.layers(draft),
        "features": feats,
        "freshness": fresh,
        "lut_dir": str(paths.draft_lut_dir(draft.get("id"))),
    }
    run_dir = paths.draft_run_dir(draft.get("id"))
    rep["plan_exists"] = os.path.exists(os.path.join(run_dir, "plan.json"))
    draftio.dump_json(rep, os.path.join(run_dir, "check.json"))
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    if fresh["stale"]:
        # 旧文案写着「Ctrl+S 或切草稿再切回来」——2026-11-14 实测两条都不行：
        # 保存只写盘、切走不重读，内存里仍是打开那一刻的那份。
        print("\n!! %s" % draftio.stale_message(fresh))
        return 3
    return 0


def cmd_plan(a):
    """只算「这稿会怎么落轨」，不碰达芬奇。

    和 send 内部用的是同一个 build_plan()，所以这不是「另一套估算」，是
    dry-run。可以进 CI、可以 diff 两次改动的位置差异。
    """
    from . import send
    try:
        draft, cur, fresh = draftio.read(allow_stale=True)
    except (draftio.NoDraft, draftio.StaleDraft) as e:
        print(json.dumps({
            "ok": False, "action": "plan",
            "gate": ("stale_draft" if isinstance(e, draftio.StaleDraft)
                     else ("no_plaintext" if isinstance(e, draftio.NoPlaintext)
                           else "no_draft")),
            "error": str(e),
        }, ensure_ascii=False, indent=1, default=str))
        return 3
    plan, skipped, overlaps, trimmed, shifted = send.build_plan(draft, speed_mode=a.speed)
    kinds = {}
    for c in plan:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    rep = {
        "ok": True, "action": "plan",
        "draft": cur.get("name"), "draft_id": draft.get("id"),
        "speed_mode": a.speed, "freshness": fresh,
        "segments": len(plan), "kinds": kinds,
        "disabled": sum(1 for c in plan if not c.get("enabled", True)),
        "baked": sum(1 for c in plan if c.get("bake")),
        "timeline_end": max((c["rec"] + c["place"] for c in plan), default=0),
        "skipped": skipped, "overlaps": overlaps,
        "trimmed": trimmed, "shifted": shifted,
        "plan": plan,
    }
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_newproj(a):
    """在达芬奇里新建空白工程并进入。

    存在的原因：送入要求工程是空白，而「停在项目管理器」时达芬奇会返回一个
    幻影工程（Untitled Project / 0 条时间线），闸门看着像空白工程，实际
    一建时间线就返回 None。这个命令把「准备一个可写的空白工程」变成一步。
    """
    from . import preflight
    rep = {"ok": False, "action": "newproj"}
    try:
        r = preflight.resolve_app()
        rep["page_before"] = r.GetCurrentPage()
        rep["project_before"] = (r.GetProjectManager().GetCurrentProject().GetName()
                                 if r.GetCurrentPage() else None)
        proj, name = preflight.open_blank_project(r, a.name)
        rep.update({"ok": True,
                    "project": (proj.GetName() if proj else name),
                    "created": name,
                    "timelines": (proj.GetTimelineCount() if proj else 0),
                    "page": r.GetCurrentPage()})
    except Exception as e:
        if isinstance(e, preflight.Gate):
            return _gate_out(rep, e)
        raise
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_status(a):
    """秒回的状态查询 —— 只认「剪映开没开、草稿是哪份、锁定状态」。

    面板每次刷新都调它。原来看 check：那个要扫剪映进程内存（最长 70 秒），
    面板是单线程轮询的，等于整个界面卡死。这里只做三件秒级的事：
    进程枚举、草稿目录 .locked、上一次送入的指针。
    """
    from . import preflight, snapshot
    rep = {"ok": True, "action": "status"}
    cur = None
    try:
        cur = draftio.identify()
        # identify() 能成，说明剪映既在跑、也认出了草稿 —— 不用再跑一次
        # tasklist（每次 0.3 秒，面板刷新感知得到）。数不出进程数就标 inferred。
        rep["jianying"] = {"processes": 1, "inferred": True}
        rep["draft"] = cur.get("name")
        rep["draft_id"] = cur.get("timeline_id")
        folder = str(cur.get("folder") or "")
        opened, age, _p = preflight.lock_state(folder) if folder else (False, 0, "")
        rep["opened"] = bool(opened)
        rep["lock_age_sec"] = round(age)
        # 打开之后又被编辑过？只看 draft_settings 的 last_edit 与 .locked
        # 的时间先后，不扫内存（几十毫秒）。面板拿它在点「送入」之前就提示
        # 「要重开」；送入时 preflight 还会再硬拦一次，命令行同样生效。
        if folder:
            edit_at = draftio.draft_edit_time(folder)
            op_at = draftio.open_mtime(folder)
            rep["draft_edited"] = draftio.edited_since_open(folder)
            rep["draft_edit_at"] = edit_at
            rep["draft_edit_text"] = draftio.stamp(edit_at)
            rep["draft_opened_text"] = draftio.stamp(op_at)
    except Exception as e:
        rep["draft"] = None
        rep["opened"] = False
        rep["draft_error"] = getattr(e, "args", [str(e)])[0]
        try:
            rep["jianying"] = preflight.jianying_alive()
        except Exception as e2:
            rep["jianying"] = {"processes": 0,
                               "why": getattr(e2, "msg", str(e2))}
    ptr = snapshot.current()
    if ptr:
        rep["last_send"] = {
            "draft": ptr.get("draft_name"),
            "at": ptr.get("snapshot_at"),
            "timeline": ptr.get("timeline"),
            "segments": ptr.get("segments"),
            "committed": bool(ptr.get("committed") and not ptr.get("_incomplete")),
        }
    # 面板在同一行显示缓存。报的是「按默认策略真能删掉的量」，不是目录总占用：
    # 总占用里大头是写回要用的底稿快照和当前草稿的 cube，删不得，显示成
    # 「缓存 253 MB」却清不动，就是在骗人。extra 是另外一笔（可再生中间产物）。
    try:
        from . import gc
        rc = gc.reclaimable()
        rep["cache_bytes"] = rc["total_bytes"]
        rep["cache_free_bytes"] = rc["free_bytes"]
        rep["cache_extra_bytes"] = rc["extra_bytes"]
        rep["cache_free_count"] = rc["free_count"] + rc["extra_count"]
    except Exception:
        rep["cache_bytes"] = None
        rep["cache_free_bytes"] = None
        rep["cache_extra_bytes"] = None
        rep["cache_free_count"] = None
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    return 0


def cmd_reset(a):
    """清掉「没走完的那次送入」，让写回回到上一次成功的版本。"""
    from . import snapshot
    rep = snapshot.clear_pending()
    rep["action"] = "reset"
    ptr = snapshot.current()
    rep["will_use"] = ({"draft": ptr.get("draft_name"),
                        "at": ptr.get("snapshot_at"),
                        "timeline": ptr.get("timeline")} if ptr else None)
    _print(rep)
    return 0 if rep.get("ok") else 1


def cmd_audit(a):
    from . import audit
    rep = audit.run(speed_mode=getattr(a, "speed", "bake"))
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    return 0 if not rep.get("blocking") else 5


def cmd_send(a):
    from . import send
    rep = {"ok": False, "action": "send"}
    if not getattr(a, "no_gate", False):
        try:
            # --allow-stale 是给「明知内存是旧版也要送」的人留的逃生口，
            # 这里必须一起放行，否则那个开关等于失效。
            info = _preflight("both", fresh=not getattr(a, "allow_stale", False))
            rep["gate"] = {"ok": True, "status": _describe(info)}
        except Exception as e:
            from . import preflight
            if isinstance(e, preflight.Gate):
                return _gate_out(rep, e)
            raise
    rep = send.run(allow_stale=a.allow_stale,
                   speed_mode=getattr(a, "speed", "slot"))
    _tidy(rep)
    _print(rep)
    return 0 if rep.get("ok") else 1


def cmd_pull(a):
    from . import pull
    rep = {"ok": False, "action": "pull"}
    if not getattr(a, "no_gate", False):
        try:
            info = _preflight("davinci")
            rep["gate"] = {"ok": True, "status": preflight_status(info)}
        except Exception as e:
            from . import preflight
            if isinstance(e, preflight.Gate):
                return _gate_out(rep, e)
            raise
    rep = pull.run()
    _print(rep)
    return 0 if rep.get("ok") else 1


def preflight_status(info):
    dv = info.get("davinci") or {}
    return ("达芬奇「%s」· 时间线 %d 条"
            % (dv.get("project"), dv.get("timeline_count") or 0))


def cmd_write(a):
    from . import write
    rep = {"ok": False, "action": "write"}
    if not getattr(a, "no_gate", False):
        # 写回按「送入时的快照」重建草稿，不读剪映内存 —— 所以只要求剪映进程在、
        # 达芬奇脚本接口连得上；不再要求目标草稿还开着，也不要求工程空白。
        try:
            from . import preflight
            preflight.jianying_alive()
            _, _, dv = preflight.daVinci(require_blank=False, auto_project=False)
            rep["gate"] = {"ok": True,
                           "status": "达芬奇「%s」· 时间线 %d 条"
                                     % (dv.get("project"),
                                        dv.get("timeline_count") or 0)}
        except Exception as e:
            from . import preflight
            if isinstance(e, preflight.Gate):
                return _gate_out(rep, e)
            raise
    rep = write.run(new_name=getattr(a, "name", None),
                    auto_pull=not getattr(a, "no_pull", False))
    _tidy(rep)
    _print(rep)
    return 0 if rep.get("ok") else 1


def _tidy(rep):
    """命令成功后顺手回收一次缓存，结果挂进报告。

    为什么放在命令行层、不放在 `send.run` / `write.run` 里：那两个是纯逻辑，
    测试会直接调。库里调 gc.auto() 用的是默认的 %LOCALAPPDATA%\\ColorPass，
    测试只隔离了 run 目录，结果一轮 unittest 把真实缓存删了（实测踩过）。
    副作用只在「用户真的跑了一次命令」时发生，这才对。
    """
    if not rep.get("ok"):
        return rep
    try:
        from . import gc
        g = gc.auto()
        if g.get("count"):
            rep["gc"] = {"count": g["count"], "freed_bytes": g.get("freed_bytes")}
    except Exception:
        pass
    return rep


def cmd_clean(a):
    """清掉变速预渲染缓存（%LOCALAPPDATA%\\ColorPass\\retime）。

    `--draft-id` 只清那份草稿的缓存；不给就整目录清。
    """
    from . import retime
    # 旧写法把 --all 的 default 设成 True，于是 --draft-id 永远用不上（死参数）。
    # 现在的语义：给了 --draft-id 就只清那一份，否则全清；--all 是显式写法。
    # 两个都给过是矛盾指令，报错比「静默全清」安全 —— 后者会顺手清掉别人草稿的
    # 预渲染，下次送入还得重编。
    if a.all and a.draft_id:
        print(json.dumps({"ok": False, "action": "clean",
                          "error": "--all 与 --draft-id 不能同时给"},
                         ensure_ascii=False, indent=1))
        return 1
    target = None if (a.all or not a.draft_id) else a.draft_id
    rep = retime.clean(target)
    rep["action"] = "clean"
    rep["scope"] = "draft %s" % target if target else "all"
    _print(rep)
    return 0 if rep.get("ok") else 1


def cmd_gc(a):
    """回收缓存。默认只算账，--apply 才删。

    为什么不反过来（默认删、--dry-run 看）：这个命令会被面板和「送完顺手跑」
    两种方式调用，默认动手太容易误伤。要看清单就裸跑，确认了再加 --apply。
    """
    from . import gc
    rep = gc.run(apply=bool(a.apply), keep_last=a.keep_last,
                 keep_days=a.keep_days, max_mb=a.max_mb,
                 intermediate=bool(getattr(a, "intermediate", False)))
    rep["action"] = "gc"
    if getattr(a, "intermediate", False):
        rep["scope"] = ("%s，只收可再生中间产物（变速预渲染 / cube 工作副本），"
                        "保留报告与底稿快照" % ("已删除" if a.apply else "仅报告"))
    else:
        rep["scope"] = "%s，保留最近 %d 份 / %d 天 / 上限 %d MB" % (
            "已删除" if a.apply else "仅报告", a.keep_last, a.keep_days, a.max_mb)
    # 控制台看清单比看 JSON 直观，JSON 里只留摘要（面板按 JSON 解析）。
    if not rep.get("items"):
        print(rep.get("note") or "没有可回收的")
    for it in rep.get("items") or []:
        print("  %-7s %-38s %8s  %s" % (it["pool"], it["name"],
                                        gc.human(it["bytes"]), it["reason"]))
    print("合计可回收 %s（回收前 %s）" % (gc.human(rep["freed_bytes"] if a.apply
                                                   else sum(i["bytes"] for i in rep.get("items") or [])),
                                          gc.human(rep["before_bytes"])))
    brief = {k: v for k, v in rep.items() if k != "items"}
    brief["items"] = len(rep.get("items") or [])
    _print(brief)
    return 0 if rep.get("ok") else 1


def cmd_lock(a):
    from . import lock
    rep = lock.run(a.state)
    _print(rep)
    return 0 if rep.get("ok") else 1


def cmd_roundtrip(a):
    from . import preflight, pull, send, write
    if not getattr(a, "no_gate", False):
        try:
            info = _preflight("both")
            print("闸门通过：" + _describe(info))
        except Exception as e:
            if isinstance(e, preflight.Gate):
                return _gate_out({}, e)
            raise
    out = {}
    out["send"] = send.run(allow_stale=a.allow_stale,
                           speed_mode=getattr(a, "speed", "slot"))
    if not out["send"].get("ok"):
        _print(out["send"])
        return 1
    out["pull"] = pull.run()
    if not out["pull"].get("ok"):
        _print(out["pull"])
        return 1
    out["write"] = write.run()
    _tidy(out["write"])
    _print(out["write"])
    return 0 if out["write"].get("ok") else 1


def _print(rep):
    keys = ("ok", "error", "gate", "state", "locked", "timeline", "placed",
            "exact", "timeline_end", "expected_end", "audio_items",
            "video_linked", "count", "identity_dev_max", "identity_ok",
            "alignment_ok", "segments", "lut_ok", "timeline_unchanged",
            "keyframes_unchanged", "transitions_unchanged", "new_draft",
            "paths_swapped", "overlaps", "trimmed", "shifted",
            "cube_problems", "problems", "mismatch", "failed", "skipped",
            "requested_name", "subdrafts", "compound_src", "compound_ok",
            "identity_graded", "baked", "draft_name",
            "speed_mode", "speed_segments", "speed_note", "kinds",
            "features", "issues", "orphan", "luts", "bake_failed",
            "snapshot_at", "snapshot_sha", "drift_note", "sent_timeline",
            "coverage", "coverage_segments", "coverage_note", "multicam_groups",
            "multicam", "coverage_capable", "exports",
            "cleared", "will_use", "scope", "files", "freed_bytes", "tracks",
            "before_bytes", "after_bytes", "base", "free_bytes", "kept_bytes",
            "pending_at", "live_note", "live_error", "freshness",
            # 通用键（note / summary / run_dir 等）。这里只挑 rep 里真有的键
            # 打印，每条命令各拿各的。
            "summary", "note", "run_dir", "clip", "project", "applied",
            "intermediate", "intermediate_bytes", "intermediate_count",
            "seconds", "settings", "report", "hint", "lut_dir", "page")
    print(json.dumps({k: rep.get(k) for k in keys if k in rep},
                     ensure_ascii=False, indent=1, default=str))
    if rep.get("error"):
        print("!!", rep["error"])


def cmd_doctor(a):
    """不给用户看热闹，只收兼容性数据：哪个版本缺哪个 API，一眼可读。"""
    from . import doctor
    return doctor.main(["--compact"] if getattr(a, "compact", False) else [])


def cmd_compat(a):
    """兼容性自检。人读一行字，机器读 JSON。

    与 doctor 的分工：doctor 是给排查用的全量快照（路径、进程、依赖），
    compat 只回答一个问题 —— **这个版本的达芬奇，哪些能力有、哪些没有、
    没有会怎样**。测试同学只要贴这一段就够定位版本问题。
    """
    from . import compat
    if getattr(a, "matrix", False):
        print(compat.matrix_text())
        return 0
    rep = compat.report()
    if getattr(a, "json", False):
        print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    else:
        print(compat.text_report())
    # 缺致命能力才算失败；「没开达芬奇」不算 —— 那是环境状态，不是不兼容。
    return 0 if rep.get("ok") or rep.get("state") == "unknown" else 5


def cmd_multicam(a):
    """多机位 / 合并片段自检：写回前先看它认不认，不必真写一遍。"""
    from . import multicam
    rep = multicam.run(export=getattr(a, "export", False))
    if getattr(a, "json", False):
        print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    else:
        print(multicam.text(rep))
    if rep.get("error"):
        return 5
    return 0


def main(argv=None):
    # 面板是 Popen 到文件的，Windows 下默认按 cp936 写，面板再按 utf-8 读就是
    # 一片「10��30��」的乱码。这里把三条标准流钉死成 utf-8，谁的父进程都一样。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    # 改名前的缓存目录迁到新位置（只在新目录还不存在时搬一次）
    paths.migrate_legacy_base()
    p = argparse.ArgumentParser(prog="colorpass")
    p.add_argument("--allow-stale", action="store_true",
                   help="允许用内存旧快照（危险，仅排错用）")
    p.add_argument("--no-gate", action="store_true",
                   help="跳过使用前闸门（仅排错用，正常不要加）")
    sub = p.add_subparsers(dest="cmd")

    def _alias_flags(sp):
        """把两个全局开关也挂到子命令上。

        这样 `colorpass send --no-gate` 与 `colorpass --no-gate send` 都能用。
        用 SUPPRESS 当默认值：子命令没写这个开关时不去覆盖母解析器已经置上的值，
        否则写在前面那个会被后面的默认 False 抹掉。
        """
        sp.add_argument("--allow-stale", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument("--no-gate", action="store_true",
                        default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    sp = sub.add_parser("gate"); sp.set_defaults(fn=cmd_gate)
    sp = sub.add_parser("status"); sp.set_defaults(fn=cmd_status)
    sp = sub.add_parser("check"); sp.set_defaults(fn=cmd_check)
    sp = sub.add_parser("plan", help="只算落轨计划，不碰达芬奇（dry-run）")
    sp.add_argument("--speed", default="bake", choices=list(paths.SPEED_MODES))
    sp.set_defaults(fn=cmd_plan)
    sp = sub.add_parser("newproj",
                        help="在达芬奇里新建一个空白工程并进入（送入前的准备）")
    sp.add_argument("name", nargs="?", default="ColorPass")
    sp.set_defaults(fn=cmd_newproj)
    sp = sub.add_parser("audit")
    # 体检里「慢放段越界」算不算致命，取决于这次用什么落地模式：
    # bake（默认）会预渲染吃掉这个问题，slot/source 不会。
    sp.add_argument("--speed", default="bake", choices=list(paths.SPEED_MODES))
    sp.set_defaults(fn=cmd_audit)
    sp = sub.add_parser("send")
    sp.add_argument("--speed", default="bake", choices=list(paths.SPEED_MODES))
    _alias_flags(sp)
    sp.set_defaults(fn=cmd_send)
    sp = sub.add_parser("pull"); _alias_flags(sp); sp.set_defaults(fn=cmd_pull)
    sp = sub.add_parser("write")
    sp.add_argument("--name", default=None,
                    help="新草稿名（默认 <草稿名>_调色，同名自动加 _2）")
    sp.add_argument("--no-pull", action="store_true",
                    help="跳过自动取回，直接用现有 pull.json（排错用，容易挂错段）")
    _alias_flags(sp)
    sp.set_defaults(fn=cmd_write)
    sp = sub.add_parser("roundtrip")
    sp.add_argument("--speed", default="bake", choices=list(paths.SPEED_MODES))
    _alias_flags(sp)
    sp.set_defaults(fn=cmd_roundtrip)
    sp = sub.add_parser("lock")
    sp.add_argument("state", nargs="?", default="on", choices=["on", "off"])
    sp.set_defaults(fn=cmd_lock)
    sp = sub.add_parser("reset", help="丢掉「没走完的那次送入」记录")
    sp.set_defaults(fn=cmd_reset)
    sp = sub.add_parser("clean", help="清掉变速预渲染缓存")
    sp.add_argument("--all", action="store_true",
                    help="整目录清（默认；给了 --draft-id 就只清那份）")
    sp.add_argument("--draft-id", default=None)
    sp.set_defaults(fn=cmd_clean)
    sp = sub.add_parser("gc", help="回收过期缓存（默认只报告）")
    sp.add_argument("--apply", action="store_true", help="真的删；不加只列出")
    sp.add_argument("--keep-last", type=_positive_int, default=5,
                    help="每个池保留最近几份草稿（默认 5，最小 1）")
    sp.add_argument("--keep-days", type=_positive_int, default=30,
                    help="超过这么多天没动就回收（默认 30，最小 1）")
    sp.add_argument("--max-mb", type=_positive_int, default=600,
                    help="缓存总量上限，超出从最老的收（默认 600 MB，最小 1）")
    sp.add_argument("--intermediate", action="store_true",
                    help="只收可再生的中间产物（变速预渲染 / cube 工作副本），"
                         "报告与底稿快照一律保留")
    sp.set_defaults(fn=cmd_gc)

    sp = sub.add_parser("doctor",
                        help="环境与兼容性报告：平台、达芬奇版本、API 缺项、路径（只读）")
    sp.add_argument("--compact", action="store_true", help="单行 JSON，便于贴进 issue")
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("compat",
                        help="兼容性自检：这版达芬奇缺哪几个 API、各自影响什么（只读）")
    sp.add_argument("--json", action="store_true", help="输出 JSON（默认是人读的多行文本）")
    sp.add_argument("--matrix", action="store_true",
                    help="打印 18/19/20/21 四版能力对照表（不连达芬奇）")
    sp.set_defaults(fn=cmd_compat)

    sp = sub.add_parser("multicam",
                        help="多机位 / 合并片段自检：识别、与送入计划匹配、能否导出 LUT")
    sp.add_argument("--export", action="store_true",
                    help="真的对多机位片段导一次 LUT（只写数据目录，不碰剪映/调色）")
    sp.add_argument("--json", action="store_true", help="输出 JSON")
    sp.set_defaults(fn=cmd_multicam)

    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_help()
        return 0
    try:
        return a.fn(a)
    except (draftio.StaleDraft, draftio.NoDraft) as e:
        return _draft_out(getattr(a, "cmd", "") or "", e)


if __name__ == "__main__":
    sys.exit(main())
