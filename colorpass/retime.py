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
import shutil
import subprocess
import sys

from . import paths

FPS = paths.FPS


_FF = None


def bundled():
    """随安装包发出去的那份 ffmpeg 在哪（没有就返回空串）。

    两个位置都算「自带」：
      · 安装版：`%LOCALAPPDATA%\\ColorPass\\bin\\_internal\\ffmpeg\\ffmpeg.exe`
        —— PyInstaller 6 的 onedir 把随包数据放在 `_internal/`，运行时
        `sys._MEIPASS` 就指着它；
      · 源码模式：`<仓库>/vendor/ffmpeg/`，由 tools/fetch_ffmpeg.py 放进去。

    为什么它排在最前面：用户机器上可能装着另一个老版本 ffmpeg（甚至 PATH 上
    是个残缺构建），那会把「我们测过的行为」变成不可控。自带的那份是构建时
    校验过编码器/滤镜的，优先用它，结果才可复现。
    """
    name = "ffmpeg.exe" if paths.WINDOWS else "ffmpeg"
    # (根目录, 子目录)：打包版是 exe 旁的 ffmpeg/（含 PyInstaller 6 的
    # _internal/ 那一档），源码模式在 vendor/ffmpeg/
    pairs = [(os.path.dirname(os.path.abspath(sys.executable)), "ffmpeg")]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        pairs.append((meipass, "ffmpeg"))
    if not getattr(sys, "frozen", False):
        # 源码模式：colorpass/ 的上一级就是仓库根
        pairs.append((os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      os.path.join("vendor", "ffmpeg")))
    for root, sub in pairs:
        p = os.path.join(root, sub, name)
        if os.path.isfile(p):
            return p
    return ""


def missing_reason():
    """找不到 ffmpeg 时该跟用户说什么。

    自带之后这已经不是「用户没装」，而是「安装包残缺 / 文件被杀毒软件删了」，
    所以说的话也得换：让人重装，而不是让人去配 PATH。

    macOS / Linux 例外：仓库里 `vendor/ffmpeg/` 只有 Windows 的 exe，源码模式
    根本没有「随包自带」这回事。这时候教人「重新运行安装包」是错的（那边也
    没有安装包），得直接说怎么装。
    """
    if not paths.WINDOWS:
        return ("找不到 ffmpeg。macOS 上先装一份：brew install ffmpeg"
                "（或者 python3 -m pip install imageio-ffmpeg）；"
                "已经装过的话用 COLORPASS_FFMPEG 指给它。")
    return ("找不到随包自带的 ffmpeg（安装包应包含 "
            "bin\\_internal\\ffmpeg\\ffmpeg.exe）。"
            "通常是被杀毒软件拦了或安装不完整 —— 重新运行一次安装包即可；"
            "源码模式请先运行 python tools/fetch_ffmpeg.py")


def _ffmpeg():
    """ffmpeg 可执行文件路径。缓存一次：available() 每次送入都会调，
    imageio_ffmpeg.get_ffmpeg_exe() 内部要扫包目录，反复调纯属浪费。

    取值顺序：COLORPASS_FFMPEG（显式指定）→ 随包自带 → PATH → imageio_ffmpeg。
    后两档只为开发机方便，打包版一定能命中第二档。
    """
    global _FF
    if _FF is None:
        env = os.environ.get("COLORPASS_FFMPEG")
        if env and os.path.isfile(env):
            _FF = env
            return _FF
        _FF = bundled()
        if _FF:
            return _FF
        found = shutil.which("ffmpeg")
        if found:
            _FF = found
            return _FF
        try:
            import imageio_ffmpeg
            _FF = imageio_ffmpeg.get_ffmpeg_exe() or ""
        except Exception:
            _FF = ""
    return _FF


def available():
    exe = _ffmpeg()
    return bool(exe) and os.path.exists(exe)


def info():
    """doctor / 报告用：这份 ffmpeg 是哪来的、版本多少。不跑进程，很快。"""
    exe = _ffmpeg()
    if not exe or not os.path.exists(exe):
        return {"ok": False, "exe": "", "why": missing_reason()}
    env = os.environ.get("COLORPASS_FFMPEG")
    if env and os.path.abspath(env) == os.path.abspath(exe):
        src = "env"
    elif os.path.abspath(exe) == os.path.abspath(bundled() or ""):
        src = "bundled"
    elif exe == shutil.which("ffmpeg"):
        src = "path"
    else:
        src = "imageio_ffmpeg"
    return {"ok": True, "exe": exe, "source": src,
            "bytes": os.path.getsize(exe)}


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


def video_filter(speed, size=None):
    """变速 + 落画布的滤镜链。

    为什么要把画布缩下来（实测数据，见 docs/性能.md）：
    4K 源按原分辨率编 ProRes 422 10bit，1080p 时间线其实一点也用不上那多出来
    的像素 —— 达芬奇摆上时间线时本来就会缩到工程分辨率。实测同一段 63 秒
    （1903 帧）的 4K 素材：原分辨率 prores_ks 82 秒 / 4.5 GB，缩到 1080p 用
    prores_aw 34 秒 / 1.4 GB。画面在时间线上逐帧一致，因为两者最终都是
    「4K 源 → 1920x1080 时间线」。

    只在**会缩小**时缩：`min(iw,W)` 让 1080p 源保持 1080p，不做无谓的放大
    （放大既慢又不可能多出细节，达芬奇自己会处理）。

    顺序是 fps 在前、scale 在后：变速 1.45x 时源帧比输出帧多，先按 fps 抽到
    目标帧数再缩，缩放次数少三成。
    """
    parts = ["setpts=(PTS-STARTPTS)/%.6f" % float(speed), "fps=%d" % FPS]
    if size:
        try:
            w, h = int(size[0]), int(size[1])
        except (TypeError, ValueError, IndexError):
            w = h = 0
        if w > 0 and h > 0:
            parts.append("scale=w='min(iw,%d)':h='min(ih,%d)'"
                         ":force_original_aspect_ratio=decrease"
                         ":force_divisible_by=2" % (w, h))
            parts.append("setsar=1")
    parts.append("format=yuv422p10le")
    return ",".join(parts)


# 本机能用的 ProRes 编码器，按速度排序（实测 1080p 300 帧：
# prores_aw 1.0 秒 / prores_ks 2.9 秒，两者都是 10bit 4:2:2 Standard）。
# 记住第一次成功的那个，后面每段都直接用，不用再试错。
_ENCODER = [""]


def _encoder_order():
    return [_ENCODER[0]] if _ENCODER[0] else ["prores_aw", "prores_ks"]


def cache_path(draft_id, seg_id, speed, src_us, want_frames, range_us=None,
               size=None):
    """缓存文件名 = 段号 + 入点 + 速度 + 目标帧数 (+ 源区间长度 + 画布)。

    为什么要把源区间也算进去：用户只把出点往前拖（入点、速度、占位都不变）时，
    前四个参数完全一样，老写法会命中上一版的缓存，把已经删掉的那几帧画面又
    摆回时间线 —— 而且没有任何报错。多一个 r 字段，区间一变就换文件。

    画布同理：老版本按源分辨率编（4K 源留下 4.5 GB 的缓存），现在按时间线
    画布编。不把画布写进键，升级后仍会命中那个 4K 旧文件 —— 慢、占盘，而且
    白白错过这次的提速。键一变自然重编，旧的交给 gc 回收。
    """
    key = "%s_%d_%.4f_%d" % (str(seg_id)[:8], int(src_us / 1000),
                             speed, want_frames)
    if range_us is not None:
        key += "_r%d" % int(int(range_us) / 1000)
    if size:
        try:
            key += "_%dx%d" % (int(size[0]), int(size[1]))
        except (TypeError, ValueError, IndexError):
            pass
    key = key.replace(".", "p").replace("-", "m")
    return os.path.join(paths.retime_dir(draft_id), key + ".mov")


def bake(path, src_us, src_dur_us, speed, want_frames, out_path,
         size=None, timeout=900):
    """把 path 的 [src_us, src_us+src_dur_us) 按 speed 重采样成 want_frames 帧。

    size=(w, h) 是时间线画布：编出来的素材缩到这个画布以内（只缩不放）。
    不传就按源分辨率编 —— 那是老行为，4K 源会产出 4.5 GB 的中转文件。

    返回 (ok, detail)。已存在且非空就直接复用（同一段只编一次）。
    """
    _USED.add(out_path)          # 命中缓存也算「本次在用」，见 prune_unused
    if os.path.exists(out_path) and os.path.getsize(out_path) > 4096:
        return True, {"cached": True, "out": out_path}
    exe = _ffmpeg()
    if not exe:
        return False, {"why": missing_reason()}
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
    vf = video_filter(speed, size)

    def _cmd(with_audio, enc="prores_ks"):
        c = [exe, "-hide_banner", "-v", "error", "-y",
             "-ss", "%.6f" % ss, "-t", "%.6f" % t, "-i", path]
        if with_audio:
            fc = ("[0:v]%s[v];[0:a]%s[a]" % (vf, _atempo(speed)))
            c += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                  "-c:a", "pcm_s16le"]
        else:
            c += ["-filter_complex", "[0:v]%s[v]" % vf, "-map", "[v]", "-an"]
        c += ["-frames:v", str(int(want_frames)),
              "-c:v", enc, "-profile:v", "2",
              "-vendor", "apl0", "-pix_fmt", "yuv422p10le",
              "-r", str(FPS), tmp]
        return c

    audio = _has_audio(path)
    # False = 确认没有音轨，直接无声编码；True/None = 先带音频试一次，
    # 失败（比如其实没音轨）再退回无声。多花的那一次失败很快（filter 初始化就报错）。
    tries = [False] if audio is False else [True, False]
    err, last_enc = "", ""
    # 外层换编码器（prores_aw 快 2.8 倍，个别构建没有它），内层换音频形态。
    for enc in _encoder_order():
        last_enc = enc
        for wa in tries:
            p = subprocess.run(_cmd(wa, enc), capture_output=True, text=True,
                               errors="ignore", timeout=timeout)
            if (p.returncode == 0 and os.path.exists(tmp)
                    and os.path.getsize(tmp) > 4096):
                os.replace(tmp, out_path)
                _ENCODER[0] = enc
                return True, {"cached": False, "out": out_path, "audio": wa,
                              "enc": enc, "bytes": os.path.getsize(out_path),
                              "ffmpeg": exe}
            err = (p.stderr or "")[-400:]
        if "Unknown encoder" not in err:
            break
    return False, {"why": "ffmpeg 编码失败：%s" % (err or "未知"),
                   "cmd": _cmd(False, last_enc or "prores_ks")}


def photo_cache_path(draft_id, path, frames, fps=FPS, width=1920, height=1080):
    """静帧转码的缓存名：源图（路径 + 修改时间 + 大小）+ 目标帧数 + 帧率 + 画布。

    必须把 mtime/size 也算进键：用户拿同名文件换掉一张图时，只按路径做键会
    沿用旧画面 —— 而且时间线看上去一切正常，这种错最难发现。
    """
    try:
        st = os.stat(path)
        stamp = "%d_%d" % (int(st.st_mtime), int(st.st_size))
    except OSError:
        stamp = "nostat"
    stem = os.path.splitext(os.path.basename(path))[0][:24]
    key = "%s_%s_%d_%d_%dx%d" % (stem, stamp, int(frames), int(fps),
                                 int(width), int(height))
    # 只替换文件系统不认的字符：中文留着更好认，排查时一眼能看出是哪张图
    key = re.sub(r'[\\/:*?"<>|]+', "_", key).strip(" ._")
    return os.path.join(paths.photo_dir(draft_id), key + ".mp4")


def _probe_image(path, timeout=8):
    """这张图能不能解出一帧。返回 (ok, why)。

    为什么值得多起一个进程：转码用的是 `-loop 1`，遇到**损坏/不支持的图片**
    时 image2 解码器会一直重试同一份坏数据 —— 表现出来是「卡住」，不是报错。
    实测一张假 png 能卡满默认超时（300 秒），一张坏图就把整批图片段拖死。
    这里先解一帧（好图约 40 ms，坏图 40 ms 就返回非 0），坏图快速失败，
    报错还带 ffmpeg 的原话，用户知道是哪张图、为什么不行。
    """
    exe = _ffmpeg()
    if not exe:
        return False, missing_reason()
    cmd = [exe, "-hide_banner", "-nostdin", "-v", "error", "-i", path,
           "-frames:v", "1", "-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           errors="ignore", timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "图片解码超时：文件可能损坏，或这个格式不受支持"
    if p.returncode != 0:
        return False, "图片读不出来：%s" % ((p.stderr or "").strip()[-200:]
                                         or "未知原因")
    return True, ""


def photo_clip(path, frames, fps=FPS, width=1920, height=1080,
               out_path=None, crf=10, timeout=120):
    """把一张静帧转成「正好 frames 帧」的普通视频，返回 (ok, detail)。

    为什么非转不可（21.2 实测，见 tools/lab_ia.py）：
      · 静帧素材 `Frames=1`、`Duration=00:00:00:01`；
      · `AppendToTimeline` 的 endFrame 被**完全忽略**，摆上去恒为 150 帧
        （5 秒，达芬奇的默认静帧时长）；
      · `SetProperty("Duration"/"ClipDuration"/"EndFrame"/"SourceDuration")`
        全部返回 False，摆完也改不了。
    剪映里一张图摆 2.4 秒，达芬奇里却占 5 秒 —— 后面每一段的位置、以及
    写回时的对齐校验全都会错。

    转成视频之后它就只是个普通素材：帧数、时长、取色、摆放都可控。
    顺便按项目画布做等比缩放 + 居中补边，和剪映里「适应画布」看到的一致。
    用 crf 10 的 H.264（视觉接近无损，1080p 5 秒约 1 MB）；静帧在写回时
    用的是原图，这里的编码只影响达芬奇里的预览与取色参考。
    """
    if not os.path.exists(path):
        return False, {"why": "图片不在盘上：%s" % path}
    frames = max(1, int(frames))
    if out_path is None:
        out_path = os.path.join(paths.PHOTO_ROOT, "tmp_%d.mp4" % frames)
    if os.path.exists(out_path) and os.path.getsize(out_path) > 4096:
        return True, {"cached": True, "out": out_path}
    exe = _ffmpeg()
    if not exe:
        return False, {"why": missing_reason()}
    ok, why = _probe_image(path)
    if not ok:
        return False, {"why": why}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".part.mp4"
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    w, h = int(width), int(height)
    vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
          "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1"
          % (w, h, w, h))
    cmd = [exe, "-hide_banner", "-v", "error", "-y",
           "-loop", "1", "-i", path,
           "-t", "%.6f" % (frames / float(fps or FPS)),
           "-r", str(int(fps or FPS)), "-vf", vf,
           "-frames:v", str(frames),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(int(crf)),
           "-pix_fmt", "yuv420p", "-an", tmp]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           errors="ignore", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, {"why": "图片转码超时（%d 秒）" % timeout}
    if p.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 4096:
        return False, {"why": "ffmpeg 转码失败：%s" % ((p.stderr or "")[-300:] or "未知")}
    os.replace(tmp, out_path)
    return True, {"cached": False, "out": out_path, "frames": frames,
                  "fps": int(fps or FPS), "bytes": os.path.getsize(out_path)}


_SAFE_ID = re.compile(r"[0-9A-Za-z_-]{1,64}\Z")

# 本次送入里 bake() 真正摸到过的产物（命中缓存也算）。
#
# 为什么需要它：缓存文件名带「速度 + 源区间」，同一段改一次倍速就换一个文件名，
# 旧的不会有人删 —— gc 是按**草稿目录**整体回收的，而正在用的那份草稿整个目录
# 受保护（写回还要读它的快照）。于是一分钟 1.2 GB 的 ProRes 会在
# `retime/<草稿ID>/` 里越堆越多，且永远收不到。攒十版就是十几个 GB。
#
# 送入成功之后按这个集合反删：时间线上摆的就是这里面的文件，其余都是上一版
# 参数留下的，删了不影响本次结果，下次要用时再编一遍即可。
_USED = set()


def reset_used():
    """每次送入开始前清空（bake_plan 会调）。"""
    _USED.clear()


def used_paths():
    return set(_USED)


def prune_unused(draft_id, keep=None):
    """删掉这份草稿目录里本次没用到的中转文件，返回 (files, bytes)。

    只在送入**成功**之后调用 —— 失败时时间线可能停在半路，保留现场比省空间
    重要。keep 不传就用本次送入实际用过的那些路径。
    """
    keep = _USED if keep is None else keep
    keep = {os.path.normcase(os.path.abspath(p)) for p in keep}
    # 和 clean() 同一条白名单：ID 直接拼进路径，`..` 会指到缓存根以外
    sid = str(draft_id)
    if not _SAFE_ID.match(sid) or sid in (".", ".."):
        return 0, 0
    root = paths.retime_dir(draft_id)
    if not keep or not os.path.isdir(root):
        return 0, 0
    files, freed = 0, 0
    for n in os.listdir(root):
        if not n.lower().endswith(".mov"):
            continue
        p = os.path.join(root, n)
        if os.path.normcase(os.path.abspath(p)) in keep:
            continue
        try:
            sz = os.path.getsize(p)
            os.remove(p)
            files += 1
            freed += sz
        except OSError:
            pass
    return files, freed


def clean(draft_id=None, root=None):
    r"""删掉预渲染缓存。draft_id 为空就清整个 retime 目录。

    root 可以换成别的池（比如 photos/）：图片转码和变速预渲染是同性质的
    可再生产物，「清缓存」不该只清一半。

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
    if root is None:
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


def bake_plan(plan, draft_id, progress=None, size=None):
    """给计划里 kind=video 且需要变速的段补上 bake 素材路径。

    返回 (baked, failed)。baked 里的 c["path"] 已换成新素材，c["src_us"]=0。
    progress(i, n, c)：每段开工前回调一次。单段 ProRes 1080p 约 1~3 秒，
    二十段就是半分钟没动静 —— 面板要靠这句话显示「正在预渲染 7/20」。

    size=(w, h)：时间线画布，预渲染按它缩（见 video_filter 的实测数据）。
    """
    import time as _time
    reset_used()                 # 本次送入的「在用清单」从这里重新记
    baked, failed = [], []
    todo = [c for c in plan if c.get("bake")]
    for i, c in enumerate(todo, 1):
        if progress:
            try:
                progress(i, len(todo), c)
            except Exception:
                pass
        out = cache_path(draft_id, c["segment_id"], c["speed"],
                         c["src_us"], c["place"], c.get("range_us"), size)
        t0 = _time.time()
        ok, d = bake(c["path"], c["src_us"], c.get("range_us"), c["speed"],
                     c["place"], out, size=size)
        spent = 0.0 if d.get("cached") else _time.time() - t0
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
                      "audio": d.get("audio"), "enc": d.get("enc", ""),
                      "bytes": d.get("bytes"),
                      "size": list(size) if size else None,
                      "sec": round(spent, 1)})
    return baked, failed
