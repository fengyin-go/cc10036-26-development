"""异常检测领域服务：多算法检测、告警规则、采样空窗识别、补采重算。

与结果缓存模块串联：每次检测先按指标序列计算空窗检测数据，再只对真实观测点
跑异常算法；检测记录（含来源时间、有效时段、数据来源）写入共享缓存，值班
读取时只取本时段有效检测，缺测结果不能当正常观测使用。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.analytics_engine import (
    detect_anomalies_zscore,
    detect_anomalies_iqr,
    detect_anomalies_moving_avg,
    detect_sampling_gaps,
)

# 数据来源标识
SOURCE_REALTIME = "实时采样"
SOURCE_BACKFILL = "补采"
SOURCE_HISTORY = "历史读值"


class AnomalyDetectService(BaseModuleService):
    MODULE = "anomaly_detect"
    ENTITY = "检测规则"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["规则名称", "监控指标", "检测算法", "阈值设置", "参考周期", "告警级别", "是否启用"]
    REQUIRED_FIELDS = ["规则名称", "监控指标", "检测算法"]
    STATUS_ORDER = ["草稿", "监控中", "已触发", "已关闭"]
    ACTION_RULES = {"启用监控": "监控中", "确认触发": "已触发", "关闭规则": "已关闭"}
    NEGATIVE_ACTIONS = ["关闭规则"]

    def __init__(self) -> None:
        # 检测记录台账：交班时不同来源（夜班实时 / 今日补采）的记录同时保留可见
        self._records: list[dict[str, Any]] = []
        self._record_seq = 0

    # ---- 空窗检测 ----

    def analyze_gaps(
        self,
        series: list[dict[str, Any]] | list[float],
        *,
        interval: float | None = None,
        period_start: float | None = None,
        period_end: float | None = None,
    ) -> dict[str, Any]:
        """按指标序列计算空窗检测数据，区分信号平稳与无人上报。"""
        return detect_sampling_gaps(
            series, interval=interval,
            period_start=period_start, period_end=period_end,
        )

    # ---- 核心检测流水线 ----

    def analyze_series(
        self,
        rule_id: int,
        series: list[dict[str, Any]] | list[float],
        *,
        interval: float | None = None,
        period_start: float | None = None,
        period_end: float | None = None,
        source: str = SOURCE_REALTIME,
        run_time: float | None = None,
        ttl_seconds: int = 1800,
        replaced_record_id: int | None = None,
    ) -> dict[str, Any]:
        """对指标时间序列执行「空窗检测 + 异常检测」，并登记检测记录。

        缺测槽位不参与算法计算，也不会被写成 0 异常；空窗结论随记录一起返回。
        """
        rule = store.find(self.MODULE, rule_id)
        if rule is None:
            return {"ok": False, "message": f"检测规则 {rule_id} 不存在"}

        run_time = time.time() if run_time is None else float(run_time)

        # Step 1：先算空窗，拿到本时段有效观测范围
        gap_report = detect_sampling_gaps(
            series, interval=interval,
            period_start=period_start, period_end=period_end,
        )
        observed_values = gap_report["observed_values"]

        # Step 2：只对真实观测点跑异常算法，缺测点不补零、不参与统计
        algorithm = str(rule.get("检测算法", "zscore")).lower()
        threshold_raw = str(rule.get("阈值设置", "3.0"))
        try:
            threshold = float(threshold_raw)
        except ValueError:
            threshold = 3.0

        if "iqr" in algorithm:
            anomalies = detect_anomalies_iqr(observed_values, threshold)
        elif "moving" in algorithm or "avg" in algorithm:
            window = int(rule.get("参考周期", "7") or 7)
            anomalies = detect_anomalies_moving_avg(observed_values, window, threshold)
        else:
            anomalies = detect_anomalies_zscore(observed_values, threshold)

        # Step 3：状态流转——有真实异常才触发；断采/缺测不等于异常
        self.run_action(rule_id, "启用监控")
        if anomalies:
            self.run_action(rule_id, "确认触发")

        self._record_seq += 1
        record = {
            "ok": True,
            "record_id": self._record_seq,
            "rule_id": rule_id,
            "rule_name": str(rule.get("规则名称", "")),
            "monitoring_metric": str(rule.get("监控指标", "")),
            "algorithm": algorithm,
            "source": source,
            "source_time": run_time,
            "period_start": gap_report["period_start"],
            "period_end": gap_report["period_end"],
            "valid_ranges": gap_report["valid_ranges"],
            "data_points": gap_report["observed_points"],
            "expected_points": gap_report["expected_points"],
            "missing_points": gap_report["missing_points"],
            "coverage_rate": gap_report["coverage_rate"],
            "has_gap": gap_report["has_gap"],
            "is_fully_sampled": gap_report["is_fully_sampled"],
            "is_stable": gap_report["is_stable"],
            "signal_state": gap_report["signal_state"],
            "gap_windows": gap_report["gap_windows"],
            "anomaly_count": len(anomalies),
            "anomalies": anomalies,
            "alert_level": str(rule.get("告警级别", "低")),
            "replaced_record_id": replaced_record_id,
            # 缺测结果不能作为正常观测使用
            "usable_as_current": gap_report["observed_points"] > 0,
        }
        record["cache_key"] = self._cache_key(rule_id, record)

        # Step 4：登记台账并串联结果缓存模块（带来源时间与有效时段）
        self._records.append(record)
        self._write_through_cache(record, ttl_seconds)
        return record

    def detect(self, rule_id: int, metric_values: list[float]) -> dict[str, Any]:
        """对指标时间序列执行异常检测（兼容旧接口：等间隔完整采样）。"""
        result = self.analyze_series(rule_id, list(metric_values))
        if not result.get("ok"):
            return result
        return {
            "ok": True,
            "rule_name": result["rule_name"],
            "monitoring_metric": result["monitoring_metric"],
            "algorithm": result["algorithm"],
            "data_points": result["data_points"],
            "anomaly_count": result["anomaly_count"],
            "alert_level": result["alert_level"],
            "anomalies": result["anomalies"],
            "gap_report": {
                "has_gap": result["has_gap"],
                "missing_points": result["missing_points"],
                "signal_state": result["signal_state"],
                "valid_ranges": result["valid_ranges"],
            },
        }

    def batch_detect(
        self, rule_ids: list[int], metric_values: list[float],
    ) -> dict[str, Any]:
        """批量检测：多规则并行扫描同一指标序列。"""
        results: list[dict[str, Any]] = []
        for rid in rule_ids:
            r = self.detect(rid, metric_values)
            if r.get("ok"):
                results.append(r)
        total_anomalies = sum(r.get("anomaly_count", 0) for r in results)
        return {
            "ok": True,
            "rule_count": len(rule_ids),
            "total_anomalies": total_anomalies,
            "results": results,
        }

    # ---- 补采重算 ----

    def backfill_and_rerun(
        self,
        rule_id: int,
        backfill_series: list[dict[str, Any]] | list[float],
        *,
        gap_start: float | None = None,
        gap_end: float | None = None,
        interval: float | None = None,
        run_time: float | None = None,
        ttl_seconds: int = 1800,
    ) -> dict[str, Any]:
        """采样中断后重试：按补采时段重算检测数据。

        补采数据覆盖原空窗区间，重算后生成「补采」来源的新记录，并标注被它
        取代的原断采记录；原记录保留在台账中，交班时两段来源同时可见。
        """
        rule = store.find(self.MODULE, rule_id)
        if rule is None:
            return {"ok": False, "message": f"检测规则 {rule_id} 不存在"}

        # 找到该规则最近一条带空窗的记录，作为被补采取代的对象
        replaced = next(
            (r for r in reversed(self._records)
             if r["rule_id"] == rule_id and r["has_gap"]),
            None,
        )
        replaced_id = replaced["record_id"] if replaced else None

        # 补采时段边界：显式传入优先，否则取补采序列自身覆盖范围
        norm = detect_sampling_gaps(
            backfill_series, interval=interval,
            period_start=gap_start, period_end=gap_end,
        )
        period_start = norm["period_start"]
        period_end = norm["period_end"]

        record = self.analyze_series(
            rule_id, backfill_series,
            interval=interval,
            period_start=period_start,
            period_end=period_end,
            source=SOURCE_BACKFILL,
            run_time=run_time,
            ttl_seconds=ttl_seconds,
            replaced_record_id=replaced_id,
        )
        if record.get("ok"):
            record["backfill_window"] = {
                "gap_start": period_start,
                "gap_end": period_end,
            }
            # 补采重算后刷新缓存条目元数据
            self._write_through_cache(record, ttl_seconds)
        return record

    # ---- 台账与监控读取 ----

    def list_detection_records(
        self, rule_id: int | None = None,
    ) -> dict[str, Any]:
        """检测记录台账：不同来源记录同时保留，供交班对照。"""
        rows = self._records
        if rule_id is not None:
            rows = [r for r in rows if r["rule_id"] == rule_id]
        return {
            "ok": True,
            "total": len(rows),
            "items": list(reversed(rows)),
        }

    def read_monitoring(
        self,
        rule_id: int,
        *,
        period_start: float,
        period_end: float,
    ) -> dict[str, Any]:
        """值班读取监控：经结果缓存模块读取本时段有效检测。

        缓存命中但来源时段不覆盖本时段（如夜班记录）时，只作为历史读值返回，
        明确不可作为当前观测；本时段缺测同样不可用，不会返回零异常结论。
        """
        from app.services.result_cache import get_global_cache

        cache = get_global_cache()
        key = self._cache_key(rule_id, {"period_start": period_start, "period_end": period_end})
        entry = cache.peek_entry(key)

        # 同键命中且覆盖本时段、含真实观测：这是本时段有效检测
        if entry is not None:
            hit = entry.result
            if (isinstance(hit, dict)
                    and self._period_covers(hit, period_start, period_end)
                    and hit.get("usable_as_current")):
                record, from_cache = hit, True
            else:
                record, from_cache = None, False
        else:
            record, from_cache = None, False

        if record is None:
            # 本时段无有效检测：找该指标最近一条含真实观测的缓存作为历史读值
            latest_cached = next(
                (e.result for e in cache.entries_snapshot()
                 if isinstance(e.result, dict)
                 and e.result.get("rule_id") == rule_id
                 and e.result.get("usable_as_current")),
                None,
            )
            # 台账兜底：优先覆盖本时段的记录，再退最近一条
            covering = next(
                (r for r in reversed(self._records)
                 if r["rule_id"] == rule_id
                 and self._period_covers(r, period_start, period_end)),
                None,
            )
            record = latest_cached or covering or next(
                (r for r in reversed(self._records) if r["rule_id"] == rule_id),
                None,
            )

        if record is None:
            return {
                "ok": True,
                "rule_id": rule_id,
                "from_cache": from_cache,
                "usable_as_current": False,
                "signal_state": "缺测",
                "message": "本时段尚无任何检测记录，属于无人上报，不能按零异常处理",
            }

        covers = self._period_covers(record, period_start, period_end)
        history = not covers
        effective_source = SOURCE_HISTORY if history else record["source"]
        return {
            "ok": True,
            "rule_id": rule_id,
            "from_cache": from_cache,
            "cache_key": key,
            "usable_as_current": bool(covers and record.get("usable_as_current")),
            "signal_state": record["signal_state"],
            "source": effective_source,
            "source_time": record["source_time"],
            "valid_ranges": record["valid_ranges"],
            "period_start": record["period_start"],
            "period_end": record["period_end"],
            "missing_points": record["missing_points"],
            "has_gap": record["has_gap"],
            "is_stable": record["is_stable"],
            "anomaly_count": record["anomaly_count"],
            "anomalies": record["anomalies"],
            "record_id": record["record_id"],
            "message": (
                "缓存为历史时段读值，不覆盖本时段，不能作为当前观测"
                if history
                else "本时段有效检测"
                if record["usable_as_current"]
                else "本时段存在缺测，缺测结果不能作为正常观测使用"
            ),
        }

    def get_active_rules(self) -> list[dict[str, Any]]:
        """获取所有激活的检测规则（供实验分析消费）。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "监控中"
        ]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        base["artifact"] = "异常检测数据集"
        base["monitoring_count"] = sum(1 for row in rows if row.get("status") == "监控中")
        base["triggered_count"] = sum(1 for row in rows if row.get("status") == "已触发")
        base["detection_records"] = len(self._records)
        base["gap_records"] = sum(1 for r in self._records if r["has_gap"])
        return base

    # ---- 内部辅助 ----

    @staticmethod
    def _cache_key(rule_id: int, record: Any) -> str:
        """缓存键只由规则与时段决定：同一时段的重算/补采更新同一条缓存。"""
        from app.services.result_cache import get_global_cache

        period_start = record.get("period_start") if isinstance(record, dict) else None
        period_end = record.get("period_end") if isinstance(record, dict) else None
        cache = get_global_cache()
        return cache.compute_key(
            "anomaly_detection",
            {
                "rule_id": rule_id,
                "period_start": float(period_start) if period_start is not None else None,
                "period_end": float(period_end) if period_end is not None else None,
            },
        )

    def _write_through_cache(self, record: dict[str, Any], ttl_seconds: int) -> None:
        """把检测记录写入共享结果缓存，带来源时间与有效时段元数据。"""
        from app.services.result_cache import get_global_cache

        cache = get_global_cache()
        cache.put(
            record["cache_key"],
            record,
            row_count=record["data_points"],
            ttl_seconds=ttl_seconds,
            source_time=record["source_time"],
            period_start=record["period_start"],
            period_end=record["period_end"],
            missing_points=record["missing_points"],
            has_gap=record["has_gap"],
            source=record["source"],
        )

    @staticmethod
    def _period_covers(
        record: dict[str, Any], period_start: float, period_end: float,
    ) -> bool:
        """记录的有效观测范围是否覆盖请求时段（端点相接即算覆盖）。

        带 valid_ranges 字段时（含空列表，代表本时段无有效观测）一律以它为准，
        只有缺失该字段的旧记录才回退到声明时段判断。
        """
        valid_ranges = record.get("valid_ranges")
        if valid_ranges is not None:
            return any(
                rng["start_time"] <= period_start and rng["end_time"] >= period_end
                for rng in valid_ranges
            )
        rec_start = record.get("period_start")
        rec_end = record.get("period_end")
        if rec_start is None or rec_end is None:
            return False
        return rec_start <= period_start and rec_end >= period_end
