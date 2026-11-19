"""真数据测试 —— 用上一次「送入」留下的 plan/pull/cube 校验判定逻辑。

为什么要有
----------
真实事故全是「结构对不上」：13 段的新计划配 14 段的旧 pull、段被删却照样导 LUT、
cube 不是完整 33³。合成数据不一定复现这些，所以这里直接吃 %LOCALAPPDATA%
里上一次真实送入的产物。没有数据就整类跳过（不假报成功）。

只读：不改 plan/pull/cube，也不碰达芬奇和剪映。
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from colorpass import paths, pull, snapshot, write  # noqa: E402


def _load():
    try:
        ptr = snapshot.current()
        if not ptr or ptr.get("_incomplete"):
            return None
        run_dir = Path(ptr.get("run_dir") or "")
        plan_p = run_dir / "plan.json"
        pull_p = run_dir / "pull.json"
        if not (plan_p.exists() and pull_p.exists()):
            return None
        plan = json.loads(plan_p.read_text(encoding="utf-8"))
        pl = json.loads(pull_p.read_text(encoding="utf-8"))
        if not plan.get("segments") or not pl.get("segment_luts"):
            return None
        return {"ptr": ptr, "plan": plan, "pull": pl, "run_dir": run_dir}
    except Exception:
        return None


REAL = _load()


class _It(object):
    def __init__(self, start, dur, sid):
        self.start, self.dur, self.sid = start, dur, sid

    def GetStart(self):
        return self.start

    def GetDuration(self):
        return self.dur

    def GetName(self):
        return self.sid[:8]

    def GetMarkers(self):
        return {0: {"name": "CP", "customData": self.sid}}


class _Tl(object):
    def __init__(self, rows, name):
        self.rows, self.name = rows, name

    def GetName(self):
        return self.name

    def GetTrackCount(self, kind):
        return max([lane for lane, _ in self.rows] or [1])

    def GetItemListInTrack(self, kind, lane):
        return [it for (l, it) in self.rows if l == lane]


def rebuild(plan, drop=None):
    """按 plan.json 里的真实落点重建一条时间线（含尾巴段）。"""
    base = int(plan.get("start_frame") or 0)
    rows = []
    for s in plan["segments"]:
        if drop and s["segment_id"] == drop:
            continue
        rows.append((s["lane"], _It(base + s["rec"], s["dur"], s["segment_id"])))
        for it in s.get("items") or []:
            if it.get("kind") == "pad":
                rows.append((s["lane"], _It(base + it["start"], it["dur"],
                                            s["segment_id"] + paths.PAD_SUFFIX)))
    return _Tl(rows, plan.get("timeline") or "")


@unittest.skipUnless(REAL, "本机没有上一次真实送入的 plan/pull")
class TestRealPlan(unittest.TestCase):
    def test_plan_segments_have_ids(self):
        segs = REAL["plan"]["segments"]
        ids = [s["segment_id"] for s in segs]
        self.assertEqual(len(ids), len(set(ids)), "段 ID 有重复")
        self.assertTrue(all(ids))

    def test_alignment_passes_on_rebuilt_timeline(self):
        plan = REAL["plan"]
        issues, placed = pull.verify_alignment(
            rebuild(plan), plan, int(plan.get("start_frame") or 0))
        self.assertEqual(issues, [], issues[:3])
        self.assertEqual(len(placed), len(plan["segments"]))

    def test_alignment_catches_missing_segment(self):
        plan = REAL["plan"]
        victim = plan["segments"][0]["segment_id"]
        issues, _ = pull.verify_alignment(
            rebuild(plan, drop=victim), plan, int(plan.get("start_frame") or 0))
        self.assertTrue(any("不存在了" in i["why"] for i in issues))

    def test_alignment_catches_shift(self):
        plan = json.loads(json.dumps(REAL["plan"]))
        tl = rebuild(plan)
        # 把第一段的落点往右挪 3 帧（模拟用户在达芬奇里拖动片段）
        tl.rows[0] = (tl.rows[0][0], _It(tl.rows[0][1].start + 3,
                                         tl.rows[0][1].dur,
                                         tl.rows[0][1].sid))
        issues, _ = pull.verify_alignment(tl, plan, int(plan.get("start_frame") or 0))
        self.assertTrue(any("位置与计划不符" in i["why"] for i in issues))

    def test_real_cubes_all_usable(self):
        bad = write.verify_cubes(REAL["pull"]["segment_luts"])
        self.assertEqual(bad, [], bad[:3])

    def test_pull_ids_are_within_plan(self):
        ids = {s["segment_id"] for s in REAL["plan"]["segments"]}
        luts = set(REAL["pull"]["segment_luts"])
        self.assertEqual(luts - ids, set(), "pull 里有计划中不存在的段（会挂错）")

    def test_manifest_matches_pull(self):
        f = REAL["run_dir"] / "manifest.json"
        if not f.exists():
            self.skipTest("这份 run 没有 manifest.json")
        man = json.loads(f.read_text(encoding="utf-8"))
        cubes = {c["segment_id"] for c in man.get("cubes") or []}
        self.assertEqual(cubes, set(REAL["pull"]["segment_luts"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
