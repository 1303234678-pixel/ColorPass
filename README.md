<p align="center">
  <img src="assets/icon.png" width="120" alt="ColorPass">
</p>

# ColorPass

[![CI](https://github.com/1303234678-pixel/ColorPass/actions/workflows/ci.yml/badge.svg)](https://github.com/1303234678-pixel/ColorPass/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/1303234678-pixel/ColorPass?label=release)](https://github.com/1303234678-pixel/ColorPass/releases/latest)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab.svg)](pyproject.toml)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-lightgrey.svg)](#安装)

剪映剪辑 · 达芬奇调色 · 回剪映继续剪

ColorPass 是达芬奇的 Workflow Integration 插件，用于在剪映专业版与达芬奇之间做**调色往返**：
读取当前打开的剪映草稿，按原语义重建到达芬奇时间线；逐段导出 LUT；再把调色写回一份
**仍可继续编辑**的剪映草稿。全过程不生成中间工程文件，不渲染成片。

| 能力 | 实现 |
|---|---|
| 读草稿 | 解密正文的三级来源（本地缓存 / 载入备份 / 进程内存），语义层解析轨道与素材 |
| 落轨 | 绝对帧号 `AppendToTimeline`，逐段 marker + custom data 作为身份锚 |
| 回写 | 深拷贝底稿快照，仅追加一条 LUT 效果材质；落盘前做结构断言 |

---

## 工作流

```
剪映（打开草稿）
   │  送入达芬奇
   ▼
达芬奇时间线（13 段 / 原时长 / 原位置 / 停用段保留）
   │  逐段调色
   │  写回剪映
   ▼
新草稿（原剪辑结构 + 每段一条 LUT，可继续剪、可再调）
```

面板是**两个步骤**，不是两个并排的按钮：① 读取草稿 → ② 命名并写回。
第 2 步读的是送入那一刻的快照，所以没送入过时，整块（序号、输入框、按钮）都是
灰的 —— 「为什么点不了」不用看说明。顶部一颗状态胶囊（就绪 / 已送入 / 待重开 /
未运行），底部一行结果，没结果时显示「写回不会覆盖原草稿」。
锁定轨道、清理缓存不在面板上（命令行 `colorpass lock` / `gc`），当前草稿由面板
每 10 秒自己核对一次。所有能力同时提供命令行入口，`check` / `plan` / `status` /
`compat` 不依赖达芬奇。

## 实现要点

### 1. 草稿正文：三级来源

剪映 11.7 起 `draft_content.json` 为密文，密钥在进程启动时注入、不落盘。ColorPass 按可信度
依次尝试三条来源，前两条是秒级文件操作：

| 顺序 | 来源 | 成立条件 | 校验 |
|---|---|---|---|
| 1 | `%LOCALAPPDATA%\ColorPass\plain\<id>.json` | 以前成功读过这份草稿 | 磁盘指纹（密文载荷字节数 + 最后编辑时间）完全一致 |
| 2 | `<草稿>/.backup/*.load.bak` | 本工具写回过的草稿（明文写入，备份亦为明文） | id 匹配，且编辑时间不晚于备份 |
| 3 | 进程内存扫描 | 明文尚未被剪映回收 | 体积对账，容差 320 B + 0.5% |

内存明文是有生命周期的：一份打开一小时、全程未编辑的草稿，进程里可能只剩 167 KB 残片
（有 `"tracks":[`、没有 `"materials"`、括号不闭合）。因此第 1 条缓存是长期可用的主路径，
实测同一操作从 25 s 失败降为 0.78 s 命中。

### 2. 放置：草稿语义 → 时间线

XML / AAF 无法表达「2 倍速」「该段已停用」这类属性，按原速摆放会让后续每一段累积错位。
ColorPass 在草稿语义层消化这些属性，再用绝对帧号落轨：

| 草稿语义 | 处理方式 |
|---|---|
| 变速 | ffmpeg 预渲染为等长 ProRes，帧序与剪映一致（21.2 虽有 `SetSpeed`，但预渲染不依赖调色页状态，行为更稳定） |
| 转场 | 后段起点收前段时长，位置不偏；近似处理，非原生转场 |
| 停用片段 `visible:false` | 照样占位，落轨后 `SetClipEnabled(False)` |
| 复合片段 | 递归展开 |
| 帧率不一致 | 绝对帧号 + 共用帧率换算，无累积误差 |

停用片段必须占位：实测一份 12.267 s 的草稿，其中有 5.5 s 由一条停用段撑起。跳过不送会在
时间线上留黑场，并使后续所有位置核对失效。

送入范围是视频轨（含复合片段）、**图片段**和**音频轨**。图片段会先转成等长视频再摆
（静帧在 21.2 上摆出来的时长恒为 5 秒，不看 `endFrame`）；音频只取声音（`mediaType=2`），
进达芬奇的 A 轨，视频自带的原声不会重复送。文字、贴纸、滤镜与独立特效轨不进入达芬奇，
写回时在原草稿中保持原样。

### 3. 回写：快照克隆 + 语义断言

```
达芬奇调色
  → 每段打 marker，custom data 写入 segment_id
  → 逐段 ExportLUT（33³ cube，每段 35,937 个采样点）
  → 取送入时保存的底稿快照，深拷贝（非重建）
  → 仅追加一条 LUT 效果材质，时间线结构不动
  → 断言全过才落盘
```

断言比较的是语义指纹而非 id：轨道类型、段顺序、入出点、素材引用、变速倍率、关键帧，
以及转场 / 蒙版 / 变速 / 淡入淡出 / 复合片段引用，覆盖全部轨道类型。任一项不一致即判定
写回失败并保留原草稿——宁可报错，也不产出一份结构已损的草稿。

结构之外还单独核一遍**基础信息**：画布分辨率、总时长（微秒与帧）、每段的起止帧、
素材像素尺寸。剪映草稿以微秒存时间、落在 1/30 秒的栅格上（实测 18 段的时长全是
33,333 µs 的整数倍），所以「帧数」是算出来的确定值，不是估的。这三处一起对账：

```
剪映原稿（送入时的快照）  675 帧 / 1920×1080 / 18 段
      ↓ 送入
达芬奇时间线（plan.json） 14 段落在 0…674 帧
      ↓ 写回
新草稿（剪映）            675 帧 / 1920×1080 / 18 段   ← 逐段帧号必须与上两处一致
```

任何一处对不上就判写回失败（`basics_problems` / `frame_check.mismatched`），
面板上直接显示「帧数：675 帧 → 675 帧｜画布 1920x1080｜18 段逐帧一致」。

---

## 安装

### 方式一：安装包（推荐给测试者）

下载 `ColorPass-Setup-<版本>.exe`，双击走安装向导即可（Inno Setup 编译，
有欢迎页 / 进度条 / 完成页，全程不需要管理员权限）。安装程序会把运行时复制到
`%LOCALAPPDATA%\ColorPass\bin`，把面板放进达芬奇的插件目录，并写好配置。

**不需要装 Python。** 装完重启达芬奇，菜单 `工作区 → Workflow Integrations → ColorPass`。

- 卸载：`设置 → 应用 → ColorPass`（或开始菜单里的「卸载 ColorPass」）——
  只删程序与面板，草稿缓存与历次报告保留
- 升级：直接双击新版安装包覆盖安装；面板里点「检查更新」也是走同一套静默参数
- 装完发现菜单里没有：确认达芬奇是 21.2；插件目录在
  `C://ProgramData//Blackmagic Design\DaVinci Resolve\Support\Workflow Integration Plugins`
- Windows SmartScreen 可能提示「未知发布者」—— 因为没买代码签名证书，点「更多信息 → 仍要运行」。
  介意的话用方式二自己从源码装。

### 方式二：从源码

```bash
git clone https://github.com/1303234678-pixel/ColorPass.git
cd ColorPass
python -m pip install -e .
python -m colorpass.install      # 安装面板，并记录源码目录与解释器路径
```

前两种方式写的是同一份 `config.json`：源码模式记 `product` 与 `python`，安装包模式记 `cli`。
面板优先用 `cli`（不需要 Python），没有才回退到 `python -m colorpass.cli`。
重新运行任一方式的安装命令即可切换，互不冲突。macOS 的一键脚本走的就是源码模式那条路
（内部调 `python3 -m colorpass.install`），所以也写同一份配置。

### 方式三：macOS（双击脚本）

把源码放到 Mac 上（`git clone`，或下载 Release 里的 `ColorPass-<版本>-mac.zip` 解压），
然后双击根目录的 **`安装ColorPass.command`**：

```bash
open 安装ColorPass.command          # Finder 里双击也是同一个结果
```

脚本自己走完五步：向上定位源码根（不依赖当前目录）→ 检查 Python / 达芬奇 / 剪映 →
编译 `reader.dylib`（`clang++`，universal arm64 + x86_64，ad-hoc 签名）→ 把面板装进达芬奇 →
写 `config.json` → 自检。

- **没有 dmg / pkg，也不需要 Apple 开发者账号。** `.app` 要代码签名 + 公证才能过 Gatekeeper，
  `.command` 是纯脚本、不受 app 拦截，双击就能跑；这是刻意的取舍，不是没做完。
- **首次「送入」会让剪映自动保存并重启一次（约 10 秒）。** 读取器必须跟剪映进程一起加载
  （`DYLD_INSERT_LIBRARIES`），已经跑起来的实例注入不进去。之后剪映照常用，再点「送入」
  立刻开始。同理，你自己退出剪映再打开，读取器也没了，下次送入会再问一次 —— 这是形态
  决定的，不是 bug。
- **不会在剪映里留下任何东西。** 读取器只响应一个「解密这个文件」的本地 socket 请求，
  密钥不出剪映进程，也不改剪映的任何文件。
- 菜单路径：`工作区 → Workflow Integration Plugins → ColorPass`；如果只装进了 Fusion
  目录（前两个目录不存在时），则是 `Fusion → Scripts → Utility → ColorPass`。
- 卸载：`python3 -m colorpass.install --remove`（删面板 + 配置，不动草稿与缓存）。
- **macOS 暂时没有自动更新**（0.7.0 起每个 Release 都会挂 `ColorPass-<版本>-mac.zip`，
  但客户端只实现了「查」，没实现「装」），面板会明说，请到
  [Releases](https://github.com/1303234678-pixel/ColorPass/releases) 手动下载新版 zip，
  解压后重新双击安装脚本即可覆盖。


可选依赖：

```bash
python -m pip install -e ".[cover]"     # 草稿封面缩略图（Pillow）
python -m pip install -e ".[ffmpeg]"    # 源码模式跑预渲染（安装包已经自带）
```

### 关于 ffmpeg

**安装包自带 ffmpeg，用户不用装，也不用配 PATH。** 它只干两件事，而这两件事都没有
替代方案：图片段必须转成等长视频（达芬奇摆静帧恒为 150 帧且改不了），变速段必须
预渲染成等长素材（脚本接口没有设速度的入口）。少一个，图片和慢放就送不进去。

代价是体积：运行时 +105 MB（安装器 20 → 57 MB，zlib 压过）。0.4.0 曾把它做成
「有就用、没有就降级」，结果是两种机器表现不一样、用户看到一句谁也解决不了的提示，
所以 0.4.2 起改成内置。

源码模式需要自己备一份（构建时也会自动补）：

```bash
python tools/fetch_ffmpeg.py            # 收进 vendor/ffmpeg/（不进 Git，见该目录 README）
```

macOS 上二选一（`安装ColorPass.command` 会替你检查，缺了会说装哪个）：

```bash
brew install ffmpeg                     # 有 Homebrew 的话
python3 -m pip install imageio-ffmpeg   # 没 Homebrew：pip 包自带一份可执行文件
```

查找顺序：`COLORPASS_FFMPEG` → 随包自带（`bin/_internal/ffmpeg/ffmpeg.exe`）→ PATH → `imageio_ffmpeg`。
自带的那份在构建时校验过编码器（`prores_ks`、`libx264`）与滤镜（`atempo`、`setpts`）。
`--no-ffmpeg` 可以打瘦包，但那个包不能发出去 —— 图片和慢放会直接不可用。

## 命令行

面板是薄壳，全部能力均可单独调用：

```bash
colorpass check                      # 读取当前打开的草稿，输出结构报告（JSON）
colorpass plan                       # 只计算落轨计划（dry-run，不需要达芬奇）
colorpass status                     # 面板使用的状态汇总
colorpass send                       # 草稿 → 达芬奇时间线
colorpass pull                       # 达芬奇调色 → 逐段 33³ LUT
colorpass write                      # LUT → 写回新草稿
colorpass roundtrip                  # send + pull + write
colorpass gc --apply                 # 回收可再生缓存（默认只报告）
colorpass compat                     # 运行环境自检：接口是否齐全、缺了影响什么
colorpass multicam                   # 多机位 / 合并片段自检（只读）
colorpass multicam --export          # 额外实测：对多机位片段导一次 LUT
colorpass doctor                     # 环境与兼容性报告（JSON，贴 issue 用）
colorpass mac-launch                 # macOS：带读取器启动剪映（首次送入前会自动做）
colorpass about                      # 联系作者：邮箱与收款码（面板页脚那个按钮）
colorpass update                     # 检查新版本（有新版会打印下载地址）
colorpass update --install           # 下载并静默安装最新版（面板「检查更新」走这里）
```

`plan` 输出的就是 `send` 内部实际使用的计划，包含重叠消解、预渲染标记与跳过原因，
可直接 diff 两次改动前后的落轨位置，便于在 CI 与批处理中回归。

## 模块地图

```
colorpass/
├── platforms.py   平台判定的单一入口（测试可用 OVERRIDE 强制走另一条链路）
├── probe.py       剪映工程识别（进程、窗口、草稿目录、锁状态）
├── procs.py       macOS 进程枚举（libproc / ctypes，不 fork 外部命令）
├── memscan.py     进程内存扫描（Windows ReadProcessMemory）
├── macdraft.py    macOS 读取链路：读取器协议、受控启动剪映、进程内解密
├── mac/           reader.dylib（注入剪映进程的解密服务，随包发布）
├── plain.py       明文三级来源仲裁
├── draftio.py     草稿语义层：轨道 / 变速 / 转场 / 蒙版 / 复合片段 / 新鲜度
├── send.py        草稿 → 达芬奇：落轨计划、变速、停用段、marker
├── pull.py        达芬奇 → LUT：marker 身份锚 + ExportLUT + 覆盖段兜底
├── multicam.py    多机位 / 合并片段自检：识别、计划匹配、导出实测
├── compat.py      21.2 的接口探测与兜底：alive / 常量值 / 页面轮询 / 导入与 append 的效果校验
├── doctor.py      环境与兼容性报告：平台 / 版本 / API 缺项（只读）
├── draftbuild.py  克隆草稿并追加 LUT 材质（macOS 走 install_mac_from_source）
├── write.py       写回编排 + 结构断言（structure_diff）
├── gc.py          缓存回收
├── update.py      自更新：查清单 / 下载 / 校验 / 交给安装器（macOS 明确不支持）
├── about.py       联系作者窗口（HTML + 浏览器 --app，Fusion 画不了图片）
├── preflight.py   四道闸：剪映在跑 / 草稿已开 / 草稿未改 / 达芬奇可用
└── panel/         达芬奇面板（Fusion 进程内，仅通过子进程调用 CLI）
```

分层规则见 [CONTRIBUTING.md](CONTRIBUTING.md)：`L0 环境 → L1 草稿语义 → L2 动作编排 → L3 壳`，
依赖单向，读草稿的模块不得依赖动作编排。

## 运行环境

| 组件 | 状态 | 备注 |
|---|---|---|
| Windows 10 / 11 | 实测 | 完整链路；依赖 `%LOCALAPPDATA%` 与 `ReadProcessMemory` |
| macOS（剪映 11.7+） | 实测全链路 | 送入 / 写回跑通（2026-11-23 真机：8 段 / 2240 帧精确落位，写回的新草稿在剪映里能打开、调色生效、复合片段完整）。草稿正文必须由**注入在剪映进程内的读取器**解密 —— 密钥只在剪映进程里有效，外部解不开也读不到内存。首次点「送入」会让剪映自动保存并重启一次（约 10 秒），之后剪映照常使用。写回**不加密**：mac 的草稿是「按目录」的（`draft_info.json` + `Timelines/<id>/` + `Resources/` + `subdraft/`），工具整目录克隆源草稿、正文写成明文，剪映自己会读明文并在保存时重新加密（`EncryptUtils::maybeEncrypted` 就是它先判断再解密）；`EncryptUtils::encrypt` 在 11.8 上一调即崩，不能用 |
| 剪映专业版 11.7 / 11.9 | 实测 | 正文加密；字段行为见 [docs/剪映草稿字段.md](docs/剪映草稿字段.md) |
| 达芬奇 **21.2** | 实测全链路 + 脚本建多机位 | 21.2.0.6 Studio：送入 / 取回 / 写回跑通；`CreateMulticamClip` 真机建出多机位并导出 LUT（`tools/verify_multicam.py`） |
| 达芬奇 21.1 及更早 | **不支持** | 缺多机位脚本接口与真变速；送入/取回/写回直接报「请升级到 21.2」，不做降级 |
| Python | 3.9+（开发与 CI 用 3.12） | 无第三方硬依赖 |

### 为什么只做 21.2

0.2 为了「18–21 都能用」，把每条路径都写了降级分支：没有多机位接口就换复合片段、
没有真变速就靠预渲染、读不到片段类型就按名字猜。代价是**代码里一半的分支在目标
机器上永远走不到**，而每次排错都要先判断「这台机器正在走哪条路」。

21.2 是当前版本里唯一同时具备「多机位脚本接口 + 真变速 + 片段类型」的版本。
目标收窄之后，接口不用再猜、多机位能真建、一屏能装下完整流程。代价是不再向下
兼容 —— 这是有意的：低版本会明确说「请升级」，而不是悄悄少做一步。

### 21.2 仍然要小心的七件事

1. **`hasattr` 会撒谎。** 21.2 的绑定对不存在的方法也返回 True（`getattr` 是
   None），一调就 `TypeError: 'NoneType' object is not callable`。能力判断统一走
   `compat.alive()` —— 取出来，看它能不能调。
2. **常量的值不一定和文档一样。** 官方写 `resolve.EXPORT_LUT_33PTCUBE`，但绑定里
   取不到；传字符串 `"33PTCube"` 会静默返回 False（连文件都不建），真正认的是整数
   枚举 `2`。`compat.lut_type()` 负责给正确值。
3. **方法会换宿主。** `DeleteTimelines` 在 21.2 位于 `MediaPool`，`Project` 上只留
   一个空壳；`AppendToTimeline` 也在 `MediaPool` 上，不在 `Timeline` 上 —— 写死一边
   就会误报「这版不支持」。
4. **「没报错」不等于「真的做了」。** 这是最坑的一类：
   * `ImportMedia([{"FilePath": ...}])` 在 21.2.0.6 上**返回 None、一个片段都不导**，
     也不抛异常（文档推荐的正是这个形态）。所以 `compat.import_media()` 不认返回值，
     改看「媒体池里多了什么」，clipinfo 无效就退回裸路径列表。
   * `AppendToTimeline` 摆不上时返回 **`[None]`**（长度 1）。信长度会在下一行
     `got[0].GetStart()` 上炸 `AttributeError`，统一过 `compat.real_items()`。
5. **导出 LUT 要调色页真的就绪。** `OpenPage("color")` 返回 True 只代表请求被接受；
   页面没切过去时 `ExportLUT` 直接 False。`compat.ensure_page(wait=…)` 轮询到页面
   真变了才算数。**渲染队列在跑时也会假失败**，`pull` 会先问
   `IsRenderingInProgress()` 再动手。
6. **`AddMarker` 不覆盖。** 同一帧已有标记时它返回 False（旧的留着）。所以
   `send.mark_item()` 失败后会改用 `UpdateMarkerCustomData` 改那一枚，否则重送一次
   可能残留上一轮的段号。
7. **静帧的时长骗人。** 图片导进来是 `Type=Still`、`Frames=1`，
   `AppendToTimeline` 的 `endFrame` **被完全忽略**，摆上去恒为 150 帧（5 秒，达芬奇
   的默认静帧时长）；`SetProperty("Duration"/"ClipDuration"/"EndFrame")` 全返回
   False，摆完也改不了。所以图片段先由 `retime.photo_clip()` 转成等长视频再摆 ——
   剪映里 3 秒，达芬奇里就是 90 帧。顺带一条：音频要用 `mediaType: 2` 才会只进气
   不进画面（不传的话画面和声音会一起进去）。

### 这个版本能做、旧的代码没做的两件事

* **画面缩放**：`AppendToTimeline` 不收变换参数，但摆完之后
  `TimelineItem.SetProperty("ZoomX"/"ZoomY", …)` 在 21.2 上返回 True 且能读回。
  剪映里 1.49x 的段，现在达芬奇里也是 1.49x（只搬缩放：位移/旋转的单位没有实测
  依据，硬套会把构图改坏，报告里会列出来）。
* **工程身份**：`Project.GetUniqueId()` 让取色能确认「还是送入时那个工程」。只比
  时间线名字是不够的 —— 另存一份同名工程、或在别的库里建同名时间线，名字全对、
  内容是另一份。

### 缺一项会怎样

21.2 上这些接口都在。表里写的是「万一这台机器装坏了 / 被裁掉了」时的行为 ——
缺能力不该变成 traceback，要么降级并说明，要么明确停下：

| 缺什么 | 行为 |
|---|---|
| `ExportLUT` | 写回中止并说明原因（致命，不猜） |
| `ImportMedia` / `CreateEmptyTimeline` / `AppendToTimeline` | 送入中止，报告 `gate` 与原因 |
| `SetClipEnabled` | 停用段照常占位落轨，但会出画面；报告与面板都标注 |
| `GetCurrentPage` / `OpenPage` | 不切调色页，LUT 照样导，只是慢一些 |
| `CreateCompoundClip` | 复合片段拆成多轨落下，回剪映后不再是一个整体 |
| `CreateMulticamClip` | 报告里记为「少一种做法」，复合片段这条路仍可用 |

`colorpass compat` 会把实际缺项摊开；面板页脚「诊断」是同一份内容，可直接复制。

面板页脚左侧还有一个**联系作者**：邮箱与两张收款码（微信 / 支付宝），
点一下开一个小窗，不占面板地方。二维码图在 `colorpass/assets/qr_*.png`，
由 `tools/make_qr_assets.py` 从收款码截图里裁出来（只留二维码本体）。

## 更新

面板右下角有一个**检查更新**按钮（左边那行小字是当前版本）：点了才查，查到新版先弹窗
说清版本号 / 体积 / 改了什么，点「下载并安装」才下载、校验、静默安装。开面板不查、
也不定时查 —— 不点就永远不打扰。机制上有两个刻意的选择：

- **清单自己维护**（仓库根的 `version.json`），不查 GitHub API。Release 附件没挂的时候
  API 返回空 assets，判不出有没有新版；清单里的下载地址还可以随时换成任意镜像。
  读取顺序：`COLORPASS_MANIFEST` 环境变量 → `api.github.com`（先 contents 读
  `version.json`，没有再退 `releases/latest`）→ `raw.githubusercontent.com` →
  `cdn.jsdelivr.net`。把 API 放前面是实测的结果：raw 与 jsDelivr 在国内多数网络下
  被直接阻断（SSL EOF，0.1 秒就断），而 api.github.com 一般能通；后两个留给海外网络。
  全都不通就当作「不知道」，不打扰用户。
- **下载与安装跑在独立进程**。安装器要 taskkill 所有 `ColorPass.exe` 才能替换 bin 里被
  占用的 dll，所以干活的进程必然死在半路 —— 面板只读它写的 `update.json`，进程没了也能
  继续显示进度。

发版流程（三步都有脚本，不手抄数字）：

```bash
python tools/build_installer.py      # 构建，末尾自动生成 version.json（含 SHA256/体积）
git archive --format=zip --prefix=ColorPass-X.Y.Z/ vX.Y.Z -o dist/ColorPass-X.Y.Z-mac.zip
python tools/make_manifest.py --mac dist/ColorPass-X.Y.Z-mac.zip   # 补 assets.darwin
python tools/make_manifest.py --notes "· 修了…"   # 想改更新说明时重跑一次即可
git add -A && git commit && git push --tags
```

mac 包必须用 `git archive` 从**标签**打，不要用文件系统打包：它保证 `reader.dylib`
按二进制原样进包（走 `.gitattributes` 的 `binary` 规则），也让包内代码与 tag 严格一致。

然后再到 GitHub 建 Release，把**两个**附件都传上去：

- `dist/ColorPass-Setup-X.Y.Z.exe` —— Windows 安装包
- `dist/ColorPass-X.Y.Z-mac.zip` —— macOS 源码包（`git archive` 打的，含 `reader.dylib`）

清单里的下载地址就是指向这两个附件。**没挂附件的话，用户点更新会看到「下载地址上还没有
文件」**，所以这一步不能省。只挂 Windows 那个的话，mac 用户按 README 去 Releases 会找不到包。

### macOS 侧的更新（当前状态：不自动更新）

macOS 的发布产物是**源码 zip**（`ColorPass-<版本>-mac.zip`，含 `reader.dylib` 与
`安装ColorPass.command`），不是安装器，所以面板在 mac 上会直接说「暂不提供自动更新，
请到 Releases 手动下载」—— 而不是下下来一个装不上的 exe。

清单里已经预埋了位置，`make_manifest.py --mac <zip>` 会写 `assets.darwin`
（`url` / `sha256` / `size` / `format: zip` / `install`）。要做 mac 自动更新，实现侧只需：
下载 → 校验 `sha256` → 解到新目录 → 跑 `installer/mac/install.command` → 重启达芬奇。
清单格式不用再改。


### 多机位 / 合并片段

剪映侧没有多机位概念：画中画只是多轨叠加，送入后达芬奇里就是多条 V 轨。
需要特殊处理的是**在达芬奇里把几段合成了一个多机位（或复合）片段之后**：
原来的段号 marker 随旧片段一起消失，老逻辑会判「时间线被改过」并整条拒绝导出。

现在的规则：

- **完全覆盖才认**：一个片段必须从某段起点开始、到某段终点结束，中间无缝，
  覆盖的每一段都能在计划里对上；差一帧都不认。
- **单段且不像多机位 → 仍然拒绝**：那更可能是你新插入的片段，给它挂 LUT
  就是挂错画面。
- **一个多机位 = 一个调色**：覆盖的几段共用同一份 cube（每段仍有自己的文件，
  写回、manifest、报告一段一行对得上）。多机位内部的机位切换不会回传 ——
  剪映没有「角度」这个概念。
- **位置变了就拒绝**：做多机位时若改动了时长，写回会停下并说明，不猜。

写回前可以先自检，不必真写一遍去试错：

```bash
colorpass multicam            # 识别当前时间线的多机位 + 与最近一次送入计划匹配
colorpass multicam --export   # 再实测一次 ExportLUT（只写数据目录）
```

想在一台新机器上完整验一遍（建工程、摆段、合成覆盖、导 LUT、比对哈希）：

```bash
python tools/verify_multicam.py --media a.mp4 b.mp4 --data D:\cp-test
```

它会在 `ColorPass MC Test <时间>` 新工程里做，不动你已有的工程。

21.2 的 `MediaPool.CreateMulticamClip` 可用，但本工具**不自动建** —— 剪映里
叠加的画面不等于「多机位」，自动合并会改变画面。需要真多机位时在达芬奇里手动
建，本工具负责认出来并写回。

### 工程规模（能扛多大）

段数一多，慢的是达芬奇的脚本 API，不是这个插件。真机实测（21.2.0.6，本机 SSD，
`tools/stress_scale.py --resolve`，全程真建时间线、真落轨、真导 LUT、真克隆草稿）：

| 段数 | 素材 | 送入 | 取回 | 写回 | 每段送入 | 结果 |
|---|---|---|---|---|---|---|
| 40 | 3 个 | 5.7 s | 3.8 s | 5.9 s | 143 ms | 40/40 落位，帧号全等 |
| 120 | 3 个 | 11.4 s | 10.7 s | 15.8 s | 95 ms | 120/120 |
| 240 | 3 个 | 18.7 s | 19.7 s | 31.6 s | 78 ms | 240/240 |
| 500 | 3 个 | 38.6 s | 41.5 s | 66.1 s | 77 ms | 500/500 |
| 1000 | 3 个 | 88.5 s | 87.9 s | 137 s | 88 ms | 1000/1000 |
| 150 | 150 个 | 16.3 s | 13.9 s | 21.1 s | 108 ms | 150/150 |
| 200（含 40 图片段） | 3 个 + 40 张图 | 43.9 s | 17.3 s | 28.1 s | 219 ms | 200/200，图片段时长精确 |
| 239（含 39 段画中画） | 3 个 | 25.6 s | 20.9 s | 33.3 s | 128 ms | 239/239，V2 独立落位 |

每一行都核过：`placed == exact == 段数`、markers 一条不少、`timeline_end == expected_end`、
写回后 `basics_diff` / `structure_diff` 都是 0 处不一致。

结论：

- **段数线性增长，没有崩点**。1000 段（约 3 万帧）也一段不错，只是要一分半钟。
- **钱花在 API 调用上**：`AppendToTimeline` 约 65 ms/次、`ExportLUT` 约 85 ms/次，
  这两项占了绝大部分。插件自己的 Python 逻辑（计划 / 快照 / 体检 / 写回克隆 /
  帧数对账）在 1000 段时合计约 0.2 秒，可以忽略。
- **写回含一次取回**（面板上是「一次点击做完」），所以写回耗时 ≈ 取回 + 25 ms/段。
- **图片段是唯一的非线性来源**：每张要先用 ffmpeg 转成等长视频，约 0.9 s/张；
  转过的按「源图 + 帧数 + 画布」缓存，同一份草稿重送不重复编码。100 张图 ≈ 1.5 分钟。
- **素材文件数不是断崖**：150 个不同文件比 3 个文件每段多约 13 ms（媒体池导入 + 索引）。

顺带修掉两处只有大工程才暴露的问题：图片段原来是**逐张导入**并且每张都重建一次
媒体池索引（100 张图 = 上万次 `GetClipProperty`），现在整批转码、一次导入；`send`
与 `pull` 里各有一处「在列表里线性查找」，2000 段时各要 45 ms 与 142 ms，改成索引表。
这两处都有回归测试锁住（`tests/test_send_fake.py::TestSendPhoto`）。

### 怎么确认这台机器没问题

1. **能探测的不写死** —— 缺方法的路径有降级、有报告，不会变成 traceback；
2. **假 API 面回归** —— `tests/test_compat.py` 用一套 21.2 的假 API 面（含
   「谎报方法存在」的空壳）断言能力探测与降级行为；`tests/test_multicam.py`
   锁住覆盖匹配与共享 LUT；CI 每次都会跑；
3. **在本机跑一次自检** —— `colorpass compat` 或面板「诊断」，输出含系统、
   达芬奇版本、实际缺项，贴进 issue 就能定位。

剪映的加密与内存行为属内部实现，升级后可能变化；这类情况由 `docs/剪映草稿字段.md`
中的版本矩阵记录。

## 文档

- [使用手册](docs/使用手册.md) —— 面板操作、改稿后重开的机制、明文来源与缓存
- [面板设计规范](docs/设计规范.md) —— 字号 / 间距 / 控件尺寸令牌，以及怎么用预览图验证
- [剪映草稿字段](docs/剪映草稿字段.md) —— 实测字段表与版本差异
- [参与开发](CONTRIBUTING.md) —— 分层规则、改代码前的三条约定
- [安全策略](SECURITY.md) —— 漏洞上报渠道

## 数据与产物

`%LOCALAPPDATA%\ColorPass\` 下只有可再生数据：

- `plain\` —— 明文缓存（滚动保留 12 份，按磁盘指纹失效）
- `luts\` —— LUT 工作副本
- `retime\` —— 变速预渲染的中间素材
- `runs\` —— 每次动作的报告 JSON

另有一类跟随草稿：写回时把 cube 复制进新草稿目录的 `Resources/ColorPass/`，
草稿被复制或迁移到其他机器，LUT 依然有效。

`colorpass gc` 只统计可回收量，加 `--apply` 才实际删除。

## 隐私与边界

- 只读本机、当前用户、当前打开的剪映草稿；不联网、不上传。
- 不绕过剪映授权限制，不解析他人文件，不修改剪映安装目录。
- 设置 `COLORPASS_NO_MEMSCAN=1` 可完全关闭内存扫描，仅用缓存与载入备份
  （代价：从未读取过的新草稿需先执行一次送入并保持剪映打开）。
- **macOS 的读取器（`reader.dylib`）需要你知道它在做什么**：它靠 `DYLD_INSERT_LIBRARIES`
  注入到剪映进程里，在进程内部把草稿解密后交给本工具（密钥不出进程、不落盘、不联网），
  并且只处理你自己机器上、你自己创建的草稿。剪映的签名带 `disable-library-validation`，
  所以不需要关闭 SIP、也不需要开发者证书。这属于对剪映加载机制的利用，可能触及剪映的
  用户协议；且**剪映一旦改成严格校验动态库，这条路当天失效**（读取器会明确报错，不会
  静默失败）。不接受这一点就用 Windows 版，或只用「手动拖入明文草稿」以外的功能。
- `colorpass doctor` 只输出本机环境信息（系统、版本、路径、API 有无），
  不含素材内容、时间线片段与草稿正文；贴 issue 前可自行过一眼。
- 本项目与字节跳动、Blackmagic Design 均无关联。

## 开发与测试

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -p "test_*.py"
python -m ruff check colorpass tests --line-length 100
```

离线测试不依赖达芬奇、不依赖剪映、不触碰任何真实草稿。CI 覆盖
Ubuntu 静态检查、Windows 3.9 / 3.12 与 macOS 3.9 / 3.12 全量测试，
并在无达芬奇的环境下跑一次 `colorpass doctor` 验证报告完整性。

打安装包（需要 `pip install pyinstaller`，以及 Inno Setup 6 的 `ISCC.exe`）：

```bash
python tools/make_logo.py        # 图标 / .ico / 横幅（改版本号后重跑，横幅上的版本会跟着变）
python tools/build_installer.py  # 产出 dist/ColorPass（运行时）与 dist/ColorPass-Setup-<版本>.exe
```

产物与目录：`installer/entry_cli.py`（运行时入口）、`installer/ColorPass.iss`（安装向导：
复制文件、卸载条目、静默参数）、`colorpass/postinstall.py`（复制完之后注册面板 /
写配置，安装程序通过 `ColorPass.exe setup --bin <app>` 调它）、
`tools/build_installer.py`（PyInstaller 运行时 + ISCC 编译）、`assets/`（图标与横幅）。

真机上排查用的脚本（都在 `tools/`，都不改工程设置，实验时间线跑完自己删）：

```bash
python tools/apimap.py            # 这台机器的脚本 API 全貌（哪些方法真的能调）
python tools/lab21.py             # 逐个试「调了到底有没有效果」（自建 LAB21 时间线，测完删掉）
python tools/verify_multicam.py --media a.mp4 b.mp4   # 多机位/合并片段全链路
python tools/verify_photo_audio.py    # 图片段与音频轨能不能按时长落进达芬奇
python tools/verify_pa_e2e.py         # 「视频+图片+音乐」走完整 send.run（不需要剪映）
python tools/verify_write_basics.py   # 回剪映后的帧数/画布/素材像素（拿真实快照重跑一遍写回）
python tools/stress_scale.py --offline 50,200,500,1000   # 段数放大时的纯 Python 侧耗时
python tools/stress_scale.py --resolve 120,500           # 真机：造 N 段 → 送入 → 取回 → 写回
python tools/panel_preview.py sent && python tools/shot.py build/panel.png   # 面板截图验收
```

`stress_scale.py` 的合成草稿刻意做成脏数据：每 7 段一个转场、每 11 段一个变速、
每 13 段一组关键帧、每 17 段一张图片、每 19 段一个停用段。`--photos N` 会插入 N 段
图片（张张不同文件，压批量转码）、`--overlay-every K` 每 K 段在 V2 叠一段画中画、
`--distinct N` 用 N 个不同素材文件（单独压媒体池导入）。真机模式跑完自己删掉实验
时间线，写回落在临时目录（不往剪映工程列表里塞假草稿）。

## 许可证

[Apache-2.0](LICENSE)，含专利授权。
