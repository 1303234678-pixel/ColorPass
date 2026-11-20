<p align="center">
  <img src="assets/icon.png" width="120" alt="ColorPass">
</p>

# ColorPass

[![CI](https://github.com/1303234678-pixel/ColorPass/actions/workflows/ci.yml/badge.svg)](https://github.com/1303234678-pixel/ColorPass/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776ab.svg)](pyproject.toml)

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
   │  ① 送入达芬奇
   ▼
达芬奇时间线（13 段 / 原时长 / 原位置 / 停用段保留）
   │  逐段调色
   │  ② 写回剪映
   ▼
新草稿（原剪辑结构 + 每段一条 LUT，可继续剪、可再调）
```

面板上只有三个操作：① 送入达芬奇、② 写回剪映（基于送入时的快照）、锁定/解锁轨道。
所有能力同时提供命令行入口，`check` / `plan` / `status` 不依赖达芬奇。

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
| 变速 | ffmpeg 预渲染为等长 ProRes，帧序与剪映一致（达芬奇脚本接口无速度入口） |
| 转场 | 后段起点收前段时长，位置不偏；近似处理，非原生转场 |
| 停用片段 `visible:false` | 照样占位，落轨后 `SetClipEnabled(False)` |
| 复合片段 | 递归展开 |
| 帧率不一致 | 绝对帧号 + 共用帧率换算，无累积误差 |

停用片段必须占位：实测一份 12.267 s 的草稿，其中有 5.5 s 由一条停用段撑起。跳过不送会在
时间线上留黑场，并使后续所有位置核对失效。

送入范围是视频轨（含复合片段）。音频、文字、贴纸、滤镜与独立特效轨不进入达芬奇，
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

---

## 安装

### 方式一：安装包（推荐给测试者）

下载 `ColorPass-Setup-<版本>.exe`，双击即可。安装器会把运行时复制到
`%LOCALAPPDATA%\ColorPass\bin`，把面板放进达芬奇的插件目录，并写好配置。

**不需要装 Python。** 装完重启达芬奇，菜单 `工作区 → Workflow Integrations → ColorPass`。

- 卸载：`ColorPass-Setup-<版本>.exe --uninstall`（保留草稿缓存与报告）
- 装完发现菜单里没有：确认达芬奇 ≥ 18；插件目录在
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

两种方式都会写同一份 `config.json`：源码模式记 `product` 与 `python`，安装包模式记 `cli`。
面板优先用 `cli`（不需要 Python），没有才回退到 `python -m colorpass.cli`。
重新运行任一方式的安装命令即可切换，互不冲突。

可选依赖：

```bash
python -m pip install -e ".[cover]"     # 草稿封面缩略图（Pillow）
python -m pip install -e ".[ffmpeg]"    # 变速预渲染（缺省时退回近似摆放）
```

安装包里**没有**打包 ffmpeg（那会让体积从 20 MB 涨到 186 MB）。需要帧级准确的变速预渲染时，
把 `ffmpeg` 放进 PATH 或设 `COLORPASS_FFMPEG` 指向它；没有就自动退回「占位对齐」模式，
并在报告里写明。要打全量包：`python tools/build_installer.py --with-ffmpeg`。

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
colorpass doctor                     # 环境与兼容性报告（JSON，贴 issue 用）
```

`plan` 输出的就是 `send` 内部实际使用的计划，包含重叠消解、预渲染标记与跳过原因，
可直接 diff 两次改动前后的落轨位置，便于在 CI 与批处理中回归。

## 模块地图

```
colorpass/
├── probe.py       剪映工程识别（进程、窗口、草稿目录、锁状态）
├── memscan.py     进程内存扫描（Windows ReadProcessMemory）
├── plain.py       明文三级来源仲裁
├── draftio.py     草稿语义层：轨道 / 变速 / 转场 / 蒙版 / 复合片段 / 新鲜度
├── send.py        草稿 → 达芬奇：落轨计划、变速、停用段、marker
├── pull.py        达芬奇 → LUT：marker 身份锚 + ExportLUT
├── doctor.py      环境与兼容性报告：平台 / 版本 / API 缺项（只读）
├── draftbuild.py  克隆草稿并追加 LUT 材质
├── write.py       写回编排 + 结构断言（structure_diff）
├── gc.py          缓存回收
├── preflight.py   四道闸：剪映在跑 / 草稿已开 / 草稿未改 / 达芬奇可用
└── panel/         达芬奇面板（Fusion 进程内，仅通过子进程调用 CLI）
```

分层规则见 [CONTRIBUTING.md](CONTRIBUTING.md)：`L0 环境 → L1 草稿语义 → L2 动作编排 → L3 壳`，
依赖单向，读草稿的模块不得依赖动作编排。

## 兼容性

| 组件 | 实测版本 | 备注 |
|---|---|---|
| Windows | 10 / 11 | 完整链路；依赖 `%LOCALAPPDATA%` 与 `ReadProcessMemory` |
| macOS | 未真机联调 | 数据目录、剪映路径、面板解释器、脚本模块搜索路径均已按 mac 取值，CI 在 `macos-latest` 上跑全量离线测试；仍缺「窗口级」草稿识别（见下） |
| 剪映专业版 | 11.7.0 / 11.9 | 正文加密；字段行为见 [docs/剪映草稿字段.md](docs/剪映草稿字段.md) |
| 达芬奇 | 18+（本地 20.x） | 需 Workflow Integration 支持；API 按 `hasattr` 判断，不按版本号 |
| Python | 3.9+（开发与 CI 用 3.12） | 无第三方硬依赖 |

**为什么不用版本号判断能力。** 达芬奇的脚本 API 是增量加的，同一个大版本内部
也会有增删，所以代码里一律 `hasattr` 探测 + 降级（例如 `SetClipEnabled` 缺失时
停用片段照常落轨，只在报告里标注未生效）。想知道某个组合上到底缺哪些方法，
跑一次 `colorpass doctor`，看 `resolve.api.missing`。

**没有的机器怎么验。** 兼容性不靠开发者凑齐所有硬件，而是靠三件事叠加：

1. **能探测的不写死** —— 缺方法的路径有降级、有报告，未测过的版本大多能直接跑通；
2. **CI 覆盖平台差异** —— `windows-latest` 与 `macos-latest` 各跑一遍全量离线测试
   （达芬奇装不进 CI，但平台分支全在纯 Python 这半边）；
3. **用户回帖补矩阵** —— `colorpass doctor` 输出一份 JSON，含系统、达芬奇版本、
   每个对象上缺哪几个方法、路径与实际加载到的脚本模块位置，贴进 issue 即可定位。

剪映的加密与内存行为属内部实现，升级后可能变化；这类情况由 `docs/剪映草稿字段.md`
中的版本矩阵记录，并以 issue 模板 `compat` 收集反馈。

## 文档

- [使用手册](docs/使用手册.md) —— 面板操作、改稿后重开的机制、明文来源与缓存
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
- `colorpass doctor` 只输出本机环境信息（系统、版本、路径、API 有无），
  不含素材内容、时间线片段与草稿正文；贴 issue 前可自行过一眼。
- 本项目与字节跳动、Blackmagic Design 均无关联。

## 开发与测试

```bash
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -p "test_*.py"
python -m ruff check colorpass tests --line-length 100
```

330 个离线测试，不依赖达芬奇、不依赖剪映、不触碰任何真实草稿。CI 覆盖
Ubuntu 静态检查、Windows 3.9 / 3.12 与 macOS 3.9 / 3.12 全量测试，
并在无达芬奇的环境下跑一次 `colorpass doctor` 验证报告完整性。

打安装包（需要 `pip install pyinstaller`）：

```bash
python tools/make_logo.py        # 图标 / .ico / 横幅（改版本号后重跑，横幅上的版本会跟着变）
python tools/build_installer.py  # 产出 dist/ColorPass（运行时）与 dist/ColorPass-Setup-<版本>.exe
```

产物与目录：`installer/entry_cli.py`（运行时入口）、`installer/entry_setup.py`（安装器逻辑）、
`tools/build_installer.py`（两次 PyInstaller 调用与裁剪规则）、`assets/`（图标与横幅）。

## 许可证

[Apache-2.0](LICENSE)，含专利授权。
