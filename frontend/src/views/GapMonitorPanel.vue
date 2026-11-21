<template>
  <section class="gap-panel" data-testid="gap-monitor-panel">
    <header class="gap-head">
      <h3>采样空窗监控</h3>
      <p class="page-desc">
        按指标时序计算空窗检测数据，分辨「信号平稳」与「无人上报」；读取告警时经结果缓存只取本时段有效检测。
      </p>
    </header>

    <div class="gap-controls">
      <label class="gap-field">
        <span>检测规则</span>
        <select v-model.number="ruleId" data-testid="gap-rule">
          <option v-for="row in rules" :key="String(row.id)" :value="Number(row.id)">
            {{ row.id }} · {{ row['规则名称'] }}（{{ row['监控指标'] }}）
          </option>
        </select>
      </label>
      <label class="gap-field">
        <span>采样间隔</span>
        <input v-model.number="interval" type="number" min="1" data-testid="gap-interval" />
      </label>
      <label class="gap-field">
        <span>本时段起</span>
        <input v-model.number="periodStart" type="number" data-testid="gap-period-start" />
      </label>
      <label class="gap-field">
        <span>本时段止</span>
        <input v-model.number="periodEnd" type="number" data-testid="gap-period-end" />
      </label>
    </div>

    <div class="gap-controls">
      <label class="gap-field gap-series">
        <span>时序点（每行一个：时间戳,数值；留空行模拟无人上报）</span>
        <textarea
          v-model="seriesText"
          rows="6"
          data-testid="gap-series"
          placeholder="10,100&#10;11,101&#10;12,102"
        ></textarea>
      </label>
    </div>

    <div class="gap-actions">
      <button class="btn" type="button" data-testid="gap-analyze" @click="onAnalyzeGaps">空窗检测</button>
      <button class="btn primary" type="button" data-testid="gap-detect" @click="onAnalyzeSeries">
        检测并写入缓存
      </button>
      <button class="btn" type="button" data-testid="gap-read" @click="onReadMonitoring">
        从缓存读告警
      </button>
      <button class="btn primary" type="button" data-testid="gap-backfill" @click="onBackfill">
        补采并重算
      </button>
      <button class="btn ghost" type="button" data-testid="gap-load-records" @click="loadRecords">
        刷新交班台账
      </button>
    </div>

    <p v-if="errorMessage" class="error-text" data-testid="gap-error">{{ errorMessage }}</p>

    <div v-if="gapReport" class="stat-row" data-testid="gap-summary">
      <article class="stat-card">
        <span class="stat-label">信号状态</span>
        <strong class="stat-value" :data-state="gapReport.signal_state">{{ gapReport.signal_state }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">期望/实采/缺测</span>
        <strong class="stat-value">
          {{ gapReport.expected_points }} / {{ gapReport.observed_points }} / {{ gapReport.missing_points }}
        </strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">采样覆盖率</span>
        <strong class="stat-value">{{ (gapReport.coverage_rate * 100).toFixed(1) }}%</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">是否平稳</span>
        <strong class="stat-value">{{ gapReport.is_stable ? '平稳' : '非平稳' }}</strong>
      </article>
    </div>

    <div v-if="gapReport && gapReport.gap_windows.length" class="gap-windows" data-testid="gap-windows">
      <h4>空窗区间（缺测不能当零异常）</h4>
      <table class="data-table">
        <thead>
          <tr><th>空窗起</th><th>空窗止</th><th>缺测点数</th><th>来源判定</th></tr>
        </thead>
        <tbody>
          <tr v-for="(w, i) in gapReport.gap_windows" :key="i">
            <td>{{ w.start_time }}</td>
            <td>{{ w.end_time }}</td>
            <td>{{ w.expected_points }}</td>
            <td>{{ w.source }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="readResult" class="gap-read" data-testid="gap-read-result">
      <h4>缓存读取裁决</h4>
      <table class="data-table">
        <tbody>
          <tr><th>能否作为当前观测</th><td :data-usable="String(readResult.usable_as_current)">
            {{ readResult.usable_as_current ? '可以（本时段有效检测）' : '不可以（缺测或历史读值）' }}
          </td></tr>
          <tr><th>数据来源</th><td>{{ readResult.source ?? '—' }}</td></tr>
          <tr><th>来源时间</th><td>{{ formatTime(Number(readResult.source_time)) }}</td></tr>
          <tr><th>信号状态</th><td>{{ readResult.signal_state ?? '—' }}</td></tr>
          <tr><th>异常数</th><td>{{ readResult.anomaly_count ?? 0 }}</td></tr>
          <tr><th>说明</th><td>{{ readResult.message ?? '—' }}</td></tr>
        </tbody>
      </table>
    </div>

    <div class="gap-ledger" data-testid="gap-ledger">
      <h4>检测记录台账（交班时不同来源同时可见）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>#</th><th>规则</th><th>来源</th><th>来源时间</th><th>时段</th>
            <th>信号</th><th>缺测</th><th>异常</th><th>当前可用</th><th>取代记录</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in records" :key="r.record_id" :data-source="r.source">
            <td>{{ r.record_id }}</td>
            <td>{{ r.rule_name }}</td>
            <td>{{ r.source }}</td>
            <td>{{ formatTime(r.source_time) }}</td>
            <td>{{ r.period_start }} ~ {{ r.period_end }}</td>
            <td>{{ r.signal_state }}</td>
            <td>{{ r.missing_points }}</td>
            <td>{{ r.anomaly_count }}</td>
            <td>{{ r.usable_as_current ? '是' : '否' }}</td>
            <td>{{ r.replaced_record_id ?? '—' }}</td>
          </tr>
          <tr v-if="!records.length">
            <td colspan="10" class="empty-state">暂无检测记录</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  analyzeGaps,
  analyzeSeries,
  backfillAndRerun,
  listDetectionRecords,
  readMonitoring,
  type DetectionRecord,
  type GapReport,
  type SeriesPoint,
} from '@/api/backend-service'
import { listEntries } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const rules = ref<EntryRow[]>([])
const ruleId = ref(1)
const interval = ref(1)
const periodStart = ref(10)
const periodEnd = ref(19)
const seriesText = ref('')
const gapReport = ref<GapReport | null>(null)
const readResult = ref<Record<string, unknown> | null>(null)
const records = ref<DetectionRecord[]>([])
const errorMessage = ref('')

function parseSeries(): SeriesPoint[] {
  return seriesText.value
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [ts, val] = line.split(',').map((part) => part.trim())
      return { timestamp: Number(ts), value: val === '' || val == null ? null : Number(val) }
    })
}

function period() {
  return {
    interval: interval.value,
    period_start: periodStart.value,
    period_end: periodEnd.value,
  }
}

function fail(error: unknown) {
  errorMessage.value = error instanceof Error ? error.message : '监控操作失败'
}

async function onAnalyzeGaps() {
  errorMessage.value = ''
  try {
    const result = await analyzeGaps(parseSeries(), period())
    gapReport.value = result.gap_report
  } catch (error) {
    fail(error)
  }
}

async function onAnalyzeSeries() {
  errorMessage.value = ''
  try {
    const record = await analyzeSeries(ruleId.value, parseSeries(), period())
    gapReport.value = {
      interval: interval.value,
      period_start: record.period_start,
      period_end: record.period_end,
      expected_points: record.expected_points,
      observed_points: record.data_points,
      total_input_points: record.data_points,
      missing_points: record.missing_points,
      coverage_rate: record.coverage_rate,
      has_gap: record.has_gap,
      is_fully_sampled: record.is_fully_sampled,
      is_stable: record.is_stable,
      signal_state: record.signal_state,
      gap_windows: record.gap_windows,
      valid_ranges: record.valid_ranges,
      observed_values: [],
      value_std: 0,
      value_mean: 0,
    }
    await loadRecords()
  } catch (error) {
    fail(error)
  }
}

async function onReadMonitoring() {
  errorMessage.value = ''
  try {
    readResult.value = await readMonitoring(ruleId.value, {
      period_start: periodStart.value,
      period_end: periodEnd.value,
    })
  } catch (error) {
    fail(error)
  }
}

async function onBackfill() {
  errorMessage.value = ''
  try {
    const record = await backfillAndRerun(ruleId.value, parseSeries(), {
      gap_start: periodStart.value,
      gap_end: periodEnd.value,
      interval: interval.value,
    })
    gapReport.value = {
      interval: interval.value,
      period_start: record.period_start,
      period_end: record.period_end,
      expected_points: record.expected_points,
      observed_points: record.data_points,
      total_input_points: record.data_points,
      missing_points: record.missing_points,
      coverage_rate: record.coverage_rate,
      has_gap: record.has_gap,
      is_fully_sampled: record.is_fully_sampled,
      is_stable: record.is_stable,
      signal_state: record.signal_state,
      gap_windows: record.gap_windows,
      valid_ranges: record.valid_ranges,
      observed_values: [],
      value_std: 0,
      value_mean: 0,
    }
    await loadRecords()
  } catch (error) {
    fail(error)
  }
}

async function loadRecords() {
  errorMessage.value = ''
  try {
    const result = await listDetectionRecords()
    records.value = result.items
  } catch (error) {
    fail(error)
  }
}

function formatTime(ts: number): string {
  if (!ts) return '—'
  // 演示数据常用自定义时间戳，原样保留并附带可读时间（大于 1e9 视为 epoch）
  const readable = ts > 1_000_000_000 ? new Date(ts * 1000).toLocaleString() : String(ts)
  return `${ts}（${readable}）`
}

onMounted(async () => {
  try {
    const page = await listEntries('anomaly_detect', {})
    rules.value = page.items
    await loadRecords()
  } catch (error) {
    fail(error)
  }
})
</script>

<style scoped>
.gap-panel {
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
  margin: 18px 0;
}
.gap-head h3 { margin: 0 0 4px; }
.gap-controls { display: flex; flex-wrap: wrap; gap: 12px; margin: 12px 0; }
.gap-field { display: flex; flex-direction: column; font-size: 12px; color: var(--muted); gap: 4px; }
.gap-field input,
.gap-field select { min-width: 140px; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.gap-series { flex: 1; min-width: 100%; }
.gap-series textarea { width: 100%; font-family: monospace; padding: 8px; border: 1px solid var(--border); border-radius: 6px; }
.gap-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.gap-windows,
.gap-read,
.gap-ledger { margin-top: 16px; }
.gap-windows h4,
.gap-read h4,
.gap-ledger h4 { margin: 0 0 8px; }
.stat-value[data-state='缺测'] { color: #b91c1c; }
.stat-value[data-state='断采'] { color: #d97706; }
.stat-value[data-state='平稳'] { color: #047857; }
</style>
