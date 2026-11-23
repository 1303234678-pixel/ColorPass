# ColorPass · macOS 移植与真机测试交接单

> 这份文档是**给 Mac 上那个 AI 的完整交接**。它没有我们之前的对话上下文，所以下面
> 把所有前提、已知结论、红线都写全了。请直接从「一、你现在要做什么」开始读。

---

## 一、你现在要做什么

把一个已经在 **Windows 上跑通并发布到 0.6.3** 的项目（ColorPass）移植/验证到 **macOS**。

代码已经在你手上（桌面上的 `ColorPass-0.6.3-src.zip`，解压即用，不进 git 也能跑）。
你需要做的是**按顺序验证 + 修 macOS 特有的缺口**，并把结果整理成一份报告回传。

不要在没跑「阶段 0」之前就动手改代码。这个项目在 Windows 上踩过的坑都是靠真机
日志定位的，凭猜测改只会把已经对齐好的帧位搞坏。

---

## 二、项目是什么（30 秒版）

剪映 → 达芬奇 → 剪映 的往返工具。用户在一个「已有剪辑」的剪映草稿上调色：

1. **送入**：读剪映草稿（Windows 上是加密的，从剪映进程内存里取明文），在达芬奇里
   按**绝对帧位**重新摆一条时间线（段数、时长、帧号必须与剪映逐帧一致）。
2. **调色**：用户在达芬奇里调色。
3. **写回**：从达芬奇导出 33³ LUT，写进一份**新草稿**（不覆盖原草稿），
   LUT 复制到新草稿自己的 `Resources/ColorPass/` 下。

核心约束（**不要用近似方案绕过**）：

- 达芬奇脚本 API **没有任何设置片段速度的入口**，所以变速段必须先用 ffmpeg 预渲染成
  等长新素材（ProRes 422 10bit），这是唯一能让「变速」真的出现在达芬奇里的办法。
- 帧位对齐是靠 `solve_exact()` 反解源帧数做到的，任何「四舍五入一下」都会让整条
  时间线错位。测试里有专门的用例。

代码结构（解压后）：

```
colorpass/            主包
  cli.py              命令行入口（send / write / pull / doctor / gc ...）
  draftio.py          读剪映草稿（内存 → 明文缓存 → 载入备份，三级降级）
  memscan.py          从剪映进程内存取明文 —— **仅 Windows**
  send.py             送入：建计划 + 预渲染 + 导素材 + 落轨 + 打标记
  retime.py           变速段 ffmpeg 预渲染（含缓存回收）
  write.py            写回：从快照生成新草稿 + 嵌入 LUT
  pull.py             取回：从达芬奇导 LUT
  resolve.py          达芬奇脚本连接（mac 模块路径已写）
  install.py          把面板复制进 Fusion 目录（含 mac 三个目标目录）
  panel/ColorPass.py  达芬奇里的面板 UI（Fusion 脚本面板）
tests/                525 条离线单测（Windows 基线）
tools/mac_probe.sh    ← 先跑这个（阶段 0）
docs/性能.md           预渲染的实测数据与磁盘占用说明
docs/设计规范.md       面板 UI 的硬规则（改 UI 前必读）
```

---

## 三、红线（违反任何一条都会出问题）

1. **不要改用户的真实剪映草稿**。送入是只读；写回必须生成**新草稿**。测试期间用
   一份复制出来的草稿，别拿人家正在做的片子试。
2. **不要 `git push`、不要发 Release**。Windows 那边没推，节奏由人决定。
3. **不要为了「跑通」而静默降级**。这个项目的原则是：能力缺失必须报出来（`compat`
   里分级：致命 / 可降级），不许悄悄跳过然后说成功。
4. **不要重做 UI 版面**。面板刚在 Windows 上定过版（shadcn ghost 风格、页脚裸字、
   自折行），`docs/设计规范.md` 里写了为什么。mac 上只验证「能不能加载、布局对不对」。
5. **测试数据要隔离，但别乱设环境变量**：手动跑 `cli send/write` 时用
   `export COLORPASS_DATA="$HOME/cp-test"`，别污染 `~/Library/Application Support/ColorPass`
   里的真实数据；**跑 pytest 时不要设**（见阶段 1 的说明，会假失败）。
6. **动达芬奇前先确认工程**：不要删用户的时间线，`send` 只会清理自己建的 `CP` 前缀
   时间线，不要绕过这个保护。

---

## 四、阶段 0：环境预检（必做，先回传结果）

```bash
cd ~/Desktop && unzip ColorPass-0.6.3-src.zip -d ColorPass-src && cd ColorPass-src
bash tools/mac_probe.sh > ~/colorpass-mac.txt 2>&1
open ~/colorpass-mac.txt
```

`mac_probe.sh` 只读。它会输出：系统与架构、python3、ffmpeg、达芬奇版本与脚本模块
位置、剪映 Mac 版草稿目录、以及**最新一份 `draft_content.json` 的前 80 字节是明文
JSON 还是 base64 密文**。

⚠️ 最后这一条是整个移植的**关键判断依据**，请务必把那一行原文贴进报告。

补充要确认的三件事：

| 项 | 怎么确认 | 为什么重要 |
|---|---|---|
| 达芬奇「外部脚本」开关 | 偏好设置 → 系统 → 常规 → `External scripting using` 设为 **Local** | 默认可能是 None，那样脚本 API 连不上，`doctor` 会显示 `loaded:false` |
| 剪映实际草稿根目录 | 和代码里写死的 `~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft` 对一下 | 路径不对，后面全白做（改 `probe.py` 的 `user_data_dir()`） |
| 有没有 Homebrew / Xcode CLT | `brew -v`、`xcode-select -p` | 决定 ffmpeg 和 python3 怎么装 |

---

## 五、阶段 1：离线测试（不碰达芬奇、不碰剪映）

```bash
cd ~/Desktop/ColorPass-src
python3 -m pip install --user pytest ruff pillow imageio-ffmpeg
python3 -m pytest tests -q
python3 -m ruff check colorpass tests tools
python3 -m colorpass.cli doctor
```

> ⚠️ **跑测试不要设 `COLORPASS_DATA`**。有一条断言依赖「默认数据目录叫 ColorPass」
> （`test_installs_where_postinstall_expects`），一改环境变量就假失败。测试自己
> 会把临时目录隔离开，不会污染真实数据。`COLORPASS_DATA` 只在阶段 3 手动跑
> `cli send/write` 时按需使用。

**Windows 基线**（拿这个当尺子）：

| 项 | Windows 结果 |
|---|---|
| `pytest tests -q` | **526 passed, 1 skipped** |
| `ruff check` | All checks passed |
| `doctor` | `resolve.loaded=true`、`app=true`、`colorpass=0.6.3` |
| 预渲染实测 | 63 秒 4K 变速段 → 34.1 s / 1.39 GB / 1920×1080 / prores_aw |

**Mac 上这些「跳过」是正常的，不是失败**：

- `TestInnoScript` 整类 —— Inno Setup 只产出 Windows 安装包，已按平台跳过。
- `TestFfmpegResolve` 里断言「随包那份优先」的 3 条 —— 仓库 `vendor/ffmpeg/`
  只有 Windows 的 `ffmpeg.exe`，zip 里刻意没带（省 105 MB）。
- 没装 ffmpeg 时 `retime` 的 7 条真编码用例也会跳过 —— **这 7 条必须跑到**，
  装完 ffmpeg 重跑确认它们真的执行了，否则等于没测转码路径。

Mac 上出现失败是**预期之内**的。请按这三类分流，不要一律当 bug 修：

- **A 类｜平台差异**：Windows 才有的断言（`tasklist`、注册表、`%LOCALAPPDATA%`、
  `font-weight` 之类）。修法是给测试加平台条件跳过，**不是**改产品代码。
- **B 类｜产品真 bug**：mac 上逻辑就走错了。必须修，并补一条测试。
- **C 类｜环境缺件**：没装 ffmpeg / 没装 pillow / 没连达芬奇。装完重跑即可。

预计最先炸的几处（我提前审过，按此顺序看）：

1. `tests/test_doctor.py` 里针对 Windows 进程/路径的用例。
2. `retime.available()`：`bundled()` 找的是 `vendor/ffmpeg/ffmpeg.exe`，mac 上没有 →
   应回落到 PATH 上的 `ffmpeg`。**确认它确实回落了**，否则 7 条预渲染用例会全 skip，
   等于没测真转码路径。
3. 编码：Windows 用 `gbk` 解 `tasklist` 输出，mac 走 `ps`（`probe.py` 里有分支），
   确认没有漏掉的 `decode("gbk")`。

**已经在 Windows 侧预先处理掉的**（不用你再查一遍）：`TestInnoScript` 与 3 条
「随包 ffmpeg 优先」的断言已加平台/存在性跳过；`retime.missing_reason()` 在非
Windows 上不再说「重新运行一次安装包」，改成教 `brew install ffmpeg`（并加了
`test_missing_reason_says_how_to_install` 覆盖它）。

---

## 六、阶段 2：连达芬奇（只读）

达芬奇先打开（随便一个工程），然后：

```bash
python3 -m colorpass.cli doctor
python3 -m colorpass.cli compat
```

期望：`resolve.loaded=true`、`app=true`，`compat.features` 里 `media_import` /
`empty_timeline` / `append` / `delete_timeline` 都是 `true`。

如果 `loaded=false`：先查「外部脚本 = Local」，再查脚本模块路径
`/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules`
是否存在（`resolve.py` 里已经列了这个路径）。

---

## 七、阶段 3：真机送入（最关键的一步）

**前置**：剪映 Mac 版打开那份测试草稿（复制一份，别用原片），并让它在最前面。

```bash
export COLORPASS_DATA="$HOME/cp-test"
python3 -m colorpass.cli status      # 应认出草稿名
python3 -m colorpass.cli plan        # 只算落轨计划，不碰达芬奇
python3 -m colorpass.cli send        # 真送
```

**这里会遇到整个移植最大的未知**：Windows 上读加密草稿首选「扫剪映进程内存」
（`memscan.py`，纯 Win32 API）。mac 上没有这条路，`draftio.py` 会退到
「上次读过的明文缓存」和「剪映的载入备份 `.backup/*.load.bak`」。

所以请先回答一个问题，再决定怎么做：

> 那份测试草稿，在**从没被 ColorPass 读过**的情况下，`send` 能不能读到内容？

- **能** → 说明 mac 剪映的落盘/备份是明文，皆大欢喜，记为「mac 走备份直读」。
- **不能** → 这是**阻塞项**，不要硬写内存扫描（需要 `task_for_pid` 权限，SIP 下基本
  是死路）。可行方向按优先级：
  1. 找剪映 Mac 版的明文落盘点（缓存目录、`Timelines/` 子目录、代理清单）；
  2. 用 `.load.bak`（打开草稿时生成的那份）；
  3. 让用户在剪映里「另存为/导出草稿」触发落盘；
  4. 退一步：做「手动导出草稿 json → 指定文件路径」的半自动入口。

无论哪条，**结论和证据（文件路径 + 前 80 字节 hex）都要写进报告**。

**送入成功的判据**（照 `send.json` 看，不要凭感觉）：

- `ok: true`
- `placed` 条数 == `plan` 段数
- 每段 `exact`（帧位严格对齐，没有 `tail1`/`approx`）
- `timeline_end` 与预期一致
- `timings` 里 `bake / media / place / finish` 各多少秒

日志与报告位置：`~/cp-test/runs/<草稿ID>/send.json`、`plan.json`、`snap-*/`。

---

## 八、阶段 4：写回 + 取回

```bash
python3 -m colorpass.cli pull        # 从达芬奇导 LUT（需在调色页）
python3 -m colorpass.cli write       # 生成新草稿
```

验收：新草稿能在剪映 Mac 版打开、能播放，`Resources/ColorPass/` 下有 cube，
LUT 应用后画面有变化。**不要覆盖原草稿。**

---

## 九、阶段 5：面板能不能加载

```bash
# 源码模式：要告诉面板「用哪个解释器、代码在哪」，它起子进程时才知道怎么调
python3 -m colorpass.install --python "$(command -v python3)" --product "$PWD"
# 卸载面板注册：python3 -m colorpass.install --remove
```

mac 的目标目录（`install.py` 已写）：

- `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Support/Workflow Integration Plugins`
- `~/Library/Application Support/.../Support/Workflow Integration Plugins`
- `~/Library/Application Support/.../Fusion/Scripts/Utility`

重启达芬奇，看 `工作区 → 工作流集成`（或 Fusion 的 Scripts 菜单）里有没有 ColorPass。
面板 UI 是 Fusion 脚本 + Qt，重点看：能不能起来、有没有报错、字体/间距对不对
（那台 Mac 的缩放比 Windows 干净，理论上会更好看）。

起不来就把 Fusion 的 Console 报错原文贴进报告。

---

## 十、已知的 macOS 缺口清单（我提前审过，按危害排序）

| # | 位置 | 问题 | 建议做法 |
|---|---|---|---|
| 1 | `memscan.py` / `draftio.py` | 读草稿首选的「进程内存」只有 Windows 实现 | 见阶段 3 的四个方向，**先取证再定** |
| 2 | `draftio.py` / `send.py`（素材路径比对） | macOS 文件名是 **NFD**，剪映 json 里多为 NFC，`normcase` 在 posix 上也不折叠大小写 → 素材可能整片「找不到」 | 比对前统一 `unicodedata.normalize("NFC", p).lower()`，补一条测试 |
| 3 | `update.py` | 只认 `.exe` 资产，mac 上不可用 | 面板上把「检查更新」隐藏，或改成提示去 GitHub 手动下载；别让它报错 |
| 4 | `retime.bundled()` | 只找 `vendor/ffmpeg/ffmpeg.exe` | 确认回落 PATH；如需自带，放 `vendor/ffmpeg/ffmpeg`（arm64，不要 x86） |
| 5 | 打包 | Inno Setup 只能打 Windows | mac 暂不打包，跑源码；以后要发版用 PyInstaller onedir + 签名公证 |
| 6 | `postinstall.py` | 注册的 cli 指向 `.exe` | mac 上应注册成 `python3 -m colorpass.cli` |
| 7 | 面板 | 依赖 Fusion 进程里的 Qt | 确认达芬奇自带的 PySide 可用，不需要 pip 装 PySide6 |

---

## 十一、回传格式（重要）

最后请写一份 `~/Desktop/MAC测试报告.md`，用这个结构，方便直接贴回 Windows 侧：

```markdown
## 1. 环境
- macOS / 架构 / python3 / ffmpeg / 达芬奇版本 / 剪映版本
- 剪映草稿真实根目录：
- 最新草稿 draft_content.json 前 80 字节（hex）：

## 2. 离线测试
- pytest: __ passed, __ failed, __ skipped
- ruff:
- 失败用例清单（用例名 → A/B/C 分类 → 原因一行）

## 3. 达芬奇连接
- doctor 关键字段：
- compat 里为 false 的能力：

## 4. 真机送入
- 草稿名 / 段数 / 是否全是 exact
- send.json 的 timings：
- 是否读到草稿明文（来源是「内存/缓存/备份/读不到」）：

## 5. 写回与面板
- 写回是否成功、新草稿能否打开
- 面板是否加载、报错原文

## 6. 你改了哪些文件（逐条，含原因）
```

**另外**：如果某一步你判断「不改代码就过不去」，先把**现象 + 日志 + 你的方案**
写进报告再动手，别默默改完只说「好了」。Windows 那边需要知道每一处改动的理由。
