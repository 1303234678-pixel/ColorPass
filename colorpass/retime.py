"""变速段预渲染 —— 唯一能让「变速」真的出现在达芬奇里的办法。

为什么必须这么做
----------------
达芬奇脚本接口没有任何设置片段速度的入口（实测：GetProperty 的 26 个键里
没有 Speed；SetProperty("Speed", 2.0) 返回 False；SetSpeed / ChangeSpeed /
SetRetimeSpeed / SetClipSpeed / Timeline.SetClipsSpeed 等 11 个候选全是 None）。

所以把源区间按 1x 摆进去，永远只能得到「正常速度」：

    剪映：源 1.233s 压进 0.667s（1.85 倍速）
    达芬奇：占位 0.667s，1x 只吃到源的前 0.667s —— 后面 0.567s 画面没了

用户在达芬奇里看到的就是「有的段像变速了、有的没变」，实际是每段都丢了
尾巴（加速）或越界读了出点之后的画面（慢放）。

做法：先用 ffmpeg 把该段的源区间按 speed 重采样成一份新素材，长度正好等于
剪映的占位，帧率 30，ProRes 422 10bit（调色不丢色深，体积约 18 MB/s）。
摆进达芬奇时它就是普通 1x 素材 —— 时长、内容、帧号全部和剪映一致，调色结果
也能原样回写。

成本：每段编码一次，落在 %LOCALAPPDATA%\\ColorPass\\retime\\<草稿ID>\\。
同一段（路径+区间+速度）只编一次，重复送入直接复用。
"""
import os
import re
import subprocess

from . import paths

FPS = paths.FPS


_FF = None


def _ffmpeg():
    """ffmpeg 可执行文件路径。缓存一次：available() 每次送入都会调，
    imageio_ffmpeg.get_ffmpeg_exe() 内部要扫包目录，反复调纯属浪费。"""
    global _FF
    if _FF is None:
        try:
            import imageio_ffmpeg
            _FF = imageio_ffmpeg.get_ffmpeg_exe() or ""
        except Exception:
            _FF = ""
    return _FF


def available():
    exe = _ffmpeg()
    return bool(exe) and os.path.exists(exe)


def _has_audio(path):
    """探一下有没有音轨（ffmpeg 的 atempo 只能作用在真音轨上）。

    返回值三态：True / False / None（不知道）。没有 PyAV 时不能直接当
    「没有音轨」—— 那会让预渲染偷偷丢掉声音，而且完全不报错。
    None 的语义是「先试着带音频编一次，失败再退回无声」。
    """
    try:
        import av
        with av.open(path) as c:
            return any(getattr(s, "type", "") == "audio" for s in c.streams)
    except ImportError:
        return None
    except Exception:
        return None


def _atempo(speed):
    """atempo 单次只接受 0.5~2.0，超出就串联。"""
    s, parts = float(speed), []
    while s > 2.0:
        parts.append(2.0)
        s /= 2.0
    while s < 0.5:
        parts.append(0.5)
        s /= 0.5
    parts.append(s)
    return ",".join("atempo=%.6f" % p for p in parts)


def cache_path(draft_id, seg_id, speed, src_us, want_frames, range_us=None):
    """缓存文件名 = 段号 + 入点 + 速度 + 目标帧数 (+ 源区间长度)。

    为什么要把源区间也算进去：用户只把出点往前拖（入点、速度、占位都不变）时，
    前四个参数完全一样，老写法会命中上一版的缓存，把已经删掉的那几帧画面又
    摆回时间线 —— 而且没有任何报错。多一个 r 字段，区间一变就换文件。
    """
    key = "%s_%d_%.4f_%d" % (str(seg_id)[:8], int(src_us / 1000),
                             speed, want_frames)
    if range_us is not None:
        key += "_r%d" % int(int(range_us) / 1000)
    key = key.replace(".", "p").replace("-", "m")
    return os.path.join(paths.retime_dir(draft_id), key + ".mov")


def bake(path, src_us, src_dur_us, speed, want_frames, out_path,
         timeout=900):
    """把 path 的 [src_us, src_us+src_dur_us) 按 speed 重采样成 want_frames 帧。

    返回 (ok, detail)。已存在且非空就直接复用（同一段只编一次）。
    """
    if os.path.exists(out_path) and os.path.getsize(out_path) > 4096:
        return True, {"cached": True, "out": out_path}
    exe = _ffmpeg()
    if not exe:
        return False, {"why": "找不到 ffmpeg（imageio_ffmpeg 未安装）"}
    if not os.path.exists(path):
        return False, {"why": "素材不在盘上：%s" % path}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".part.mov"
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass

    ss = max(0.0, src_us / 1e6)
    t = max(0.001, src_dur_us / 1e6)
    vf = ("setpts=(PTS-STARTPTS)/%.6f,fps=%d,format=yuv422p10le"
          % (float(speed), FPS))

    def _cmd(with_audio):
        c = [exe, "-hide_banner", "-v", "error", "-y",
             "-ss", "%.6f" % ss, "-t", "%.6f" % t, "-i", path]
        if with_audio:
            fc = ("[0:v]%s[v];[0:a]%s[a]" % (vf, _atempo(speed)))
            c += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                  "-c:a", "pcm_s16le"]
        else:
            c += ["-filter_complex", "[0:v]%s[v]" % vf, "-map", "[v]", "-an"]
        c += ["-frames:v", str(int(want_frames)),
              "-c:v", "prores_ks", "-profile:v", "2",
              "-vendor", "apl0", "-pix_fmt", "yuv422p10le",
              "-r", str(FPS), tmp]
        return c

    audio = _has_audio(path)
    # False = 确认没有音轨，直接无声编码；True/None = 先带音频试一次，
    # 失败（比如其实没音轨）再退回无声。多花的那一次失败很快（filter 初始化就报错）。
    tries = [False] if audio is False else [True, False]
    err = ""
    for wa in tries:
        p = subprocess.run(_cmd(wa), capture_output=True, text=True,
                           errors="ignore", timeout=timeout)
        if p.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 4096:
            os.replace(tmp, out_path)
            return True, {"cached": False, "out": out_path, "audio": wa,
                          "bytes": os.path.getsize(out_path),
                          "ffmpeg": exe}
        err = (p.stderr or "")[-400:]
    return False, {"why": "ffmpeg 编码失败：%s" % (err or "未知"), "cmd": _cmd(False)}


_SAFE_ID = re.compile(r"[0-9A-Za-z_-]{1,64}\Z")


def clean(draft_id=None):
    """删掉预渲染缓存。draft_id 为空就清整个 retime 目录。

    这些是我们自己生成的中转素材（ProRes 体积大：1080p30 约 18 MB/s），
    草稿定稿后可以放心清掉 —— 原素材和剪映草稿都不受影响。

    draft_id 必须先过白名单。它来自命令行，而 `paths.retime_dir` 会
    `os.path.join` 后 `makedirs`，再被 `shutil.rmtree` ——
    一个 `--draft-id ..` 就会把 `%LOCALAPPDATA%\ColorPass` 整个删掉
    （current.json、所有 runs 快照、日志全在里面），`..\..` 更是直接
    打到 `%LOCALAPPDATA%`。
    """
    import shutil
    if draft_id is not None:
        sid = str(draft_id)
        if not _SAFE_ID.match(sid) or sid in (".", ".."):
            return {"ok": False, "root": "", "files": 0, "freed_bytes": 0,
                    "error": "草稿 ID 不合法：%r（只接受字母、数字、下划线、连字符）"
                             % sid}
    root = paths.retime_dir(draft_id) if draft_id else paths.RETIME_ROOT
    freed, files = 0, 0
    if not os.path.isdir(root):
        return {"ok": True, "root": root, "files": 0, "freed_bytes": 0}
    for base, _dirs, names in os.walk(root):
        for n in names:
            p = os.path.join(base, n)
            try:
                freed += os.path.getsize(p)
                files += 1
            except OSError:
                pass
    try:
        shutil.rmtree(root)
    except Exception as e:
        return {"ok": False, "root": root, "error": str(e),
                "files": files, "freed_bytes": freed}
    return {"ok": True, "root": root, "files": files, "freed_bytes": freed}


def bake_plan(plan, draft_id):
    """给计划里 kind=video 且需要变速的段补上 bake 素材路径。

    返回 (baked, failed)。baked 里的 c["path"] 已换成新素材，c["src_us"]=0。
    """
    baked, failed = [], []
    for c in plan:
        if not c.get("bake"):
            continue
        out = cache_path(draft_id, c["segment_id"], c["speed"],
                         c["src_us"], c["place"], c.get("range_us"))
        ok, d = bake(c["path"], c["src_us"], c["range_us"], c["speed"],
                     c["place"], out)
        if not ok:
            failed.append({"segment_id": c["segment_id"], **d})
            continue
        c["bake_src_path"] = c["path"]
        c["bake_src_us"] = c["src_us"]
        c["path"] = out
        c["src_us"] = 0
        c["src_frames"] = c["place"]     # 新素材正好这么多帧
        baked.append({"segment_id": c["segment_id"], "speed": round(c["speed"], 4),
                      "want": c["place"], "out": out, "cached": d.get("cached"),
                      "audio": d.get("audio")})
    return baked, failed
