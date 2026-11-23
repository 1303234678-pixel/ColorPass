"""读取剪映当前草稿 —— 带「新鲜度闸门」。

判据（2026-11-13 深夜实测后改定，前一版判断是错的，见下）：

  剪映 11.7 的 draft_content.json 是 base64(AES) 的：
  载荷熵 7.996 bit/B、无重复块、无周期 —— 拿不到密钥就**读不出磁盘正文**。
  所以唯一可用的明文来源是进程内存；而内存里的对象**没有可靠的时间戳**：
  它的 `update_time` 是草稿文档内嵌字段，加载后不会随编辑更新
  （实测：内存对象 update_time 恒为 21:36:57，磁盘却在 23:23 还在保存）。

  真正能对得上的信号是**体积**：剪映序列化用紧凑分隔符（无空格），
  密文载荷 = 明文 + PKCS7 填充(1..16)，可能再加 16 字节 IV。
  实测同一版本：明文 51716 B ↔ 密文载荷 51728 B（Δ=12）。
  版本一旦不同（用户改了内容），Δ 就不再落在这个窄区间里。

所以：Δ 落在「固定余量 + 千分之五」之内判为「内存与磁盘同一版本」（TOL_FIXED /
TOL_RATIO，两次实测见下），否则判 stale 并拒绝动作 —— 宁可停下，也不拿看不出来源
的旧数据覆盖新工程。
"""
import glob
import json
import os
import sys
import time
from pathlib import Path

from . import memscan, paths, plain, platforms, probe

# 体积对账的容差。剪映落盘时会做两件事，让「内存明文」和「密文载荷」不能
# 简单相等：
#   · 加密：明文 + PKCS7 填充(1..16) [+ 可能 16 字节 IV]
#   · 序列化差异：内存对象带若干运行期字段（打开草稿时补的路径/设备信息、
#     浮点格式化差异），存盘时会被去掉或改写
# 所以容差 = 固定余量 + 千分之五。两次实测：
#   51716 B → 载荷 51728（Δ=+12）
#   57828 B → 载荷 57732（Δ=-96，带复合片段的草稿）
# 只用一个固定窗口会在第二类草稿上误拦（用户什么都没改也报 stale）。
TOL_FIXED = 320
TOL_RATIO = 0.005


def stamp(ts):
    """epoch 秒 → '11-19 23:47:41'；0/None → ''。面板和报告都用这一种写法。"""
    return time.strftime("%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""


class StaleDraft(Exception):
    """内存快照落后于磁盘保存版本。"""


class NoDraft(Exception):
    """没有识别到正在编辑的草稿。"""


class NoPlaintext(NoDraft):
    """草稿认出来了，但拿不到它的明文（内存回收了，也没有备份/缓存）。"""


def identify():
    """当前打开的是哪份草稿（名称/ID/目录/进程）。"""
    if platforms.is_mac():
        # macOS 没有可读的窗口标题，也拿不到剪映的内部状态；用草稿根里
        # 「最近活动的那份」代替。`.locked` 存在时优先，避免刚编辑过别的
        # 草稿就被抢走。
        from . import macdraft
        if not macdraft.installed():
            raise NoDraft("没有找到剪映：%s" % macdraft.BIN)
        if not macdraft.running():
            raise NoDraft("剪映未运行。请先启动剪映并打开目标草稿。")
        try:
            row = macdraft.current()
        except macdraft.NotInstalled as exc:
            raise NoDraft(str(exc))
        return macdraft.identity(row)
    snap = probe.snapshot()
    cur = snap.get("draft") or {}
    if not snap.get("running"):
        raise NoDraft("剪映未运行。请先启动剪映并打开目标草稿。")
    if not cur:
        raise NoDraft(snap.get("draft_error") or
                      "无法唯一识别剪映当前打开的草稿。")
    return cur


def _scan_open_draft(deadline=None, expected=None, target_bytes=None):
    """直接从剪映进程内存里取明文。取不到返回 None。

    这是 plain.py 出现之前唯一的一条路，现在退到最后一道 —— 但它仍然是
    「第一次读这份草稿、又没有缓存」时的唯一来源，所以不能删。

    前后各拍一次进程快照，是为了挡住「扫描期间用户切了草稿」：两次身份不一致
    就宁可返回 None，也不能把 A 工程的明文送进 B 工程的达芬奇时间线。

    设 COLORPASS_NO_MEMSCAN=1 可以彻底关掉这条路（只用缓存与载入备份）。
    给介意「读别人进程内存」的人一个开关，代价是从没读过的新草稿会读不到。
    """
    if os.environ.get("COLORPASS_NO_MEMSCAN"):
        return None
    try:
        snap = probe.snapshot()
    except Exception:
        return None
    cur = snap.get("draft") or {}
    hint = cur.get("timeline_id")
    if (not snap.get("running") or not cur or not hint or not cur.get("pid")
            or (expected is not None and not probe.same_draft(expected, cur))):
        return None
    try:
        pid, draft = memscan.extract_open_draft(
            pid=cur["pid"], uuid_hint=hint,
            deadline=(70 if deadline is None else int(deadline)),
            target_bytes=target_bytes)
    except Exception:
        return None
    scan = dict(getattr(memscan, "LAST_SCAN", {}) or {})
    if pid != cur["pid"] or not draft:
        return None
    if str(draft.get("id", "")).lower() != str(hint).lower():
        return None
    try:
        after = probe.snapshot()
    except Exception:
        return None
    if not after.get("running") or not probe.same_draft(cur, after.get("draft")):
        return None
    return {"draft": draft, "identity": cur, "source": "内存直读",
            "mem_candidates": scan}


def disk_saved_at(folder):
    """草稿在磁盘上最后一次保存的时间（epoch 秒）。"""
    cands = [os.path.join(folder, "draft_content.json")]
    cands += glob.glob(os.path.join(folder, "Timelines", "*",
                                    "draft_content.json"))
    stamps = [os.path.getmtime(p) for p in cands if os.path.exists(p)]
    return max(stamps) if stamps else 0.0


def plaintext_size(draft):
    """按剪映的紧凑序列化量出明文体积（用来跟密文载荷对账）。"""
    return len(json.dumps(draft, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8"))


def disk_payload_size(folder):
    """磁盘草稿的密文载荷字节数（base64 解码后），取最新的那份。"""
    import base64
    cands = [os.path.join(folder, "draft_content.json")]
    cands += glob.glob(os.path.join(folder, "Timelines", "*",
                                    "draft_content.json"))
    best = (0.0, 0)
    for p in cands:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "rb") as f:
                raw = f.read()
            payload = len(base64.b64decode(raw + b"=" * (-len(raw) % 4)))
        except Exception:
            continue
        best = max(best, (os.path.getmtime(p), payload))
    return best[1]


def open_mtime(folder):
    """剪映打开这份草稿的时刻（`.locked` 的 mtime）。0 = 现在没开着。

    剪映打开草稿时建 `.locked`、关闭时删掉，它是唯一能拿到「打开那一刻」
    的时间戳。用来和 draft_settings 里的 last_edit 比先后。
    """
    try:
        return os.path.getmtime(os.path.join(folder, ".locked"))
    except OSError:
        return 0.0


# 打开草稿的一两秒内剪映会自己写一份规范化副本，`draft_last_edit_time`
# 可能顺带落在同一秒，给 1 秒余量，避免「刚打开就报改过」。
EDIT_GRACE_SEC = 1.0


def draft_edit_time(folder):
    """这份草稿「最后一次被人编辑」的时刻（epoch 秒），读不出来返回 0。

    来源是草稿目录里的 `draft_settings`，一个明文 INI：

        [General]
        draft_create_time=1795102693
        draft_last_edit_time=1795102693

    别用 draft_content.json 的 mtime 代替：剪映**打开**草稿时也会写一次盘
    （实测 2026-11-19：.locked 23:38:25、draft_content.json 23:38:26，
    相差 1 秒），拿 mtime 判「打开后改过」会把每次打开都算成改动。
    draft_last_edit_time 只在真的编辑后前进 —— 本机实测「11月19日」这份草稿
    创建/打开于 03:44:28、last_edit 是 03:47:41，正是「打开后又改了 3 分钟」
    的真实样本；而刚写回的「123_调色」last_edit 23:38:13 早于打开时刻
    23:38:25，不会被误判。
    """
    p = os.path.join(folder, "draft_settings")
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if "=" not in ln:
                    continue
                k, v = ln.strip().split("=", 1)
                if k.strip() == "draft_last_edit_time":
                    return float(v.strip() or 0)
    except (OSError, ValueError):
        return 0.0
    return 0.0


def edited_since_open(folder):
    """草稿是不是在「打开之后」又被编辑过。

    True 意味着：剪映内存里那份明文是打开那一刻的，磁盘上已经是新的了。
    送进达芬奇会把你后来的改动悄悄丢掉 —— 所以要停下让人重开草稿。
    """
    opened = open_mtime(folder)
    edit = draft_edit_time(folder)
    return bool(opened and edit and edit > opened + EDIT_GRACE_SEC)


def freshness(cur, draft, scan=None, source="内存直读"):
    """内存快照 vs 磁盘保存版本：按体积对账，而不是比时间戳。

    source：这份明文从哪来的。`内存直读` 才做体积对账 —— 内存副本是剪映
    自己的序列化，长度和密文载荷能对上；另外两条来源（`缓存` / `载入备份`）
    是**另一套校验**：缓存要比磁盘指纹完全一致，备份要比「最后编辑时间不晚于
    备份时间」。拿体积去卡它们只会误拦：剪映的载入备份是加载器序列化，
    同一份内容能比落盘版长 14%（实测 73683 B vs 63143 B）。

    scan：jy_memscan 的候选清单。剪映内存里同一份草稿常常躺着 2~3 个
    id 相同的副本（打开时那版 + 之后每次保存留下的），体积对账只能说明
    「选中的这份跟磁盘差多少」，不能说明「是不是只有这一份」。给了 scan 时
    额外报出副本数量与结构异同，供 stale_message() 给出可执行的处置建议。

    注意：副本结构一致**不能**当成放行理由。2026-11-14 实测：用户加上两个
    蒙版后，内存里三份副本段结构完全一致，但其中两份是加蒙版之前的老版本
    （差 1742 B）。当时放行就会把蒙版悄悄丢掉 —— 差多少字节才是硬判据。
    """
    mem_us = float(draft.get("update_time") or 0)
    mem_sec = mem_us / 1e6 if mem_us else 0.0
    folder = str(cur.get("folder") or "")
    disk = disk_saved_at(folder) if folder else 0.0
    payload = disk_payload_size(folder) if folder else 0
    mem = plaintext_size(draft)
    delta_size = (payload - mem) if payload else 0
    tol = TOL_FIXED + TOL_RATIO * max(mem, payload)
    trusted = source != "内存直读"
    same = True if trusted else (bool(payload) and abs(delta_size) <= tol)
    # 第二条判据（与体积无关）：草稿在「打开之后」被编辑过。
    # 体积容差有 320 B + 0.5%，改个文字/挪一下蒙版可能只有几十字节，
    # 落在容差里就被放行 —— 但那同样是拿旧内存覆盖新工程。last_edit 与
    # .locked 的先后能抓住这类小改动，而且判它不需要扫内存。
    edit_at = draft_edit_time(folder) if folder else 0.0
    opened_at = open_mtime(folder) if folder else 0.0
    edited = bool(edit_at) and bool(opened_at) and \
        edit_at > opened_at + EDIT_GRACE_SEC
    scan = scan or {}
    out = {
        "memory_update_time": mem_sec,
        "memory_update_text": stamp(mem_sec),
        "disk_saved_at": disk,
        "disk_saved_text": stamp(disk),
        "delta_sec": round((disk - mem_sec) if (disk and mem_sec) else 0.0, 1),
        "plaintext_bytes": mem,
        "disk_payload_bytes": payload,
        "delta_bytes": delta_size,
        "draft_edit_at": edit_at,
        "draft_edit_text": stamp(edit_at),
        "opened_at": opened_at,
        "opened_text": stamp(opened_at),
        "edited_since_open": edited,
        "plain_source": source,
        "source_trusted": trusted,
        "same_version": same,
        "stale": (bool(payload) and not same) or edited,
        "stale_reason": ("edited_since_open" if edited
                         else ("" if same or not payload else "size")),
        "folder": folder,
    }
    if scan:
        n = int(scan.get("count") or 0)
        out["memory_copies"] = n
        out["copy_picked_bytes"] = int(scan.get("picked_size") or mem)
        out["copy_delta_bytes"] = int(scan.get("picked_delta") or 0)
        out["copies_same_structure"] = scan.get("structure_consistent")
        out["scan_sec"] = scan.get("elapsed")
        # 「多副本 + 结构一致」只作为说明材料（stale_message 里用），
        # 不能拿来放行 —— 老副本也可能结构完全一致，缺的正是蒙版。
        out["copy_ok"] = bool(n >= 2 and scan.get("structure_consistent"))
    return out


def read(allow_stale=False, strict_identity=True, deadline=None):
    """读出当前草稿明文。

    返回 (draft, cur, fresh)。stale 且 allow_stale=False 时抛 StaleDraft。

    deadline：内存扫描的时间上限（秒）。不传就是老行为（sync() 内部默认 70 秒）。
    写回时的「漂移报告」只是附带信息，给它 6 秒足够；扫不出来就跳过，
    不能因为它把一次 15 秒的写回拖成 90 秒。
    """
    if platforms.is_mac():
        return _read_mac(allow_stale=allow_stale, strict_identity=strict_identity,
                         deadline=deadline)
    cur = identify()
    folder = str(cur.get("folder") or "")
    payload = disk_payload_size(folder)
    # 先试两条「秒级」的路：本地缓存（磁盘指纹完全一致）和剪映的载入备份。
    # 它们不是省时间的小优化 —— 剪映把内存里的明文放久了会回收，只有这两条
    # 能在这个时候还读得到（见 plain 模块开头的实测）。
    s = plain.best(cur, payload) or {}
    if not s:
        if deadline:
            # sync 的旧签名：deadline 是毫秒级预算，调用方给的是「最多等多久」
            s = _scan_open_draft(deadline=int(deadline), expected=cur,
                                 target_bytes=payload) or {}
        else:
            s = _scan_open_draft(expected=cur, target_bytes=payload) or {}
    draft = s.get("draft")
    if not isinstance(draft, dict):
        raise NoPlaintext(no_plaintext_message(cur, deadline))
    fresh = freshness(cur, draft, s.get("mem_candidates"),
                      source=s.get("source") or "内存直读")
    if strict_identity:
        want = str(cur.get("timeline_id") or "").lower()
        got = str(draft.get("id") or "").lower()
        if want and got and want != got:
            raise NoDraft("内存里的草稿 ID 与当前工程不一致（%s vs %s）"
                          % (got[:8], want[:8]))
    if fresh["stale"] and not allow_stale:
        raise StaleDraft(stale_message(fresh))
    # 存一份带磁盘指纹的副本。下次剪映把内存里的明文回收掉之后，只要磁盘没
    # 再存过盘，还能按同一份内容送入 —— 这是唯一能长期生效的兜底。
    if not fresh["stale"]:
        plain.save(draft, cur)
    return draft, cur, fresh


def _read_mac(allow_stale=False, strict_identity=True, deadline=None):
    """macOS：草稿正文来自「剪映进程内解密」。

    和 Windows 的本质区别（2026-11-23 实测）：这里读的是**磁盘上最新的密文
    解密结果**，不存在「内存快照落后于磁盘保存版本」这回事 —— 剪映自己写盘、
    自己解密，两者天然同版本。所以不做体积对账，也不会因为内存回收而读不到：
    只要读取器在剪映进程里，任何时刻都能读。

    代价是「读取器必须在位」。剪映若由用户自行启动，进程里没有它，这时报
    ReaderDown，并给出「重启剪映一次」的可执行动作，而不是含糊地说读不到。
    """
    from . import macdraft  # 仅 darwin 引入，避免影响 Windows 启动开销

    if not macdraft.installed():
        raise NoDraft("没有找到剪映：%s\n请先安装剪映专业版。" % macdraft.BIN)
    try:
        # deadline（秒）也用在 socket 上：剪映卡住时 decrypt 默认等 60 秒，
        # 而调用方给这一步的预算是 6 秒（写回的「漂移报告」只是附带信息）。
        draft, cur = macdraft.read_draft(
            timeout=float(deadline) if deadline else None)
    except macdraft.ReaderDown as exc:
        raise NoPlaintext(_mac_reader_message(str(exc)))
    except macdraft.NotInstalled as exc:
        raise NoDraft(str(exc))

    fresh = freshness(cur, draft, source="进程内解密")
    if strict_identity:
        want = str(cur.get("timeline_id") or "").lower()
        got = str(draft.get("id") or "").lower()
        if want and got and want != got:
            raise NoDraft("解密出的草稿 ID 与目标不一致（%s vs %s）"
                          % (got[:8], want[:8]))
    if fresh["stale"] and not allow_stale:
        raise StaleDraft(stale_message(fresh))
    if not fresh["stale"]:
        plain.save(draft, cur)
    return draft, cur, fresh


def _mac_reader_message(why):
    """剪映在跑但读不到 —— 每一句都得指向一个具体动作。"""
    return (
        "剪映正在运行，但进程内没有 ColorPass 的读取器（%s）。\n\n"
        "macOS 上剪映 11.7+ 的草稿密钥只在它自己的进程里有效，外部进程拿不到，"
        "所以必须让剪映带着读取器启动一次。\n\n"
        "处理：\n"
        "1  退出剪映（它会自动保存当前草稿）\n"
        "2  在 ColorPass 里点「读取草稿」，由它启动剪映\n"
        "3  之后再点一次读取即可，剪映照常使用，不影响任何功能\n\n"
        "也可以在终端执行： python3 -m colorpass.cli mac-launch" % why)


def no_plaintext_message(cur, deadline=None):
    """「草稿认出来了，但明文哪儿都拿不到」该怎么说。

    这跟「打开后又被编辑过」是两件事，不能混着讲。2026-11-20 实测到的机制：
    剪映只在打开草稿后的一段时间里把解密后的 JSON 留在进程内存里。一份
    23:38 打开、全程没动过的草稿，00:38 再扫只剩 167 KB 的半截残片
    （有 `"tracks":[`，没有 `"materials"`，括号都不闭合）—— 用户什么都没改，
    只是挂在那儿一个小时。旧文案说「内存与磁盘对不上」，会让人去怀疑自己
    动了草稿，方向完全错了。
    """
    name = cur.get("name") or "当前草稿"
    if edited_since_open(cur.get("folder") or ""):
        head = ("「%s」在打开之后又被编辑过，而剪映内存里已经没有它的完整明文了。"
                % name)
        why = ("现在既拿不到新版（内存被回收了），也不该拿旧版（会丢掉你后来的"
               "改动），所以停在这里。")
    elif deadline:
        head = ("在 %d 秒内没能从剪映进程内存里读到「%s」的完整明文。"
                % (int(deadline), name))
        why = ""
    else:
        head = "剪映内存里已经没有「%s」的完整明文了。" % name
        why = ("它只在刚打开草稿那段时间里把明文留在内存，放久了会自己回收 —— "
               "你什么都没改也一样读不到，这时内存里通常只剩半截残片。")
    steps = ("\n\n处理：\n"
             "1  回到剪映首页，把这份草稿关掉\n"
             "2  再双击重新打开它\n"
             "3  重开后尽快点一次送入（刚打开时明文一定还在）\n\n"
             "成功送一次之后，工具会按当时的磁盘状态存一份本地副本；"
             "这份草稿的磁盘内容没再变，以后放多久都能直接送。")
    return head + (("\n" + why) if why else "") + steps


def stale_message(fresh):
    """草稿不是最新版时到底该怎么办 —— 每一句都得是可执行的。

    2026-11-14 实测到的机制（别再写「按 Ctrl+S 再试」了）：
      剪映编辑时，明文 JSON 只是**按需序列化**出来的临时副本。保存那一刻会
      生成一份，随后被回收；长期留在内存里的是「打开草稿那一刻」解析出来的
      那几份。所以用户新加的两个蒙版只在磁盘（密文）里有，内存里最长的那份
      仍是没有蒙版的老版本 —— 差 1742 B，正好是蒙版体量。
      可靠恢复方式只有一个：关掉草稿再重新打开，剪映会把磁盘上的新版读进来。

    两种判据各写一段：一种是「打开后又被编辑过」（时间先后，抓小改动），
    一种是「体积对不上」（内容量差异，抓大改动/多副本）。
    """
    if fresh.get("edited_since_open"):
        return (
            "这份草稿在打开之后又被编辑过（最后编辑 %s，打开时刻 %s）。\n\n"
            "剪映只在「打开草稿」那一刻把最新内容读进内存，之后的编辑只往磁盘写。"
            "现在内存里留着的是打开那一刻的版本，照它送进去会把你后来的改动"
            "悄悄丢掉，所以这里停住了。\n\n"
            "处理：\n"
            "1  回到剪映首页（把这份草稿关掉）\n"
            "2  再双击重新打开它\n"
            "3  重新点一次\n\n"
            "只按 Ctrl+S 不够 —— 保存只是写盘，剪映不会因此重读内存。"
            % (fresh.get("draft_edit_text") or "?", fresh.get("opened_text") or "?"))
    head = ("内存里的草稿明文（%d B）与磁盘密文载荷（%d B）对不上，差 %+d 字节，"
            "不是同一版本。照它写回会在你不知情的情况下丢掉磁盘上那一版的改动。"
            % (fresh["plaintext_bytes"], fresh["disk_payload_bytes"],
               fresh["delta_bytes"]))
    n = fresh.get("memory_copies") or 0
    if n >= 2:
        tail = ("\n剪映进程内存里找到 %d 份同 ID 副本，最接近磁盘的那份也差 %+d 字节"
                "%s。这通常说明：你在剪映里做的改动已经写盘，但还没被重新读进内存。"
                "\n处理：把这份草稿关掉，再在剪映里双击打开（只按 Ctrl+S 不够 —— "
                "剪映保存后会把内存里那份明文副本回收掉），然后再点一次。"
                % (n, fresh.get("copy_delta_bytes") or 0,
                   "" if fresh.get("copies_same_structure")
                   else "，而且几份副本的片段结构互不相同（有增删改），更没法猜哪份是当前版"))
    else:
        tail = ("\n处理：把这份草稿关掉再重新打开，让剪映把磁盘上的最新版读进内存；"
                "只按 Ctrl+S 不够。")
    return head + tail


# ---------- 结构识别 ----------

def layers(draft):
    """按语义统计图层：视频/文字/贴纸/特效轨。"""
    out = {"video": 0, "text": 0, "sticker": 0, "effect": 0, "audio": 0}
    for tr in draft.get("tracks") or []:
        if not isinstance(tr, dict):
            continue
        t = tr.get("type") or ""
        n = len(tr.get("segments") or [])
        if t in out:
            out[t] += n
        elif t:
            out[t] = out.get(t, 0) + n
    return out


def mask_map(draft):
    """草稿里的蒙版材质（id → 材质）。剪映 11.x 在 common_mask，老版本在 masks。"""
    mats = draft.get("materials") or {}
    out = {}
    for key in ("common_mask", "masks"):
        for m in (mats.get(key) or []):
            if isinstance(m, dict) and m.get("id"):
                out[m["id"]] = m
    return out


def mask_keyframe_groups(seg, mask_ids):
    """段上属于蒙版的关键帧组（KFTypeCommonMask* 或挂在蒙版材质上的）。"""
    cids = {str(m.get("constant_material_id") or "")
            for m in (mask_ids or {}).values()}
    out = []
    for k in (seg.get("common_keyframes") or []):
        if not isinstance(k, dict):
            continue
        pt = str(k.get("property_type") or "")
        if pt.startswith("KFTypeCommonMask") or \
                str(k.get("material_id") or "") in cids:
            out.append(k)
    return out


def mask_segments(draft):
    """逐段列出「带蒙版」的视频段。

    蒙版在达芬奇侧是无解的（TimelineItem 的 26 个属性与候选方法里没有任何
    蒙版入口，实测已排除），所以这条链路对蒙版的策略是**完全不送**：
    不尝试、不报错、不阻断，只在报告里列清哪几段有、写回时按快照带回。
    这份清单就是那条策略的数据来源。
    """
    mm = mask_map(draft)
    if not mm and not any(s.get("enable_adjust_mask")
                          for t in draft.get("tracks") or []
                          for s in t.get("segments") or []):
        return []
    out, lane = [], 0
    for tr in (draft.get("tracks") or []):
        if not isinstance(tr, dict) or tr.get("type") != "video":
            continue
        lane += 1
        for sg in (tr.get("segments") or []):
            if not isinstance(sg, dict) or not sg.get("id"):
                continue
            refs = sg.get("extra_material_refs") or []
            hit = [r for r in refs if r in mm]
            if not hit and not sg.get("enable_adjust_mask"):
                continue
            tt = sg.get("target_timerange") or {}
            kfg = mask_keyframe_groups(sg, mm)
            out.append({
                "segment_id": sg["id"],
                "lane": lane,
                "start_sec": round(int(tt.get("start") or 0) / 1e6, 3),
                "dur_sec": round(int(tt.get("duration") or 0) / 1e6, 3),
                "mask_names": [str((mm.get(r) or {}).get("name") or "蒙版")
                               for r in hit],
                "mask_keyframes": sum(len(k.get("keyframe_list") or [])
                                      for k in kfg),
            })
    return out


def clip_transform(seg):
    """段上的画面变换（缩放/位移/旋转/翻转/裁切）。None = 全是默认值。

    剪映的 `clip` 决定构图：`scale` 放大、`transform` 位移、`rotation` 旋转。
    `AppendToTimeline` 不接受任何变换参数 —— 所以送进去先按原素材铺满，
    **事后再**用 `TimelineItem.SetProperty` 把缩放补上（`send.apply_transform`）。
    位移和旋转没有实测过的单位映射，只报不改，否则会把构图改坏。
    """
    c = seg.get("clip") or {}
    out = {}
    sc = c.get("scale") or {}
    try:
        sx, sy = float(sc.get("x", 1) or 1), float(sc.get("y", 1) or 1)
    except Exception:
        sx = sy = 1.0
    if abs(sx - 1) > 1e-3 or abs(sy - 1) > 1e-3:
        out["scale"] = [round(sx, 3), round(sy, 3)]
    tf = c.get("transform") or {}
    try:
        px, py = float(tf.get("x", 0) or 0), float(tf.get("y", 0) or 0)
    except Exception:
        px = py = 0.0
    if abs(px) > 1e-6 or abs(py) > 1e-6:
        out["pos"] = [round(px, 4), round(py, 4)]
    rot = c.get("rotation")
    if rot not in (None, 0, 0.0):
        out["rotation"] = rot
    flip = c.get("flip") or {}
    if flip.get("horizontal") or flip.get("vertical"):
        out["flip"] = [bool(flip.get("horizontal")), bool(flip.get("vertical"))]
    crop = c.get("crop") or {}
    if isinstance(crop, dict) and any(
            isinstance(v, (int, float)) and abs(v) > 1e-6
            for v in crop.values()):
        out["crop"] = True
    return out or None


def transform_segments(draft):
    """有非默认画面变换的视频段（送进达芬奇后构图会变的那些）。"""
    out, lane = [], 0
    for tr in (draft.get("tracks") or []):
        if not isinstance(tr, dict) or tr.get("type") != "video":
            continue
        lane += 1
        for sg in (tr.get("segments") or []):
            if not isinstance(sg, dict):
                continue
            tf = clip_transform(sg)
            if not tf:
                continue
            tt = sg.get("target_timerange") or {}
            out.append({
                "segment_id": sg.get("id"),
                "lane": lane,
                "start_sec": round(int(tt.get("start") or 0) / 1e6, 3),
                "transform": tf,
            })
    return out


def curve_speed_segments(draft):
    """引用「曲线变速」的段数。

    剪映的 speeds[].speed 是整段的平均速度；真正的节奏在 curve_speed 的
    speed_points 里。预渲染（bake）按平均速度做恒定变速，曲线会被拉平 ——
    帧位和总长对得上，观感不对。必须单独报出来。
    """
    mats = draft.get("materials") or {}
    curved = {m.get("id") for m in (mats.get("speeds") or [])
              if isinstance(m, dict) and m.get("curve_speed")}
    if not curved:
        return 0
    n = 0
    for tr in (draft.get("tracks") or []):
        for sg in (tr.get("segments") or []):
            if isinstance(sg, dict) and any(
                    r in curved for r in (sg.get("extra_material_refs") or [])):
                n += 1
    return n


def compound_inner_unsent(draft):
    """复合片段内部「有、但不会跟着进达芬奇」的元素。

    达芬奇里的复合片段是用内部**视频素材**重新合成的（CreateCompoundClip），
    内部的特效/调节/动画/文本根本没参与。外层看起来只有 1 段，用户不会想到
    里面少了东西，所以要单独报。
    """
    out = []
    for sid, comp in compound_index(draft).items():
        inner, how = compound_inner(comp)
        if not inner:
            continue
        mats = inner.get("materials") or {}
        ids = set()
        for key in ("effects", "video_effects", "material_animations", "hsl",
                    "color_curves", "common_mask", "masks"):
            for m in (mats.get(key) or []):
                if isinstance(m, dict) and m.get("id"):
                    ids.add(m["id"])
        n_ref = n_extra = 0
        for tr in inner.get("tracks") or []:
            if not isinstance(tr, dict):
                continue
            for sg in (tr.get("segments") or []):
                if not isinstance(sg, dict):
                    continue
                if any(r in ids for r in (sg.get("extra_material_refs") or [])):
                    n_ref += 1
            if (tr.get("type") or "") in ("text", "sticker", "filter"):
                n_extra += len(tr.get("segments") or [])
        if n_ref or n_extra:
            out.append({"compound": sid,
                        "name": str(comp.get("name") or "复合片段"),
                        "decorated_segments": n_ref,
                        "extra_tracks": n_extra, "from": how})
    return out


# 不送入达芬奇的东西：每一项都写清「为什么」和「写回会不会丢」。
# feature / label / why 是给人看的；writeback 表示写回时是否按快照原样带回。
UNSENT_SPEC = {
    "mask": ("蒙版", "达芬奇脚本接口没有蒙版入口（TimelineItem 无相关属性/方法）"),
    "mask_keyframes": ("蒙版关键帧", "同上：蒙版本身都送不进去"),
    "text": ("文本/字幕", "达芬奇字幕轨要 Subtitle 对象，脚本接口不能写文字内容"),
    "sticker": ("贴纸", "剪映贴纸是私有素材包，达芬奇读不了这个格式"),
    "filter": ("滤镜轨", "滤镜是剪映私有效果包，不是标准 LUT"),
    "video_effect": ("视频特效", "剪映特效包只能由剪映渲染"),
    "animation": ("入场/出场动画", "动画是关键帧曲线，脚本接口只能设静态变换"),
    "keyframes": ("段关键帧动画", "位置/缩放关键帧无法通过 AppendToTimeline 写入"),
    "transform": ("画面缩放/位移", "AppendToTimeline 不接受变换参数，达芬奇按"
                                  "项目分辨率铺满素材"),
    "speed": ("变速", "达芬奇没有变速接口；只有预渲染（bake）或占位对齐两种近似"),
    "curve_speed": ("曲线变速", "预渲染只能按平均速度做恒定变速，曲线节奏会被拉平"),
    "transition": ("转场", "脚本接口不能建转场；用「后段起点收前段时长」近似"),
    "audio_fade": ("音量/淡入淡出", "音频段送进去了，但音量曲线与淡入淡出是段上的"
                                  "参数，脚本接口没有可靠入口，不搬；写回时原样保留"),
    "compound_inner": ("复合片段内部的装饰", "达芬奇里的复合片段是用内部视频素材"
                                            "重新合成的，内部的特效/调节/动画/文本"
                                            "不会跟过去"),
    "adjust": ("调节（HSL/曲线）", "属于调色，应在达芬奇里重做；写回时保留剪映里的原设置"),
}


def unsupported(draft, plan=None):
    """草稿里有、但不会（或没法）送进达芬奇的元素清单。

    返回 list[dict]，每项：feature / label / count / where / why /
    sent=False / writeback=True。count 为 0 的不返回。

    「不送」是有意的：写回是从送入快照克隆的，这些元素天然保留，所以不在
    达芬奇里重现它们不会让成品丢东西；硬要塞进去反而会把时间线搞乱。
    """
    mats = draft.get("materials") or {}
    feats = feature_counts(draft)
    rows = []

    def add(feature, count, where=""):
        if not count:
            return
        label, why = UNSENT_SPEC[feature]
        rows.append({"feature": feature, "label": label, "count": count,
                     "where": where, "why": why, "sent": False,
                     "writeback": True})

    add("mask", feats.get("segments_with_mask", 0), "视频段")
    add("mask_keyframes", feats.get("segments_with_mask_keyframes", 0), "视频段")
    add("text", sum(len(t.get("segments") or []) for t in draft.get("tracks") or []
                    if (t.get("type") or "") == "text"), "文字轨")
    add("sticker", sum(len(t.get("segments") or []) for t in draft.get("tracks") or []
                       if (t.get("type") or "") == "sticker"), "贴纸轨")
    add("filter", sum(len(t.get("segments") or []) for t in draft.get("tracks") or []
                      if (t.get("type") or "") == "filter"), "滤镜轨")
    # 音频与图片现在都送了（音频进 A 轨、图片转成等长视频），不再算「不送」。
    # 送没送成看 send.json 的 audio / photos 两个字段，那里有逐段结果。
    add("video_effect", len(mats.get("video_effects") or []), "素材特效")
    add("animation", feats.get("segments_with_animation", 0), "视频段")
    # 非蒙版的关键帧：蒙版关键帧已单列，这里只算「除蒙版以外」的那部分
    n_kf = feats.get("segments_with_keyframes", 0)
    n_mask_kf = feats.get("segments_with_mask_keyframes", 0)
    add("keyframes", max(0, n_kf - n_mask_kf), "视频段")
    add("transform", len(transform_segments(draft)), "视频段")
    if feats.get("segments_with_speed"):
        add("speed", feats["segments_with_speed"], "视频段")
    add("curve_speed", curve_speed_segments(draft), "视频段")
    if feats.get("segments_with_transition"):
        add("transition", feats["segments_with_transition"], "视频段")
    add("audio_fade", len(mats.get("audio_fades") or []), "音频")
    n_adj = len(mats.get("hsl") or []) + len(mats.get("color_curves") or [])
    add("adjust", n_adj, "调节")
    # count 统一按「元素个数」：内部几段带装饰就算几段，不是复合片段个数。
    _ci = compound_inner_unsent(draft)
    add("compound_inner",
        sum(c["decorated_segments"] + c["extra_tracks"] for c in _ci),
        "复合片段内部")
    return rows


def feature_counts(draft):
    """转场 / 关键帧 / 动画 / 蒙版 / 变速 的实际数量。

    这些元素不需要搬进达芬奇（写回是从剪映草稿克隆的，天然保留），
    但必须**数出来**：一来说明它们确实在草稿里、二来用于写回后逐项核对。
    """
    mats = draft.get("materials") or {}
    trans_mat = {m.get("id"): m for m in (mats.get("transitions") or [])
                 if isinstance(m, dict)}
    kf_mat = mats.get("keyframes") or []
    anim = {m.get("id") for m in (mats.get("material_animations") or [])
            if isinstance(m, dict)}
    # 剪映 11.x 的蒙版在 materials.common_mask（老版本用过 materials.masks；
    # 段上的 enable_adjust_mask 是「调节蒙版」开关，两回事）。名字认错会数出 0，
    # 报告里就少一行，用户以为蒙版没被识别。
    mask_mat = mask_map(draft)

    out = {"transition_materials": len(trans_mat),
           "keyframe_materials": len(kf_mat),
           "mask_materials": len(mask_mat),
           "segments_with_transition": 0,
           "segments_with_keyframes": 0,
           "segments_with_animation": 0,
           "segments_with_mask": 0,
           "segments_with_mask_keyframes": 0,
           "segments_with_speed": 0,
           "segments_with_compound": 0,
           # 停用段：段上 visible:false（剪映只在这个状态才写这个键，
           # 正常段上根本没有这个字段）。它们仍占着时间线，只是不渲染。
           "segments_disabled": 0,
           "compound_names": [],
           "mask_names": [],
           "keyframe_total": 0,
           "transition_names": [],
           "speed_values": []}

    cindex = compound_index(draft)
    spd = speeds_by_ref(draft)
    for tr in draft.get("tracks") or []:
        for sg in (tr.get("segments") or []):
            if not isinstance(sg, dict):
                continue
            refs = sg.get("extra_material_refs") or []
            if sg.get("visible") is False:
                out["segments_disabled"] += 1
            if any(r in trans_mat for r in refs):
                out["segments_with_transition"] += 1
                for r in refs:
                    if r in trans_mat:
                        nm = trans_mat[r].get("name") or trans_mat[r].get("type")
                        if nm:
                            out["transition_names"].append(str(nm))
            if any(r in anim for r in refs):
                out["segments_with_animation"] += 1
            mk = [r for r in refs if r in mask_mat]
            if mk or sg.get("enable_adjust_mask"):
                out["segments_with_mask"] += 1
                for r in mk:
                    nm = (mask_mat[r].get("name")
                          or mask_mat[r].get("resource_type"))
                    if nm:
                        out["mask_names"].append(str(nm))
            ck = sg.get("common_keyframes") or []
            if ck:
                out["segments_with_keyframes"] += 1
                out["keyframe_total"] += sum(len(k.get("keyframe_list") or [])
                                             for k in ck if isinstance(k, dict))
                # 蒙版关键帧：property_type 形如 KFTypeCommonMaskPositionX/Y，
                # 也有人把它们挂在蒙版材质的 constant_material_id 上。
                if mask_keyframe_groups(sg, mask_mat):
                    out["segments_with_mask_keyframes"] += 1
            # 按「段」统计，不按 ref 统计：一段可能挂多个 speed 材质，
            # 老写法用 for r in refs 计数，同一段能被数两三次，报出来的
            # 「变速段 5」和时间线上看到的 3 段对不上。
            sp = segment_speed(sg, spd)
            if abs(sp - 1.0) > 1e-6:
                out["segments_with_speed"] += 1
                out["speed_values"].append(sp)
            comp = compound_of(sg, cindex)
            if comp is not None:
                out["segments_with_compound"] += 1
                out["compound_names"].append(
                    str(comp.get("name") or "复合片段"))
    return out


def transition_records(draft):
    """把每段的转场、关键帧、蒙版摘成可核对的记录（写回后逐条比对用）。"""
    mats = draft.get("materials") or {}
    trans_mat = {m.get("id"): m for m in (mats.get("transitions") or [])
                 if isinstance(m, dict)}
    mask_mat = mask_map(draft)
    recs = {}
    for tr in draft.get("tracks") or []:
        for sg in (tr.get("segments") or []):
            if not isinstance(sg, dict) or not sg.get("id"):
                continue
            refs = sg.get("extra_material_refs") or []
            trans = [{"id": r, "name": (trans_mat.get(r) or {}).get("name"),
                      "duration": (trans_mat.get(r) or {}).get("duration")}
                     for r in refs if r in trans_mat]
            ck = sg.get("common_keyframes") or []
            recs[sg["id"]] = {
                "transitions": trans,
                "masks": sorted(r for r in refs if r in mask_mat),
                "mask_names": sorted(str(mask_mat[r].get("name") or "")
                                     for r in refs if r in mask_mat),
                "common_keyframes": json.dumps(ck, ensure_ascii=False,
                                               sort_keys=True),
                "n_keyframe_groups": len(ck),
                "target": (sg.get("target_timerange") or {}).get("start"),
                "dur": (sg.get("target_timerange") or {}).get("duration"),
                "src": (sg.get("source_timerange") or {}).get("start"),
            }
    return recs


def dump_json(obj, path):
    paths.ensure(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
    return path


def fail(rep, path, why, **extra):
    """收尾一条失败路径：写结论、落报告、返回报告。调用方一行 return。"""
    rep["error"] = why
    rep.update(extra)
    dump_json(rep, path)
    return rep


def prog(msg):
    """进度播报：一句人话，面板据此显示「现在在干嘛」。

    只在有人看的时候说话 —— 终端里（isatty）或者面板显式开了
    COLORPASS_PROGRESS。别的场合一律闭嘴：
      · 输出被重定向到文件/管道时，多出来的行会污染 JSON（测试里
        json.loads(stdout) 会直接炸）；
      · 大批量跑压测时，几百行噪音没有意义。
    """
    try:
        if not (os.environ.get("COLORPASS_PROGRESS") or sys.stdout.isatty()):
            return
        print("·· " + msg, flush=True)
    except Exception:
        pass


# ---------- 变速 ----------

def speeds_by_ref(draft):
    """{材质ID: 速度值}。剪映把变速放在 materials.speeds 里，段通过
    extra_material_refs 引用；段自己也可能带一个 speed 数值字段。"""
    out = {}
    for m in (draft.get("materials") or {}).get("speeds") or []:
        if isinstance(m, dict) and m.get("id"):
            try:
                out[m["id"]] = float(m.get("speed") or 1.0)
            except (TypeError, ValueError):
                out[m["id"]] = 1.0
    return out


def segment_speed(seg, speed_map):
    """这一段的实际速度倍率（1.0 表示没变速）。"""
    v = seg.get("speed")
    if isinstance(v, (int, float)) and v and abs(float(v) - 1.0) > 1e-6:
        return float(v)
    for r in seg.get("extra_material_refs") or []:
        if r in speed_map and abs(speed_map[r] - 1.0) > 1e-6:
            return speed_map[r]
    return 1.0


# ---------- 复合片段 ----------

def compound_index(draft):
    """{材质ID: 复合片段条目}。

    判定依据是实测出来的：复合片段那一段的 extra_material_refs 里，
    一定会包含 materials.drafts[].id（见 subdraft 的真实数据）。
    """
    out = {}
    for e in (draft.get("materials") or {}).get("drafts") or []:
        if isinstance(e, dict) and e.get("id"):
            out[str(e["id"])] = e
    return out


def compound_of(seg, cindex):
    """这一段是不是复合片段；是就返回对应条目。"""
    for r in seg.get("extra_material_refs") or []:
        if r in cindex:
            return cindex[r]
    return None


def compound_path(entry, folder=None):
    """复合片段内容文件在**某份草稿目录**下的绝对路径。

    macOS 的 `draft_file_path` 是 `##_draftpath_placeholder_<uuid>_##/subdraft/…`
    这种相对占位符，必须先用草稿目录把它展开，否则 `os.path.exists` 永远是
    False —— 于是「复合片段内部时间线」两边都读不到，被当成「一字节没变」。
    """
    p = str((entry or {}).get("draft_file_path") or "")
    if not p:
        return ""
    rel, ph = subdraft_rel(p, folder or "")
    if rel is None:
        return p
    if folder:
        return str(Path(str(folder)) / rel)
    return "" if ph else str(rel)


def compound_inner(entry, folder=None):
    """取复合片段内部的时间线。

    两条来源，优先级很重要：
      1) entry["draft"] —— 内存快照里内嵌的完整时间线（最新）
      2) entry["draft_file_path"] —— 磁盘上的子草稿 JSON，**明文**，
         即使母稿被加密也能读（实测：subdraft/<uuid>/draft_content.json 首字节是 {）
         mac 上这条是占位符路径，要靠 `folder`（草稿目录）展开。
    """
    inner = entry.get("draft")
    if isinstance(inner, dict) and (inner.get("tracks")):
        return inner, "embedded"
    p = compound_path(entry, folder)
    if p and os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                inner = json.load(f)
            if isinstance(inner, dict) and inner.get("tracks"):
                return inner, "file"
        except Exception:
            pass
    return None, "missing"


# 剪映 macOS 11.8 的复合片段路径不是绝对路径，而是一段「本草稿目录」占位符：
#   ##_draftpath_placeholder_0E685133-..._##/subdraft/F4AB9BA1-.../draft_content.json
# 占位符由剪映在运行时替换成当前草稿目录。上一版只认绝对路径（`..` 判断），
# 于是 Mac 上每一个复合片段都被判成「子草稿不在源草稿的 subdraft 下」，
# 复制被整段跳过 —— 写回出来的新草稿里复合片段自然是空的（用户看到的
# 「复合片段会缺失」就是这条）。
_PLACEHOLDER = "##_draftpath_placeholder_"


def subdraft_rel(path, folder):
    """把 draft_file_path 归一成「草稿目录内的相对路径」。

    返回 (相对路径 or None, 是否占位符形式)。占位符形式**不要**改写成绝对
    路径：它本来就是相对当前草稿解析的，跟着新草稿走才对。
    """
    p = str(path or "")
    if not p:
        return None, False
    if p.startswith(_PLACEHOLDER):
        head, sep, tail = p.partition("_##")
        if not sep:
            return None, True
        rel = tail.lstrip("/\\")
        return (Path(rel) if rel else None), True
    try:
        return Path(p).relative_to(Path(str(folder))), False
    except Exception:
        return None, False


def expand_compound(inner, depth=0, max_depth=3):
    """复合片段内部 → 可摆放的视频片段列表（递归展开嵌套复合）。

    时间都是微秒，相对复合片段自己的零点。
    """
    if not isinstance(inner, dict) or depth > max_depth:
        return []
    out = []
    vids = {v.get("id"): v for v in (inner.get("materials") or {}).get("videos") or []
            if isinstance(v, dict)}
    cindex = compound_index(inner)
    spd = speeds_by_ref(inner)
    v_tracks = [t for t in (inner.get("tracks") or [])
                if isinstance(t, dict) and t.get("type") == "video"]
    for lane, tr in enumerate(v_tracks):
        for sg in (tr.get("segments") or []):
            if not isinstance(sg, dict):
                continue
            tt = sg.get("target_timerange") or {}
            st = sg.get("source_timerange") or {}
            sub = compound_of(sg, cindex)
            if sub is not None:
                sin, src = compound_inner(sub)
                for p in expand_compound(sin, depth + 1, max_depth):
                    p = dict(p)
                    p["rec_us"] += int(tt.get("start") or 0)
                    p["lane"] = lane + 1 + (p.get("lane") or 1) - 1
                    p["nested"] = True
                    out.append(p)
                continue
            v = vids.get(sg.get("material_id")) or {}
            p = str(v.get("path") or "").replace("\\", "/")
            out.append({"path": p, "lane": lane + 1,
                        "rec_us": int(tt.get("start") or 0),
                        "dur_us": int(tt.get("duration") or 0),
                        "src_us": int(st.get("start") or 0),
                        "speed": segment_speed(sg, spd),
                        # 复合片段内部的停用段：带出来就好，怎么处理由 send
                        # 决定（CreateCompoundClip 遇到禁用片段的行为没实测过，
                        # 不敢在这里自作主张）
                        "enabled": sg.get("visible") is not False,
                        "segment_id": sg.get("id")})
    # 内部图层编号要归一化成 1..k：剪映的复合片段内部常常从 V2 起才有内容
    # （V1 空着），若照搬原始轨号，合成时会整体上移一层，和外层片段的
    # 上下关系就错了。实测：内部 V2/V3 → 不归一化合成项落在 V2，
    # 归一化后落在 V1，与剪映里复合片段所在的轨道一致。
    used = sorted({p["lane"] for p in out})
    remap = {old: i + 1 for i, old in enumerate(used)}
    for p in out:
        p["lane"] = remap.get(p["lane"], p["lane"])
    return out

