<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ChartNoAxesCombined, CircleAlert, Database, RefreshCw, ShieldCheck } from '@lucide/vue'
import { api } from '../api'
import type { BenchmarkOverview } from '../types'

const overview = ref<BenchmarkOverview | null>(null)
const isRunning = ref(false)
const error = ref('')
const hasResults = computed(
  () => overview.value?.methods.some((method) => method.status === 'ready') ?? false,
)

async function loadOverview() {
  error.value = ''
  try {
    overview.value = await api.getBenchmarkOverview()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '无法读取评测说明。'
  }
}

async function runBenchmark() {
  error.value = ''
  isRunning.value = true
  try {
    overview.value = await api.runBenchmark()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '本机评测暂不可用。'
  } finally {
    isRunning.value = false
  }
}

onMounted(loadOverview)
</script>

<template>
  <section class="page-heading evaluation-heading">
    <div>
      <p class="eyebrow">TRUST LAB</p>
      <h1>可信度实验室</h1>
    </div>
    <button
      class="evaluation-run-button"
      type="button"
      :disabled="isRunning || !overview?.dataset.private_dataset_available"
      :title="
        overview?.dataset.private_dataset_available
          ? '运行本机私有评测集'
          : '请先导入本机私有评测集'
      "
      @click="runBenchmark"
    >
      <RefreshCw :size="16" :class="{ spin: isRunning }" />{{
        isRunning ? '正在运行' : '运行本机评测'
      }}
    </button>
  </section>

  <p v-if="error" class="message error-message page-message">
    <CircleAlert :size="16" />{{ error }}
  </p>

  <section v-if="overview" class="evaluation-summary" aria-label="评测集概览">
    <div class="evaluation-summary-main">
      <span class="engine-badge local"><ShieldCheck :size="14" />可复现评测</span>
      <h2>{{ overview.dataset.total }} 篇中文新闻标注集</h2>
      <p>公开来源元数据与方法，新闻正文和详细标注仅保留在本机，评测过程不依赖网络。</p>
    </div>
    <div class="evaluation-status" :class="{ ready: overview.dataset.private_dataset_available }">
      <Database :size="18" />{{
        overview.dataset.private_dataset_available ? '私有评测集已就绪' : '等待导入私有评测集'
      }}
    </div>
  </section>

  <section v-if="overview" class="evaluation-category-band" aria-label="测试集类别">
    <div v-for="category in overview.dataset.categories" :key="category.name">
      <strong>{{ category.count }}</strong
      ><span>{{ category.name }}</span>
    </div>
  </section>

  <section v-if="overview" class="evaluation-method-section">
    <header class="section-copy-heading">
      <div>
        <span class="section-index">01</span>
        <h2>算法对照</h2>
      </div>
      <p>所有方法使用同一批私有新闻快照和人工关键句标注。</p>
    </header>
    <div class="benchmark-method-list">
      <article v-for="method in overview.methods" :key="method.id" class="benchmark-method">
        <div class="benchmark-method-title">
          <span :class="{ ready: method.status === 'ready' }">{{
            method.status === 'ready' ? '已计算' : '待评测'
          }}</span>
          <h3>{{ method.name }}</h3>
          <p>{{ method.description }}</p>
        </div>
        <div v-if="method.metrics.length" class="benchmark-metrics">
          <div v-for="metric in method.metrics" :key="metric.label">
            <span>{{ metric.label }}</span
            ><strong>{{ metric.value }}</strong>
          </div>
        </div>
        <p v-else class="benchmark-pending">导入完成 60 篇私有新闻快照和标注后可在本机计算。</p>
      </article>
    </div>
  </section>

  <section v-if="overview" class="evaluation-methodology">
    <header class="section-copy-heading">
      <div>
        <span class="section-index">02</span>
        <h2>评测方法</h2>
      </div>
      <ChartNoAxesCombined :size="20" />
    </header>
    <ol>
      <li v-for="item in overview.methodology" :key="item">{{ item }}</li>
    </ol>
    <div class="human-review-note">
      <strong>人工盲评</strong>
      <span>{{ overview.human_review.reviewers_target }}</span>
      <span>{{ overview.human_review.samples }} 篇均衡样本</span>
      <span>{{ overview.human_review.dimensions.join(' · ') }}</span>
    </div>
  </section>

  <p v-if="overview && hasResults" class="evaluation-result-note">
    结果仅反映当前私有评测集与标注版本，报告应同步记录数据版本、运行日期和完整指标。
  </p>
</template>
