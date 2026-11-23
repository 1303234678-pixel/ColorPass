"""把真面板代码在 21.2 里跑起来，用来截图验收。

面板平时由 Fusion 菜单启动（宿主注入 `fusion` / `bmd` 两个全局）。这里手动
注入同样的两个名字，再把 `Panel.loop` 换成「跑 N 秒就退出」，就能在不点菜单
的情况下看到真实渲染结果 —— 验的是装进达芬奇的那份代码，不是另写的样机。
"""
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from colorpass import resolve  # noqa: E402

f = resolve.scriptapp("Resolve").Fusion()
import fusionscript  # noqa: E402

ui = f.UIManager
bmd = types.SimpleNamespace(UIDispatcher=fusionscript.UIDispatcher)

src = (ROOT / "colorpass" / "panel" / "ColorPass.py").read_text(encoding="utf-8")
cut = src.find("\ntry:\n    main()")
src = src[:cut]

ns = {"__name__": "panel_preview", "__file__": "ColorPass.py",
      "fusion": f, "bmd": bmd}
exec(compile(src, "ColorPass.py", "exec"), ns)

Panel = ns["Panel"]
STOP = ROOT / "build" / "stop_panel"

# 面板开窗后会进自己的 loop；这里换成「步进到收到停止文件」。
real_init = Panel.__init__


def patched_init(self):
    self.closing = False
    real_loop = self.loop
    self.loop = lambda: None
    real_init(self)
    self.loop = real_loop


Panel.__init__ = patched_init

MODE = sys.argv[1] if len(sys.argv) > 1 else "idle"
p = Panel()
if MODE == "sent":
    p._has_draft = True
    p._draft_name = p._sent_name = "123"
    p.items["jybDraft"].Text = "123"
    p._can_write = True
    p.set_state("已送入", ns["C_ACCENT"])
    p.say_r("已送入达芬奇（11-21 02:18）。调完色就能写回。")
    p.say_result("写回完成：新草稿「123_调色」，12 段 LUT 已嵌入\n"
                 "原样保留：画中画 1 轨 · 复合片段 2 个", tone="ok")
    p._sync_buttons()
elif MODE == "long":
    p._has_draft = True
    p._draft_name = "企业宣传片 · 客户确认…"
    p.items["jybDraft"].Text = p._draft_name
    p._can_write = True
    p.set_state("待重开", ns["C_WARN"])
    p.say_r("打开后又改过（02:14:03）。关掉重开一次再送入；只按 Ctrl+S 不够。\n"
            "写回不受影响，用的是上次送入那版。")
    p.say_result("写回完成：新草稿「企业宣传片 · 客户确认…_调色」，12 段 LUT 已嵌入\n"
                 "原样保留：变速 3 段 · 转场 2 个 · 蒙版 2 个 · 画中画 1 轨 · 复合片段 2 个\n"
                 "不送：文本 1、贴纸 1", tone="ok")
    p._sync_buttons()
print("window:", p.win.GetGeometry())
for k in ("jybDraft", "jybInfo", "btnSend", "btnWrite", "txtName",
          "jybResult", "jybState", "btnDiag"):
    try:
        print(" ", k, dict(p.items[k].Geometry))
    except Exception:
        pass
sys.stdout.flush()

end = time.time() + 40
while time.time() < end:
    try:
        p.disp.StepLoop()
        if MODE == "idle":
            p.apply_status()
        p.poll_job()
        p.poll_diag()
    except Exception as exc:
        print("loop error", exc)
    time.sleep(0.02)
    if STOP.exists():
        break
try:
    p.win.Hide()
except Exception:
    pass
