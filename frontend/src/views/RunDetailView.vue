<template>
  <div class="page" v-if="run">
    <div style="display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap">
      <div>
        <h1 style="margin-bottom: 4px">{{ run.name }}</h1>
        <p class="muted" style="margin-top: 0">
          {{ run.project }} ·
          <n-tag size="small" :type="statusType">{{ statusLabel }}</n-tag>
          · version {{ run.version }}
        </p>
      </div>
      <div style="display: flex; gap: 8px">
        <n-button @click="$router.push(`/runs/${run.id}/events`)">事件时间线</n-button>
        <n-button @click="$router.push(`/runs/${run.id}/lineage`)">血缘</n-button>
      </div>
    </div>

    <div class="card" style="margin-bottom: 16px">
      <div class="grid-2">
        <div>
          <div class="muted">dataset_content_sha256</div>
          <div class="mono">{{ run.dataset_content_sha256 }}</div>
        </div>
        <div>
          <div class="muted">code_commit_sha</div>
          <div class="mono">{{ run.code_commit_sha }}</div>
        </div>
        <div>
          <div class="muted">started_by / started_at</div>
          <div>{{ run.started_by }} · {{ formatTime(run.started_at) }}</div>
        </div>
        <div>
          <div class="muted">finished_at</div>
          <div>{{ run.finished_at ? formatTime(run.finished_at) : '—' }}</div>
        </div>
      </div>
      <p v-if="run.description" style="margin-top: 12px">{{ run.description }}</p>
      <p v-if="run.result_summary"><strong>结果：</strong>{{ run.result_summary }}</p>
      <p v-if="run.abort_reason"><strong>中止原因：</strong>{{ run.abort_reason }}</p>
    </div>

    <div class="grid-2" style="margin-bottom: 16px">
      <div class="card">
        <h3 style="margin-top: 0">指标（投影）</h3>
        <n-data-table
          size="small"
          :columns="metricCols"
          :data="run.metrics_json || []"
          :bordered="false"
        />
      </div>
      <div class="card">
        <h3 style="margin-top: 0">产物（投影）</h3>
        <n-data-table
          size="small"
          :columns="artifactCols"
          :data="run.artifacts_json || []"
          :bordered="false"
        />
      </div>
    </div>

    <n-alert
      v-if="conflict && conflict.kind === 'version_conflict'"
      type="warning"
      title="版本冲突：页面上的投影版本已过期"
      :bordered="false"
      style="margin-bottom: 16px"
    >
      <div>
        「{{ conflict.label }}」未写入：你基于版本
        <b>{{ conflict.staleVersion }}</b> 提交，但服务器当前已是版本
        <b>{{ conflict.serverVersion }}</b>。投影版本号已自动刷新，表单内容已保留，
        请先核对他人刚写入的指标/产物，再用新版本重试。
      </div>
      <template #action>
        <n-button size="small" type="warning" :loading="busy" @click="retryLast">
          用版本 {{ conflict.serverVersion }} 重试
        </n-button>
        <n-button size="small" quaternary style="margin-left: 8px" @click="dismissConflict">
          知道了
        </n-button>
      </template>
    </n-alert>

    <n-alert
      v-else-if="conflict && conflict.kind === 'terminal_state'"
      type="error"
      title="Run 已在他处结束，命令无法写入"
      :bordered="false"
      style="margin-bottom: 16px"
    >
      <div>
        「{{ conflict.label }}」未写入：该 Run 已被其他操作置为终态（当前状态：{{ statusLabel }}，
        版本 {{ conflict.serverVersion }}），刷新版本也不能再提交。
      </div>
      <template #action>
        <n-button size="small" quaternary @click="dismissConflict">知道了</n-button>
      </template>
    </n-alert>

    <div v-if="canWrite" class="card">
      <h3 style="margin-top: 0">命令操作区（乐观锁 expected_version = {{ run.version }}）</h3>
      <div class="grid-2">
        <div>
          <h4>RecordMetric</h4>
          <n-input v-model:value="metric.name" placeholder="指标名" style="margin-bottom: 8px" />
          <n-input-number v-model:value="metric.value" style="width: 100%; margin-bottom: 8px" />
          <n-input-number v-model:value="metric.step" :min="0" style="width: 100%; margin-bottom: 8px" />
          <n-button type="primary" :loading="busy" @click="doMetric">记录指标</n-button>
        </div>
        <div>
          <h4>AttachArtifact</h4>
          <n-input v-model:value="artifact.name" placeholder="产物名" style="margin-bottom: 8px" />
          <n-input v-model:value="artifact.uri" placeholder="URI" style="margin-bottom: 8px" />
          <n-input v-model:value="artifact.content_sha256" placeholder="content sha256" class="mono" style="margin-bottom: 8px" />
          <n-button text type="primary" @click="artifact.content_sha256 = randomHex(32)">随机指纹</n-button>
          <div style="margin-top: 8px">
            <n-button type="primary" :loading="busy" @click="doArtifact">挂载产物</n-button>
          </div>
        </div>
      </div>
      <div style="margin-top: 20px; display: flex; gap: 12px; flex-wrap: wrap; align-items: flex-end">
        <div style="flex: 1; min-width: 220px">
          <n-input v-model:value="completeSummary" type="textarea" placeholder="完成摘要" :rows="2" />
        </div>
        <n-button type="success" :loading="busy" @click="doComplete">CompleteRun</n-button>
        <div style="flex: 1; min-width: 220px">
          <n-input v-model:value="abortReason" type="textarea" placeholder="中止原因" :rows="2" />
        </div>
        <n-button type="warning" :loading="busy" @click="doAbort">AbortRun</n-button>
      </div>
    </div>
    <div v-else class="card muted">
      当前为只读视图{{ auth.role === 'auditor' ? '（审计员）' : '' }}：仅研究员对「进行中」的 Run 可发送命令。
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useMessage } from 'naive-ui'
import {
  abortRun,
  attachArtifact,
  completeRun,
  getRun,
  recordMetric,
} from '../api/client'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const auth = useAuthStore()
const message = useMessage()
const run = ref(null)
const busy = ref(false)
const completeSummary = ref('')
const abortReason = ref('')
// 最近一次版本冲突状态；null 表示无冲突。区别于普通参数错误（走 message.error 浮层）。
const conflict = ref(null)
// 最近一次成功捕获的命令描述符，供冲突后“用新版本重试”。
const lastCommand = ref(null)

const metric = reactive({ name: 'loss', value: 0.5, step: 1 })
const artifact = reactive({
  name: 'checkpoint.pt',
  uri: 's3://lab-artifacts/checkpoint.pt',
  content_sha256: '',
  media_type: 'application/octet-stream',
})

const canWrite = computed(() => auth.role === 'researcher' && run.value?.status === 'running')
const statusLabel = computed(() => {
  const m = { running: '进行中', completed: '已完成', aborted: '已中止' }
  return m[run.value?.status] || run.value?.status
})
const statusType = computed(() => {
  const m = { running: 'info', completed: 'success', aborted: 'warning' }
  return m[run.value?.status] || 'default'
})

const metricCols = [
  { title: 'name', key: 'name' },
  { title: 'value', key: 'value' },
  { title: 'step', key: 'step' },
]
const artifactCols = [
  { title: 'name', key: 'name' },
  { title: 'uri', key: 'uri', ellipsis: { tooltip: true } },
]

function formatTime(v) {
  return v ? new Date(v).toLocaleString() : '—'
}

function randomHex(n) {
  const bytes = new Uint8Array(n)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}

async function load() {
  run.value = await getRun(route.params.id)
}

// 命令表：版本号在真正发出时才读取，冲突自动刷新投影后，重试天然带上新版本。
// 每个命令携带 label，用于冲突告警与成功提示。
const COMMANDS = {
  metric: () => ({
    label: '记录指标',
    exec: (version) =>
      recordMetric(run.value.id, {
        name: metric.name,
        value: metric.value,
        step: metric.step,
        expected_version: version,
      }),
  }),
  artifact: () => ({
    label: '挂载产物',
    exec: (version) =>
      attachArtifact(run.value.id, { ...artifact, expected_version: version }),
  }),
  complete: () => ({
    label: '完成 Run',
    exec: (version) =>
      completeRun(run.value.id, {
        result_summary: completeSummary.value,
        expected_version: version,
      }),
  }),
  abort: () => ({
    label: '中止 Run',
    exec: (version) =>
      abortRun(run.value.id, {
        reason: abortReason.value,
        expected_version: version,
      }),
  }),
}

async function runCommand(kind) {
  const descriptor = COMMANDS[kind]()
  lastCommand.value = { kind, label: descriptor.label }
  conflict.value = null
  busy.value = true
  try {
    await descriptor.exec(run.value.version)
    message.success(`「${descriptor.label}」已接受，投影已更新`)
    await load()
    if (kind === 'metric') metric.step += 1
  } catch (e) {
    if (e?.isConflict) {
      // 409：与普通参数错误明确区分。自动刷新投影版本号后，允许带新版本再提交。
      const staleVersion = e.expectedVersion ?? run.value.version
      await refreshAfterConflict(staleVersion)
      conflict.value = {
        kind: e.errorCode === 'terminal_state' ? 'terminal_state' : 'version_conflict',
        label: descriptor.label,
        staleVersion,
        serverVersion: run.value?.version ?? e.currentVersion ?? staleVersion,
      }
    } else {
      // 400/422 等普通参数错误：浮层提示，不改动投影版本。
      message.error(e.message || '命令失败')
    }
  } finally {
    busy.value = false
  }
}

async function refreshAfterConflict(staleVersion) {
  try {
    await load()
    message.info(
      `检测到版本冲突，已自动刷新：版本 ${staleVersion} → ${run.value?.version}`,
    )
  } catch (e) {
    message.error(e.message || '冲突后刷新投影失败，请手动刷新页面')
  }
}

async function retryLast() {
  if (!lastCommand.value || !run.value) return
  const descriptor = COMMANDS[lastCommand.value.kind]()
  conflict.value = null
  busy.value = true
  try {
    await descriptor.exec(run.value.version)
    message.success(`「${descriptor.label}」已用新版本 ${run.value.version} 写入成功`)
    await load()
    if (lastCommand.value.kind === 'metric') metric.step += 1
    lastCommand.value = null
  } catch (e) {
    if (e?.isConflict) {
      const staleVersion = e.expectedVersion ?? run.value.version
      await refreshAfterConflict(staleVersion)
      conflict.value = {
        kind: e.errorCode === 'terminal_state' ? 'terminal_state' : 'version_conflict',
        label: descriptor.label,
        staleVersion,
        serverVersion: run.value?.version ?? e.currentVersion ?? staleVersion,
      }
    } else {
      message.error(e.message || '重试失败')
    }
  } finally {
    busy.value = false
  }
}

function dismissConflict() {
  conflict.value = null
}

function doMetric() {
  return runCommand('metric')
}

function doArtifact() {
  if (!artifact.content_sha256 || artifact.content_sha256.length !== 64) {
    message.warning('请填写 64 位 content_sha256')
    return
  }
  return runCommand('artifact')
}

function doComplete() {
  if (!completeSummary.value.trim()) {
    message.warning('请填写完成摘要')
    return
  }
  return runCommand('complete')
}

function doAbort() {
  if (!abortReason.value.trim()) {
    message.warning('请填写中止原因')
    return
  }
  return runCommand('abort')
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    message.error(e.message || '加载失败')
  }
})
</script>
