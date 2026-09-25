<template>
  <section class="page" data-module="wrap">
    <header class="page-head">
      <div>
        <h2>杀青结算管理</h2>
        <p class="page-desc">多选结算对象批量核对，逐条校验应结金额、已付金额与结算周期，整组标记通过、差额与争议。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="!selectedIds.length || checking" @click="runBatchCheck">
          {{ checking ? '核对中…' : `批量核对（${selectedIds.length}）` }}
        </button>
        <button class="btn" type="button" @click="exportRows">导出杀青结算清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <!-- 批量核对结果：整组汇总 + 逐条标记，重试/重交都只看同一份结果 -->
    <section v-if="batchResult" class="batch-panel">
      <div class="batch-head">
        <div>
          <strong>核对批次 {{ batchResult.batch_no }}</strong>
          <span class="batch-round">第 {{ batchResult.round }} 轮</span>
        </div>
        <div class="batch-summary">
          <span v-for="(value, key) in batchResult.summary" :key="key" class="summary-chip" :class="chipClass(String(key))">
            {{ key }} {{ value }}
          </span>
        </div>
      </div>
      <p class="batch-message" :class="{ 'replay-text': batchResult.replayed }">{{ batchResult.message }}</p>
      <div class="batch-actions">
        <button class="btn" type="button" :disabled="checking || !batchResult.failed_ids.length" @click="retryFailed">
          只重试失败项（{{ batchResult.failed_ids.length }}）
        </button>
        <button class="btn ghost" type="button" :disabled="checking" @click="resubmitSame">
          整组再提交（验证不重复扣减）
        </button>
        <button class="link" type="button" @click="batchResult = null">收起结果</button>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>结算单号</th>
            <th>结算对象</th>
            <th>应结金额</th>
            <th>已付金额</th>
            <th>核对结果</th>
            <th>核对差额</th>
            <th>扣减额</th>
            <th>轮次</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in batchResult.items" :key="String(item.id)" :class="rowClass(item.核对结果)">
            <td>{{ item.结算单号 ?? `#${item.id}` }}</td>
            <td>{{ item.结算对象 ?? '—' }}</td>
            <td>{{ formatAmount(item.应结金额) }}</td>
            <td>{{ formatAmount(item.已付金额) }}</td>
            <td><span class="result-tag" :class="tagClass(item.核对结果)">{{ item.核对结果 }}</span></td>
            <td>{{ item.核对差额 == null ? '—' : formatAmount(item.核对差额) }}</td>
            <td>{{ formatAmount(item.扣减额) }}</td>
            <td>{{ item.核对轮次 }}</td>
            <td>{{ item.核对说明 }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th class="check-col">
            <input
              type="checkbox"
              :checked="allSelected"
              :disabled="!selectableRows.length"
              @change="toggleAll"
            />
          </th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>核对结果</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="rowClass(String(row.核对结果 ?? ''))">
          <td class="check-col">
            <input
              type="checkbox"
              :value="Number(row.id)"
              v-model="selectedIds"
              :disabled="row.status === '已付清'"
              :title="row.status === '已付清' ? '已付清结算单不能被改写' : ''"
            />
          </td>
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span v-if="row.核对结果" class="result-tag" :class="tagClass(String(row.核对结果))">{{ row.核对结果 }}</span>
            <span v-else class="muted-text">未核对</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 3" class="empty-state">暂无杀青结算数据，可先登记结算单</td>
        </tr>
      </tbody>
    </table>

    <!-- 争议区：与列表同一份核对结果，仅取「争议」标记 -->
    <section class="dispute-panel">
      <h3>争议区（核对结果为「争议」的结算单）</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>结算单号</th>
            <th>结算对象</th>
            <th>结算周期</th>
            <th>应结金额</th>
            <th>已付金额</th>
            <th>核对差额</th>
            <th>核对时间</th>
            <th>争议原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in disputeRows" :key="String(row.id)" class="row-dispute">
            <td>{{ row.结算单号 }}</td>
            <td>{{ row.结算对象 }}</td>
            <td>{{ row.结算周期 }}</td>
            <td>{{ formatAmount(row.应结金额) }}</td>
            <td>{{ formatAmount(row.已付金额) }}</td>
            <td>{{ row.核对差额 == null ? '—' : formatAmount(row.核对差额) }}</td>
            <td>{{ row.核对时间 ?? '—' }}</td>
            <td>{{ row.核对说明 }}</td>
          </tr>
          <tr v-if="!disputeRows.length">
            <td colspan="8" class="empty-state">暂无争议结算单</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条杀青结算记录 · 导出清单含核对结果，与本页同口径</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type BatchItem = Row & {
  核对结果: string
  核对差额: number | null
  扣减额: number
  核对轮次: number
  核对说明: string
}
type BatchResult = {
  ok: boolean
  message: string
  batch_no: string
  round: number
  replayed: boolean
  summary: Record<string, number>
  items: BatchItem[]
  failed_ids: number[]
}

const ENDPOINT = '/api/wrap'
const columns = ["结算单号", "结算对象", "结算周期", "应结金额", "已付金额", "未付金额", "结算人", "结算状态"]
const actions = ["发起核对", "确认结算", "标记争议"]
const FAIL_RESULTS = ['差额', '争议', '缺失']

const rows = ref<Row[]>([])
const disputeRows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const selectedIds = ref<number[]>([])
const checking = ref(false)
const batchResult = ref<BatchResult | null>(null)

const selectableRows = computed(() => rows.value.filter((row) => row.status !== '已付清'))
const allSelected = computed(
  () => selectableRows.value.length > 0
    && selectableRows.value.every((row) => selectedIds.value.includes(Number(row.id))),
)

const stats = computed(() => {
  const pending = rows.value.filter((row) => row.status === '待核对').length
  const dispute = disputeRows.value.length
  const payable = rows.value.reduce((sum, row) => sum + (toNumber(row.应结金额) ?? 0), 0)
  return [
    { label: '待核对结算单', value: pending },
    { label: '争议单数', value: dispute },
    { label: '应结金额合计', value: formatAmount(payable) },
  ]
})

function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const num = Number(String(value).replace(/[,，元]/g, ''))
  return Number.isFinite(num) ? num : null
}

function formatAmount(value: unknown): string {
  const num = toNumber(value)
  return num === null ? '—' : num.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

function tagClass(result: string): string {
  return {
    '通过': 'tag-pass',
    '差额': 'tag-diff',
    '争议': 'tag-dispute',
    '缺失': 'tag-missing',
    '已锁定': 'tag-locked',
  }[result] ?? ''
}

function rowClass(result: string): string {
  return {
    '通过': 'row-pass',
    '差额': 'row-diff',
    '争议': 'row-dispute',
    '缺失': 'row-missing',
    '已锁定': 'row-locked',
  }[result] ?? ''
}

function chipClass(key: string): string {
  if (key === '通过' || key === '扣减额') return 'chip-pass'
  if (FAIL_RESULTS.includes(key) || key === '失败项') return 'chip-fail'
  return ''
}

function toggleAll(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  selectedIds.value = checked
    ? selectableRows.value.map((row) => Number(row.id))
    : selectedIds.value.filter((id) => !selectableRows.value.some((row) => Number(row.id) === id))
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  // 导出走全量列表，行上已带核对结果字段，与列表、争议区同源。
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runBatchCheck() {
  await submitBatch(selectedIds.value, batchResult.value?.batch_no)
}

async function retryFailed() {
  if (!batchResult.value) return
  const failedIds = [...batchResult.value.failed_ids]
  selectedIds.value = failedIds
  await submitBatch(failedIds, batchResult.value.batch_no)
}

async function resubmitSame() {
  if (!batchResult.value) return
  const ids = batchResult.value.items.map((item) => Number(item.id))
  selectedIds.value = ids
  await submitBatch(ids, batchResult.value.batch_no)
}

async function submitBatch(entryIds: number[], batchNo?: string) {
  errorMessage.value = ''
  checking.value = true
  try {
    const response = await request(`${ENDPOINT}/batch-check`, {
      method: 'POST',
      body: JSON.stringify({ entry_ids: entryIds, batch_no: batchNo ?? null }),
    })
    if (!response.ok) {
      const detail = await response.json().catch(() => null)
      throw new Error(detail?.detail ?? '批量核对未生效，请稍后重试')
    }
    batchResult.value = (await response.json()) as BatchResult
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量核对失败'
  } finally {
    checking.value = false
  }
}

async function runAction(action: string, row: Row) {
  if (row.status === '已付清') {
    errorMessage.value = '结算单已付清，不能再改写状态'
    return
  }
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || payload?.ok === false) {
      throw new Error(payload?.detail ?? payload?.message ?? '杀青结算动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '杀青结算操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listResponse, disputeResponse] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}?result=${encodeURIComponent('争议')}&size=200`),
    ])
    if (!listResponse.ok || !disputeResponse.ok) {
      throw new Error('结算单列表读取失败')
    }
    const payload = await listResponse.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    const disputePayload = await disputeResponse.json()
    disputeRows.value = disputePayload.items ?? []
    // 已付清/已不存在的勾选自动剔除
    selectedIds.value = selectedIds.value.filter((id) =>
      rows.value.some((row) => Number(row.id) === id && row.status !== '已付清'),
    )
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '杀青结算列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.page-actions {
  display: flex;
  gap: 8px;
}
.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.check-col {
  width: 36px;
  text-align: center;
}
.batch-panel,
.dispute-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin: 12px 0;
}
.dispute-panel h3 {
  margin: 0 0 10px;
  font-size: 15px;
}
.batch-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.batch-round {
  margin-left: 8px;
  font-size: 12px;
  color: var(--muted);
}
.batch-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.summary-chip {
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
  background: #eef2f7;
  color: #334155;
}
.summary-chip.chip-pass {
  background: #e7f6ec;
  color: #1a7f37;
}
.summary-chip.chip-fail {
  background: #fdecec;
  color: #b42318;
}
.batch-message {
  margin: 8px 0;
  font-size: 13px;
  color: #334155;
}
.batch-message.replay-text,
.replay-text {
  color: #1a7f37;
}
.batch-actions {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 10px;
}
.result-tag {
  display: inline-block;
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 4px;
}
.tag-pass {
  background: #e7f6ec;
  color: #1a7f37;
}
.tag-diff {
  background: #fff4e0;
  color: #b25e09;
}
.tag-dispute {
  background: #fdecec;
  color: #b42318;
}
.tag-missing {
  background: #eef2f7;
  color: #475569;
}
.tag-locked {
  background: #e2e8f0;
  color: #64748b;
}
.muted-text {
  color: var(--muted);
  font-size: 12px;
}
.row-pass td {
  background: #f6fdf9;
}
.row-diff td {
  background: #fffaf2;
}
.row-dispute td {
  background: #fef5f5;
}
.row-missing td {
  background: #f8fafc;
}
.row-locked td {
  background: #f8fafc;
  color: var(--muted);
}
</style>
