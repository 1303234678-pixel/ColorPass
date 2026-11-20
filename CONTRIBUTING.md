# 参与开发

## 先跑起来

```bat
git clone https://github.com/1303234678-pixel/ColorPass.git
cd colorpass
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -p "test_*.py"
python -m ruff check colorpass tests --line-length 100
```

要求 Windows（测试会 import `ctypes.wintypes`），Python 3.9+，开发与 CI 用 3.12。
没有达芬奇、没有剪映也能跑全部离线测试——这是这个项目最重要的一条约束。

## 分层规则

依赖只能往下走，**不许反向**：

```
L3  cli.py / panel/          壳：参数解析、界面
L2  send.py / pull.py / write.py / workflow.py     动作编排
L1  draftio.py               草稿语义：轨道、变速、转场、蒙版、复合片段、新鲜度
L0  paths.py / probe.py / memscan.py / plain.py    环境与明文来源
```

- `draftio.py` 曾经反向 `import jy_workflow`，导致「读草稿」这件事必须把达芬奇
  那套编排也拖进来，没法单独安装。**别再引入这种依赖。**
- 读草稿的代码不许 import `send` / `write` / `pull`。
- 面板不许 import 这个包（它跑在 Fusion 进程里，`sys.path` 上没有工程）；
  它只通过 `python -m colorpass.cli` 起子进程。

## 改代码前

1. **先在真实草稿上量一次。** 剪映的内部字段是靠实测得来的，不是文档。改字段
   解析前，用一份真实草稿打印出实际值，把结论写进注释。
2. **写一个会失败的测试。** 这个仓库的回归测试都是从真实事故里长出来的
   （见 `tests/` 里的 docstring），不是为覆盖率凑数。
3. **确认测试不碰真环境。** 用 `FakeResolve` / `FakeTimeline` / `FakeItem` 和临时
   目录；不读 `%LOCALAPPDATA%` 下的真实草稿，不要求剪映或达芬奇在运行。

## 改面板

面板跑在达芬奇的 Fusion 进程里，只有 Qt 控件 + Qt 样式表可用。改动前先读
`ColorPass.py` 顶部「外观」那段约定：一个强调色、四级字号、间距 4 的倍数。
新增控件走 `_header` / `_draft_block` 这类构造函数，别直接在 `_body()` 里堆。

`StyleSheet` / `MinimumSize` 不是每个 Fusion 版本都认，所以建窗是三级降级
（带样式 → 不带样式 → 空壳）。新增任何样式属性都要想一遍：**它失败了会怎样？**
正确结果是退化成朴素版，不是面板打不开。

## 提交前检查

- [ ] `python -m unittest discover -s tests -p "test_*.py"` 全过
- [ ] `python -m ruff check colorpass tests --line-length 100` 干净
- [ ] 新行为有测试；测试名说清「防的是什么事故」
- [ ] 改了行为就同步改 `README.md` / `docs/`
- [ ] 没有提交真实草稿、素材、导出的 `.cube` 或日志
- [ ] 中文注释与提交信息保持一致（这个仓库是中文优先的）

## 兼容性：没有那台机器怎么验

达芬奇装在 CI 里跑不起来（许可 + GUI），剪映更是只能用真实草稿。所以兼容性
靠三层，从便宜到贵：

**第一层 · 能力探测 + 签名容忍，不认版本号。**
调任何可能缺的方法前先 `hasattr`，缺了就降级并把「没生效」写进报告，不抛异常。
新增 API 调用时同时改两处：

- `resolve.API_GROUPS` —— 方法清单，`doctor` 靠它报缺项；
- `compat.FEATURES` —— 加上「缺了会怎样」的用户可读说明，面板与 `compat` 靠它。

方法签名在版本间变长时，用 `compat.call_shrink()` 从尾部丢参数，**但只在末尾参数
是可选的时**用；携带必需信息的参数（如片段 marker 里的段号）必须显式分层调用
（参考 `send.mark_item`），否则会「调用成功但结果不对」。常量缺失用
`compat.lut_type()` 这类兜底函数，别直接 `resolve.EXPORT_LUT_33PTCUBE`。

**第二层 · CI 跑平台差异。**
`windows-latest` 与 `macos-latest` 各跑一遍全量离线测试 + `colorpass doctor --compact`。
达芬奇对象由 `FakeResolve` 代劳，验的是「平台分支、路径取值、面板加载、
报告完整性」这些真机上最容易出错的纯 Python 部分。mac 本地不用买机器。

**第三层 · 假 API 面回归。**
`tests/test_compat.py` 按版本增量造出 17 / 18 / 20 三套 API 面（把方法从对象上删掉，
就是那版的样子），断言缺能力时降级而不是抛异常。加新 API 依赖时，给老surface
也补一条用例：**没装那版机器，也要能证明它在老版本上不会炸。**

**第四层 · 让用户回帖补矩阵。**
自己没装那个版本时，请对方跑：

```bash
colorpass compat            # 人读的一段话：缺什么、各自影响什么
colorpass compat --json     # 机器读
colorpass doctor --compact  # 全量快照，单行 JSON
```

面板页脚也有一键「诊断」，效果同 `colorpass compat`，适合让不熟悉终端的测试者用。

`resolve.api.missing` 就是「这个组合缺哪些方法」，`resolve.version` /
`resolve.product` / `jianying.installed_version` 是版本，`resolve.module` 是实际
加载到的脚本模块路径（`COLORPASS_SCRIPT_MODULES` 可指向另一份安装，用来在同机
并存的两个达芬奇版本间切换验证）。拿到回帖后，把结论写进 README 的兼容性表。

macOS 目前还缺一块：**识别「剪映当前打开的是哪一份草稿」依赖 Win32 窗口接口**，
所以 `probe.resolve_open_draft` 在 mac 上会明确报「无法自动识别」而不是猜一个
最近修改的草稿。要补这块需要真机调试（AppleScript / Accessibility 或
`NSWorkspace` 路线），有 mac 的贡献者可以带着 `doctor` 输出开 issue 讨论。

## 加一个「不送」的能力

`draftio.UNSENT_SPEC` 是「草稿里有、但不送进达芬奇」的唯一清单，面板与报告都从
这里取文案。新增一项时：

1. 在 `UNSENT_SPEC` 加条目（标签 + 为什么）；
2. 在 `feature_counts` 里能数出来；
3. 在 `write.structure_index` 里进语义指纹（写回时它必须原样保留）。

## 发布

版本号在 `colorpass/__init__.py`。打 tag 前跑一次真实往返（送入 → 调色 → 写回），
并在 `docs/剪映草稿字段.md` 更新「最后一次实测的剪映版本」。

出安装包的顺序（版本号一改就要全走一遍，安装器文件名和横幅上的版本都读它）：

```bash
python tools/make_logo.py         # 横幅上印版本号
python tools/build_installer.py   # dist/ColorPass-Setup-<版本>.exe
```

然后把 exe 挂到 Release，并在 release note 里写上构建脚本打印的 SHA256 ——
测试者下完可以自己核对，也方便判断「是不是同一个包」。

安装器的两条路径（源码模式 / 打包模式）写的是同一份 `config.json`，区别只在
`cli` 还是 `product`+`python`。改面板的启动逻辑时，两个模式都要试：
`python -m colorpass.install` 与双击 setup exe，各点一次「刷新」看状态栏有没有出草稿名。
