<template>
  <section class="page" data-module="wrap">
    <header class="page-head">
      <div>
        <h2>杀青结算管理</h2>
        <p class="page-desc">多选结算对象批量核对，逐条校验应结金额、已付金额与结算周期，整组按通过、差额、争议标记。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="!selectedIds.length" @click="verifySelected">
          批量核对（{{ selectedIds.length }}）
        </button>
        <button
          class="btn"
          type="button"
          :disabled="!failedIds.length || verifying"
          :title="failedIds.length ? `仅重试 ${failedIds.length} 个差额/争议对象` : '暂无可重试的失败项'"
          @click="retryFailed"
        >
          只重试失败项（{{ failedIds.length }}）
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

    <section v-if="batch" class="verify-panel">
      <header class="verify-panel-head">
        <div>
          <strong>核对批次 {{ batch.batch_id }}</strong>
          <span v-if="batch.idempotent" class="replay-tag">重复提交，已回放原结果（未重复扣减）</span>
        </div>
        <p class="verify-summary">{{ batch.message }}</p>
      </header>
      <div class="verify-tags">
        <span v-for="item in resultKinds" :key="item.key" class="result-chip" :class="item.cls">
          {{ item.label }} {{ batch.summary[item.key] ?? 0 }}
        </span>
      </div>
      <table class="data-table verify-table">
        <thead>
          <tr>
            <th>结算单号</th>
            <th>结算对象</th>
            <th>核对结果</th>
            <th>核对说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in batch.results" :key="String(item.id)">
            <td>{{ item['结算单号'] ?? '—' }}</td>
            <td>{{ item['结算对象'] ?? '—' }}</td>
            <td>
              <span class="result-tag" :class="resultClass(item['核对结果'])">{{ item['核对结果'] }}</span>
            </td>
            <td>{{ item['核对说明'] }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <form class="filter-bar" @submit.prevent="reload()">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>结算状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="status in statuses" :key="status" :value="status">{{ status }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th class="check-col">
            <input
              type="checkbox"
              :checked="allPageSelected"
              :disabled="!selectableRows.length"
              @change="toggleAll"
            />
          </th>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-disputed': isFailed(row) }">
          <td class="check-col">
            <input
              v-if="row.status !== '已付清'"
              type="checkbox"
              :checked="isSelected(row)"
              @change="toggleOne(row)"
            />
            <span v-else class="lock-hint" title="已付清结算单为终态，不参与核对与改写">—</span>
          </td>
          <td v-for="column in columns" :key="column">
            <template v-if="column === '核对结果'">
              <span v-if="row[column]" class="result-tag" :class="resultClass(row[column])">{{ row[column] }}</span>
              <span v-else class="muted-text">未核对</span>
            </template>
            <template v-else-if="column === '核对说明'">
              <span :title="String(row[column] ?? '')">{{ row[column] ?? '—' }}</span>
            </template>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
          <td class="row-actions">
            <template v-if="row.status === '已付清'">
              <span class="muted-text">已付清，不可改写</span>
            </template>
            <button
              v-for="action in availableActions(row)"
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
          <td :colspan="columns.length + 2" class="empty-state">暂无杀青结算数据，可先登记结算单</td>
        </tr>
      </tbody>
    </table>

    <section class="dispute-panel">
      <header class="dispute-head">
        <h3>争议区（{{ disputeRows.length }}）</h3>
        <p class="page-desc">与列表、导出同一口径：最近一次核对被标记为争议的结算单集中在此跟进。</p>
      </header>
      <table v-if="disputeRows.length" class="data-table">
        <thead>
          <tr>
            <th>结算单号</th>
            <th>结算对象</th>
            <th>结算周期</th>
            <th>核对结果</th>
            <th>核对说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in disputeRows" :key="String(row.id)">
            <td>{{ row['结算单号'] }}</td>
            <td>{{ row['结算对象'] }}</td>
            <td>{{ row['结算周期'] || '—' }}</td>
            <td><span class="result-tag dispute">争议</span></td>
            <td>{{ row['核对说明'] }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty-state">当前没有争议结算单</p>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条杀青结算记录，已选 {{ selectedIds.length }} 个对象</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

interface VerifyItem {
  id: number
  结算单号?: string | null
  结算对象?: string | null
  结算周期?: string | null
  应结金额?: number | null
  已付金额?: number | null
  未付金额?: number | null
  核对结果: string
  核对说明: string
  status?: string | null
}

interface BatchResult {
  ok: boolean
  message: string
  batch_id: string
  idempotent: boolean
  summary: Record<string, number>
  results: VerifyItem[]
  failed_ids: number[]
}

const ENDPOINT = '/api/wrap'
const columns = ["结算单号", "结算对象", "结算周期", "应结金额", "已付金额", "未付金额", "结算人", "结算状态", "核对结果", "核对说明"]
const singleActions = ["发起核对", "确认结算", "标记争议"]
const statuses = ["待核对", "核对中", "已确认", "已付清", "有争议"]

const rows = ref<Row[]>([])
const disputeRows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = ["结算单号", "结算对象", "结算周期"]

const selectedIds = ref<number[]>([])
const batch = ref<BatchResult | null>(null)
const verifying = ref(false)
// 同一选择集合复用批次号，保证重复提交返回同一组结果、不重复扣减
let batchId = ''
let selectedSignature = ''

const failedIds = computed(() => batch.value?.failed_ids ?? [])

const selectableRows = computed(() => rows.value.filter((row) => row.status !== '已付清'))
const allPageSelected = computed(
  () => selectableRows.value.length > 0 && selectableRows.value.every((row) => isSelected(row)),
)

const resultKinds = [
  { key: '通过', label: '通过', cls: 'pass' },
  { key: '差额', label: '差额', cls: 'diff' },
  { key: '争议', label: '争议', cls: 'dispute' },
  { key: '跳过', label: '已付清跳过', cls: 'skip' },
]

const stats = computed(() => {
  const pending = rows.value.filter((row) => row.pending).length
  const disputes = disputeRows.value.length
  const passed = rows.value.filter((row) => row['核对结果'] === '通过').length
  return [
    { label: '待处理结算单', value: pending },
    { label: '已核对通过', value: passed },
    { label: '争议单数', value: disputes },
  ]
})

function isSelected(row: Row) {
  return selectedIds.value.includes(Number(row.id))
}

function isFailed(row: Row) {
  const result = row['核对结果']
  return result === '差额' || result === '争议'
}

function availableActions(row: Row) {
  if (row.status === '已付清') return []
  if (row.status === '已确认') return singleActions.filter((action) => action !== '确认结算')
  return singleActions
}

function toggleOne(row: Row) {
  const id = Number(row.id)
  if (isSelected(row)) {
    selectedIds.value = selectedIds.value.filter((item) => item !== id)
  } else {
    selectedIds.value = [...selectedIds.value, id]
  }
}

function toggleAll(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  const pageIds = selectableRows.value.map((row) => Number(row.id))
  const rest = selectedIds.value.filter((id) => !pageIds.includes(id))
  selectedIds.value = checked ? [...rest, ...pageIds] : rest
}

function resultClass(result?: string | number | null) {
  if (result === '通过') return 'pass'
  if (result === '差额') return 'diff'
  if (result === '争议') return 'dispute'
  return 'skip'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function newBatchId() {
  return `BATCH-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

async function verifySelected() {
  if (!selectedIds.value.length) {
    errorMessage.value = '请先勾选需要核对的结算对象'
    return
  }
  const ids = [...selectedIds.value]
  const signature = ids.join(',')
  if (signature !== selectedSignature) {
    selectedSignature = signature
    batchId = newBatchId()
  }
  await submitVerify({ ids, batch_id: batchId })
}

async function retryFailed() {
  if (!batch.value || !failedIds.value.length) {
    errorMessage.value = '暂无可重试的失败项'
    return
  }
  const sourceBatchId = batch.value.batch_id
  // 重试只针对上一批的差额、争议对象，使用新批次号生成独立结果
  selectedIds.value = [...failedIds.value]
  selectedSignature = selectedIds.value.join(',')
  batchId = newBatchId()
  errorMessage.value = ''
  verifying.value = true
  try {
    const response = await request(`${ENDPOINT}/batch-verify`, {
      method: 'POST',
      body: JSON.stringify({
        ids: [],
        batch_id: batchId,
        only_failed: true,
        retry_of: sourceBatchId,
      }),
    })
    if (!response.ok) {
      const data = await response.json().catch(() => null)
      throw new Error(data?.detail ?? '失败项重试未生效，请稍后重试')
    }
    batch.value = (await response.json()) as BatchResult
    await Promise.all([reload(false), loadDisputes()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '失败项重试失败'
  } finally {
    verifying.value = false
  }
}

async function submitVerify(payload: Record<string, unknown>) {
  errorMessage.value = ''
  verifying.value = true
  try {
    const response = await request(`${ENDPOINT}/batch-verify`, {
      method: 'POST',
      body: JSON.stringify(payload),
    })
    if (!response.ok) {
      const data = await response.json().catch(() => null)
      throw new Error(data?.detail ?? '批量核对未生效，请稍后重试')
    }
    batch.value = (await response.json()) as BatchResult
    await Promise.all([reload(false), loadDisputes()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量核对失败'
  } finally {
    verifying.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const data = await response.json().catch(() => null)
    if (!response.ok || data?.ok === false) {
      throw new Error(data?.detail ?? data?.message ?? '杀青结算动作未生效，请稍后重试')
    }
    await Promise.all([reload(false), loadDisputes()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '杀青结算操作失败'
  }
}

async function loadDisputes() {
  try {
    const response = await request(`${ENDPOINT}/disputes`)
    if (!response.ok) return
    const payload = await response.json()
    disputeRows.value = payload.items ?? []
  } catch {
    // 争议区读取失败不阻塞主列表
  }
}

async function reload(clearError = true) {
  if (clearError) errorMessage.value = ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters.value)) {
    if (value) params.set(key, value)
  }
  try {
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    if (!response.ok) {
      throw new Error('结算单列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    // 丢弃已不在结果集中的勾选项（如被筛选掉）
    const visibleIds = new Set(rows.value.map((row) => Number(row.id)))
    selectedIds.value = selectedIds.value.filter((id) => visibleIds.has(id))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '杀青结算列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadDisputes()
})
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
  width: 40px;
  text-align: center;
}
.muted-text {
  color: var(--muted);
  font-size: 12px;
}
.result-tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 12px;
  line-height: 18px;
}
.result-tag.pass,
.result-chip.pass {
  background: #e7f6ec;
  color: #1a7f37;
}
.result-tag.diff,
.result-chip.diff {
  background: #fff4e0;
  color: #b45309;
}
.result-tag.dispute,
.result-chip.dispute {
  background: #fde8e8;
  color: #b42318;
}
.result-tag.skip,
.result-chip.skip {
  background: #eef1f5;
  color: #64748b;
}
.verify-panel,
.dispute-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.verify-panel-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 8px;
}
.verify-summary {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
}
.replay-tag {
  margin-left: 8px;
  font-size: 12px;
  color: #1a7f37;
}
.verify-tags {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}
.result-chip {
  border-radius: 10px;
  padding: 2px 10px;
  font-size: 12px;
}
.verify-table {
  margin-bottom: 0;
}
.dispute-head h3 {
  margin: 0 0 4px;
  font-size: 15px;
}
.row-disputed {
  background: #fff8f8;
}
select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 5px 8px;
  font-size: 13px;
}
</style>
