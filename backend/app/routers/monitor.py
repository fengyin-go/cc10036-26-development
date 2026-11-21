"""异常监控路由：采样空窗检测、监控读取（串联结果缓存）、补采重算、检测台账。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api/monitor", tags=["异常监控与采样空窗"])


class GapAnalyzePayload(BaseModel):
    series: list[dict[str, Any]] | list[float] = Field(default_factory=list)
    interval: float | None = None
    period_start: float | None = None
    period_end: float | None = None


class AnalyzeSeriesPayload(BaseModel):
    rule_id: int
    series: list[dict[str, Any]] | list[float] = Field(default_factory=list)
    interval: float | None = None
    period_start: float | None = None
    period_end: float | None = None
    source: str = "实时采样"
    ttl_seconds: int = 1800


class BackfillPayload(BaseModel):
    rule_id: int
    backfill_series: list[dict[str, Any]] | list[float] = Field(default_factory=list)
    gap_start: float | None = None
    gap_end: float | None = None
    interval: float | None = None
    ttl_seconds: int = 1800


class ReadMonitoringPayload(BaseModel):
    rule_id: int
    period_start: float
    period_end: float


class EffectiveDetectionPayload(BaseModel):
    rule_id: int
    period_start: float
    period_end: float
    series: list[dict[str, Any]] | list[float] = Field(default_factory=list)
    interval: float | None = None


@router.post("/gaps")
def analyze_gaps(payload: GapAnalyzePayload) -> dict[str, Any]:
    """按指标序列计算空窗检测数据，分辨信号平稳与无人上报。"""
    svc = service_for("anomaly_detect")
    if svc is None:
        return {"ok": False, "message": "异常检测服务未就绪"}
    report = svc.analyze_gaps(
        payload.series, interval=payload.interval,
        period_start=payload.period_start, period_end=payload.period_end,
    )
    return {"ok": True, "gap_report": report}


@router.post("/analyze")
def analyze_series(payload: AnalyzeSeriesPayload) -> dict[str, Any]:
    """空窗检测 + 异常检测流水线，检测记录带来源时间并写入结果缓存。"""
    svc = service_for("anomaly_detect")
    if svc is None:
        return {"ok": False, "message": "异常检测服务未就绪"}
    return svc.analyze_series(
        payload.rule_id, payload.series,
        interval=payload.interval,
        period_start=payload.period_start,
        period_end=payload.period_end,
        source=payload.source,
        ttl_seconds=payload.ttl_seconds,
    )


@router.post("/backfill")
def backfill_and_rerun(payload: BackfillPayload) -> dict[str, Any]:
    """采样中断重试：按补采时段重算检测数据。"""
    svc = service_for("anomaly_detect")
    if svc is None:
        return {"ok": False, "message": "异常检测服务未就绪"}
    return svc.backfill_and_rerun(
        payload.rule_id, payload.backfill_series,
        gap_start=payload.gap_start, gap_end=payload.gap_end,
        interval=payload.interval, ttl_seconds=payload.ttl_seconds,
    )


@router.post("/read")
def read_monitoring(payload: ReadMonitoringPayload) -> dict[str, Any]:
    """值班读取告警：经缓存读取本时段有效检测，历史读值带来源时间。"""
    svc = service_for("anomaly_detect")
    if svc is None:
        return {"ok": False, "message": "异常检测服务未就绪"}
    return svc.read_monitoring(
        payload.rule_id,
        period_start=payload.period_start, period_end=payload.period_end,
    )


@router.get("/records")
def list_records(rule_id: int | None = None) -> dict[str, Any]:
    """检测记录台账：交班时不同来源的检测记录同时可见。"""
    svc = service_for("anomaly_detect")
    if svc is None:
        return {"ok": False, "message": "异常检测服务未就绪"}
    return svc.list_detection_records(rule_id)


@router.post("/effective-detection")
def read_effective_detection(payload: EffectiveDetectionPayload) -> dict[str, Any]:
    """缓存侧入口：按空窗检测数据读取本时段有效检测。"""
    anomaly_svc = service_for("anomaly_detect")
    cache_svc = service_for("result_cache")
    if anomaly_svc is None or cache_svc is None:
        return {"ok": False, "message": "依赖服务未就绪"}
    gap_report = anomaly_svc.analyze_gaps(
        payload.series, interval=payload.interval,
        period_start=payload.period_start,
        period_end=payload.period_end,
    )
    return cache_svc.read_effective_detection(payload.rule_id, gap_report)
