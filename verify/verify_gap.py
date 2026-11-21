"""采样空窗处理端到端验证：平稳 vs 仅上一时段有观测、补采重算、缓存裁决。"""
import json
import sys

sys.path.insert(0, "/workspace/backend")

from app.services.anomaly_detect import AnomalyDetectService
from app.services.result_cache import ResultCacheService

anomaly = AnomalyDetectService()
cache_svc = ResultCacheService()

def show(title, obj):
    print(f"\n===== {title} =====")
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))

# ---- 场景0：空窗检测分辨「平稳」与「无人上报」 ----
stable_series = [{"timestamp": t, "value": 100.0} for t in range(0, 10)]
gap_series = [{"timestamp": t, "value": 100.0} for t in range(0, 4)]  # 只报了前4个点
empty_series: list = []

g_stable = anomaly.analyze_gaps(stable_series, interval=1, period_start=0, period_end=9)
g_gap = anomaly.analyze_gaps(gap_series, interval=1, period_start=0, period_end=9)
g_empty = anomaly.analyze_gaps(empty_series, interval=1, period_start=0, period_end=9)

assert g_stable["signal_state"] == "平稳" and g_stable["is_stable"] and not g_stable["has_gap"]
assert g_gap["signal_state"] == "断采" and g_gap["has_gap"] and g_gap["missing_points"] == 6
assert g_empty["signal_state"] == "缺测" and g_empty["observed_points"] == 0 and not g_empty["is_stable"]
show("空窗检测：平稳 / 断采 / 缺测", {
    "平稳": {k: g_stable[k] for k in ("signal_state", "is_stable", "has_gap", "coverage_rate")},
    "断采": {k: g_gap[k] for k in ("signal_state", "has_gap", "missing_points", "gap_windows", "valid_ranges")},
    "缺测": {k: g_empty[k] for k in ("signal_state", "is_stable", "observed_points")},
})

# ---- 场景1：夜班完整采样（上一时段 0..9），缓存留下夜班记录 ----
night_series = [{"timestamp": t, "value": 100.0 + (0.01 if t % 2 else 0)} for t in range(0, 10)]
night = anomaly.analyze_series(
    1, night_series, interval=1, period_start=0, period_end=9,
    source="实时采样", run_time=1000.0,
)
assert night["is_fully_sampled"] and not night["has_gap"]
show("夜班完整采样检测", {k: night[k] for k in (
    "record_id", "source", "source_time", "signal_state", "is_stable",
    "anomaly_count", "missing_points", "valid_ranges", "cache_key")})

# ---- 场景2A：今日接班后时序点断了（本时段 10..19 无人上报） ----
today_gap = anomaly.analyze_series(
    1, gap_series, interval=1, period_start=10, period_end=19,
    source="实时采样", run_time=2000.0,
)
# 输入只有 0..3 的点，本时段 10..19 完全无观测 -> 缺测，不能写成零异常
assert today_gap["signal_state"] == "缺测"
assert today_gap["missing_points"] == 10 and today_gap["anomaly_count"] == 0
assert today_gap["usable_as_current"] is False
show("接班后断采（缺测不写成零异常）", {k: today_gap[k] for k in (
    "signal_state", "missing_points", "anomaly_count", "usable_as_current", "has_gap")})

# 值班人从监控读取本时段告警：缓存里仍是夜班记录
read_gap_period = anomaly.read_monitoring(1, period_start=10, period_end=19)
assert read_gap_period["usable_as_current"] is False
assert read_gap_period["source"] == "历史读值"
assert read_gap_period["source_time"] == 1000.0
show("断采时段读监控（缓存只剩夜班记录）", read_gap_period)

# 缓存侧按空窗检测数据裁决：本时段无观测
gap_report_today = anomaly.analyze_gaps([], interval=1, period_start=10, period_end=19)
eff_gap = cache_svc.read_effective_detection(1, gap_report_today)
assert eff_gap["usable_as_current"] is False
assert len(eff_gap["history_reads"]) >= 1
show("缓存裁决：本时段缺测，仅历史读值", eff_gap)

# ---- 场景2B：对照——本轮平稳（本时段 10..19 完整上报且平稳，独立规则对照） ----
STABLE_RULE = 2
today_stable_series = [{"timestamp": t, "value": 100.0} for t in range(10, 20)]
today_stable = anomaly.analyze_series(
    STABLE_RULE, today_stable_series, interval=1, period_start=10, period_end=19,
    source="实时采样", run_time=2100.0,
)
assert today_stable["signal_state"] == "平稳" and today_stable["anomaly_count"] == 0
assert today_stable["usable_as_current"] is True
read_stable = anomaly.read_monitoring(STABLE_RULE, period_start=10, period_end=19)
assert read_stable["usable_as_current"] is True and read_stable["from_cache"] is True
assert read_stable["source"] == "实时采样" and read_stable["source_time"] == 2100.0
show("本轮平稳：缓存命中本时段有效检测", read_stable)

# ---- 场景3：补采重试，按补采时段重算（规则3用IQR，自然波动+尖峰可稳定识别） ----
BACKFILL_RULE = 3
backfill = [
    {"timestamp": t, "value": 100.0 + (t % 3) + (400 if t == 15 else 0)}
    for t in range(10, 20)
]
# 先制造一次断采记录，供补采标注取代关系
gap_only = anomaly.analyze_series(
    BACKFILL_RULE, [{"timestamp": 10, "value": 100.0}],
    interval=1, period_start=10, period_end=19, run_time=2150.0,
)
rerun = anomaly.backfill_and_rerun(
    BACKFILL_RULE, backfill, gap_start=10, gap_end=19, interval=1, run_time=2200.0,
)
assert rerun["source"] == "补采"
assert rerun["replaced_record_id"] == gap_only["record_id"]
assert rerun["is_fully_sampled"] and not rerun["has_gap"]
assert rerun["anomaly_count"] >= 1  # t=15 的尖峰被识别，而不是缺测
show("补采后按补采时段重算", {k: rerun[k] for k in (
    "record_id", "source", "replaced_record_id", "signal_state",
    "missing_points", "anomaly_count", "anomalies", "backfill_window")})

# 补采后再读本时段：应命中补采来源的有效检测
read_after = anomaly.read_monitoring(BACKFILL_RULE, period_start=10, period_end=19)
assert read_after["usable_as_current"] is True
assert read_after["from_cache"] is True
assert read_after["source"] == "补采" and read_after["source_time"] == 2200.0
show("补采后读监控", read_after)

# ---- 场景4：交班台账，两段不同来源记录同时可见（规则1：夜班实时 + 补采） ----
# 规则1同样走一次补采，验证交班时实时/补采两类来源并存
backfill_r1 = [{"timestamp": t, "value": 100.0} for t in range(10, 20)]
rerun_r1 = anomaly.backfill_and_rerun(
    1, backfill_r1, gap_start=10, gap_end=19, interval=1, run_time=2250.0,
)
assert rerun_r1["source"] == "补采"
ledger = anomaly.list_detection_records(rule_id=1)
sources = {item["source"] for item in ledger["items"]}
assert {"实时采样", "补采"} <= sources, sources
show(f"交班检测台账（共{ledger['total']}条，来源同时可见）", [
    {k: r[k] for k in ("record_id", "source", "source_time", "signal_state",
                        "missing_points", "anomaly_count", "replaced_record_id")}
    for r in ledger["items"]
])

# 补采后缓存侧裁决：本时段有效
eff_ok = cache_svc.read_effective_detection(
    BACKFILL_RULE, anomaly.analyze_gaps(backfill, interval=1, period_start=10, period_end=19))
assert eff_ok["usable_as_current"] is True
assert eff_ok["effective"]["source"] == "补采"
show("补采后缓存裁决：有效检测命中", {
    "usable_as_current": eff_ok["usable_as_current"],
    "effective": {k: eff_ok["effective"][k] for k in (
        "source", "source_time", "signal_state", "anomaly_count", "covers_current_period")},
    "history_count": len(eff_ok["history_reads"]),
})

# ---- 场景5：对照——平稳规则的缓存侧裁决同样命中，且与缺测规则结论相反 ----
eff_stable = cache_svc.read_effective_detection(
    STABLE_RULE,
    anomaly.analyze_gaps(today_stable_series, interval=1, period_start=10, period_end=19))
assert eff_stable["usable_as_current"] is True
assert eff_stable["effective"]["signal_state"] == "平稳"
eff_gap2 = cache_svc.read_effective_detection(
    1, anomaly.analyze_gaps([], interval=1, period_start=30, period_end=39))
assert eff_gap2["usable_as_current"] is False
show("两种对照情况的缓存裁决对比", {
    "本轮平稳": {"usable": eff_stable["usable_as_current"],
               "state": eff_stable["effective"]["signal_state"],
               "source_time": eff_stable["effective"]["source_time"]},
    "仅历史有观测/本轮缺测": {"usable": eff_gap2["usable_as_current"],
                          "history_entries": len(eff_gap2["history_reads"])},
})

print("\n全部断言通过 ✅")
