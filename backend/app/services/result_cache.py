"""结果缓存领域服务：缓存生命周期管理、命中率追踪、TTL控制。

与异常监控串联：检测结果写入缓存时带来源时间和有效观测时段；读取时结合空窗
检测数据裁决——只返回覆盖请求时段的有效检测，历史读值（如夜班记录）显式标注
来源时间且不能作为当前观测，缺测结果同样不放行。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.analytics_engine import CacheManager

# 全局缓存管理器，多模块共享
_global_cache = CacheManager(max_entries=500, max_memory_bytes=200 * 1024 * 1024)


def get_global_cache() -> CacheManager:
    """供异常检测等模块复用同一个缓存管理器，保证监控与缓存真正串联。"""
    return _global_cache


class ResultCacheService(BaseModuleService):
    MODULE = "result_cache"
    ENTITY = "缓存条目"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["缓存键", "查询描述", "结果行数", "字节大小", "创建时间", "TTL秒", "命中次数"]
    REQUIRED_FIELDS = ["缓存键", "查询描述", "结果行数"]
    STATUS_ORDER = ["有效", "已失效", "已清理"]
    ACTION_RULES = {"标记失效": "已失效", "清理缓存": "已清理"}
    NEGATIVE_ACTIONS = ["清理缓存"]

    # ---- 业务方法 ----

    def get_or_compute(
        self, query_desc: str, params: dict[str, Any],
        compute_fn: Any, ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """缓存读取或计算（供切片查询和报表消费）。"""
        key = _global_cache.compute_key(query_desc, params)
        cached = _global_cache.get(key)
        if cached is not None:
            return {"ok": True, "from_cache": True, "data": cached, "cache_key": key}
        result = compute_fn()
        _global_cache.put(key, result, ttl_seconds=ttl_seconds)
        return {"ok": True, "from_cache": False, "data": result, "cache_key": key}

    def read_effective_detection(
        self,
        rule_id: int,
        gap_report: dict[str, Any],
    ) -> dict[str, Any]:
        """按空窗检测数据读取本时段的有效检测。

        gap_report 来自异常监控的空窗检测（含 period_start/period_end、
        valid_ranges、has_gap 等）。裁决规则：
        - 缓存条目的有效观测时段必须覆盖本时段，否则只作历史读值返回；
        - 本时段缺测（observed_points=0）不放行，不能当正常观测；
        - 所有返回值都带来源时间与来源标识。
        """
        period_start = float(gap_report["period_start"])
        period_end = float(gap_report["period_end"])
        entries = _global_cache.entries_snapshot()

        candidates: list[dict[str, Any]] = []
        for entry in entries:
            result = entry.result
            if not isinstance(result, dict) or result.get("rule_id") != rule_id:
                continue
            covers = self._entry_covers(entry, period_start, period_end)
            candidates.append({
                "cache_key": entry.key,
                "source": entry.source,
                "source_time": entry.source_time,
                "period_start": entry.period_start,
                "period_end": entry.period_end,
                "valid_ranges": result.get("valid_ranges", []),
                "missing_points": entry.missing_points,
                "has_gap": entry.has_gap,
                "signal_state": result.get("signal_state"),
                "anomaly_count": result.get("anomaly_count"),
                "record_id": result.get("record_id"),
                "covers_current_period": covers,
                "usable_as_current": bool(
                    covers and result.get("usable_as_current", False)
                ),
            })

        # 本时段有效记录优先；其余按来源时间倒序保留为历史读值
        effective = next((c for c in candidates if c["usable_as_current"]), None)
        history = [c for c in candidates if not c["usable_as_current"]]

        if effective is not None:
            # 命中有效缓存，累计一次命中
            _global_cache.get(effective["cache_key"])

        return {
            "ok": True,
            "rule_id": rule_id,
            "requested_period": {"start": period_start, "end": period_end},
            "gap_in_current_period": bool(gap_report.get("has_gap")),
            "current_observed_points": gap_report.get("observed_points", 0),
            "effective": effective,
            "history_reads": history,
            "usable_as_current": effective is not None,
            "message": (
                "命中本时段有效检测缓存"
                if effective is not None
                else "本时段无有效检测缓存：缺测或仅历史时段读值，不能作为正常观测使用"
            ),
        }

    def invalidate_cache(self, cache_key: str) -> dict[str, Any]:
        """标记缓存失效。"""
        success = _global_cache.invalidate(cache_key)
        if success:
            self.run_action(1, "标记失效")
        return {"ok": success, "cache_key": cache_key}

    def cache_stats(self) -> dict[str, Any]:
        """获取缓存统计（供查询加速器消费）。"""
        stats = _global_cache.stats()
        rows = store.rows(self.MODULE)
        return {
            **stats,
            "registered_entries": len(rows),
            "active_entries": sum(1 for row in rows if row.get("status") == "有效"),
        }

    def cleanup_expired(self) -> dict[str, Any]:
        """清理过期缓存条目。"""
        rows = store.rows(self.MODULE)
        cleaned = 0
        for row in list(rows):
            if row.get("status") == "已失效":
                rows.remove(row)
                cleaned += 1
        return {"ok": True, "cleaned_entries": cleaned, "remaining": len(rows)}

    def compute_cache_turnover(self) -> dict[str, Any]:
        """计算缓存周转指标。"""
        stats = _global_cache.stats()
        return {
            "ok": True,
            "hit_rate": round(
                stats["total_hits"] / max(stats["total_hits"] + stats["expired"], 1) * 100, 2,
            ),
            "active_ratio": round(
                stats["active"] / max(stats["total"], 1) * 100, 2,
            ),
            "memory_mb": round(stats["memory_bytes"] / (1024 * 1024), 2),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        stats = _global_cache.stats()
        base["artifact"] = "结果缓存数据集"
        base["cache_hit_count"] = stats["total_hits"]
        base["aggregate_hit_rate"] = round(
            stats["total_hits"] / max(stats["total_hits"] + stats["expired"], 1) * 100, 2
        )
        return base

    # ---- 内部辅助 ----

    @staticmethod
    def _entry_covers(entry: Any, period_start: float, period_end: float) -> bool:
        """缓存条目的有效观测时段是否覆盖请求时段。"""
        result = entry.result
        valid_ranges = result.get("valid_ranges") if isinstance(result, dict) else None
        if valid_ranges is not None:
            # 空列表代表本时段无有效观测，不能判为覆盖
            return any(
                rng["start_time"] <= period_start and rng["end_time"] >= period_end
                for rng in valid_ranges
            )
        if entry.period_start is None or entry.period_end is None:
            return False
        return entry.period_start <= period_start and entry.period_end >= period_end
