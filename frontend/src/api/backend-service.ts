import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 采样空窗与异常监控 ----

export type SeriesPoint = { timestamp: number; value: number | null }

export type GapReport = {
  interval: number
  period_start: number
  period_end: number
  expected_points: number
  observed_points: number
  total_input_points: number
  missing_points: number
  coverage_rate: number
  has_gap: boolean
  is_fully_sampled: boolean
  is_stable: boolean
  signal_state: string
  gap_windows: { start_time: number; end_time: number; expected_points: number; source: string }[]
  valid_ranges: { start_time: number; end_time: number }[]
  observed_values: number[]
  value_std: number
  value_mean: number
}

export type DetectionRecord = {
  record_id: number
  rule_id: number
  rule_name: string
  monitoring_metric: string
  algorithm: string
  source: string
  source_time: number
  period_start: number
  period_end: number
  valid_ranges: { start_time: number; end_time: number }[]
  data_points: number
  expected_points: number
  missing_points: number
  coverage_rate: number
  has_gap: boolean
  is_fully_sampled: boolean
  is_stable: boolean
  signal_state: string
  gap_windows: GapReport['gap_windows']
  anomaly_count: number
  anomalies: Record<string, number | string>[]
  alert_level: string
  replaced_record_id: number | null
  usable_as_current: boolean
  cache_key: string
}

async function postMonitor<T>(path: string, body: Record<string, unknown>): Promise<T> {
  const response = await request(`/api/monitor${path}`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error('监控接口请求失败')
  return (await response.json()) as T
}

export async function analyzeGaps(
  series: SeriesPoint[] | number[],
  period: { interval?: number; period_start?: number; period_end?: number } = {},
): Promise<{ ok: boolean; gap_report: GapReport }> {
  return postMonitor('/gaps', { series, ...period })
}

export async function analyzeSeries(
  ruleId: number,
  series: SeriesPoint[] | number[],
  period: { interval?: number; period_start?: number; period_end?: number; source?: string } = {},
): Promise<DetectionRecord> {
  return postMonitor('/analyze', { rule_id: ruleId, series, ...period })
}

export async function backfillAndRerun(
  ruleId: number,
  backfillSeries: SeriesPoint[] | number[],
  period: { gap_start?: number; gap_end?: number; interval?: number } = {},
): Promise<DetectionRecord> {
  return postMonitor('/backfill', {
    rule_id: ruleId,
    backfill_series: backfillSeries,
    ...period,
  })
}

export async function readMonitoring(
  ruleId: number,
  period: { period_start: number; period_end: number },
): Promise<Record<string, unknown>> {
  return postMonitor('/read', { rule_id: ruleId, ...period })
}

export async function readEffectiveDetection(
  ruleId: number,
  series: SeriesPoint[] | number[],
  period: { interval?: number; period_start: number; period_end: number },
): Promise<Record<string, unknown>> {
  return postMonitor('/effective-detection', { rule_id: ruleId, series, ...period })
}

export async function listDetectionRecords(
  ruleId?: number,
): Promise<{ ok: boolean; total: number; items: DetectionRecord[] }> {
  const query = ruleId ? `?rule_id=${ruleId}` : ''
  const response = await request(`/api/monitor/records${query}`)
  if (!response.ok) throw new Error('检测台账读取失败')
  return await response.json()
}
