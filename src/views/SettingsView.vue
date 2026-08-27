<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import {
  Check,
  CircleAlert,
  Database,
  Eye,
  EyeOff,
  KeyRound,
  LockKeyhole,
  Network,
  RefreshCw,
  Save,
  Search,
  ShieldCheck,
  Sparkles,
  Workflow,
} from '@lucide/vue'
import { api } from '../api'
import { useNewsStore } from '../stores/news'
import type { SearchProvider } from '../types'

const store = useNewsStore()
const showApiKey = ref(false)
const showSearchKey = ref(false)
const isSaving = ref(false)
const isVerifying = ref(false)
const isSavingSearch = ref(false)
const isVerifyingSearch = ref(false)
const configError = ref('')
const configNotice = ref('')
const searchError = ref('')
const searchNotice = ref('')
const config = reactive({
  apiKey: '',
  baseUrl: 'https://api.deepseek.com',
  model: 'deepseek-chat',
})
const searchConfig = reactive<{ provider: SearchProvider; apiKey: string }>({
  provider: 'bocha',
  apiKey: '',
})
const searchProviderLabel = computed(() =>
  searchConfig.provider === 'bocha' ? '博查 Web Search' : 'Brave Search',
)
const searchProviderHint = computed(() =>
  searchConfig.provider === 'bocha'
    ? '面向中文新闻的国内默认检索服务。'
    : '适合国际新闻或已有 Brave Key 的用户。',
)

function syncConfigDefaults() {
  const ai = store.capabilities?.ai_engine
  if (!ai) return
  config.baseUrl = ai.base_url
  config.model = ai.model
}

function syncSearchConfigDefaults() {
  const provider = store.capabilities?.verification_engine.provider
  if (provider) searchConfig.provider = provider
}

onMounted(async () => {
  if (!store.capabilities) await store.bootstrap()
  syncConfigDefaults()
  syncSearchConfigDefaults()
})

async function saveAIConfig() {
  configError.value = ''
  configNotice.value = ''
  if (!config.apiKey.trim()) {
    configError.value = '请输入 API Key 后再保存。'
    return
  }

  isSaving.value = true
  try {
    await store.configureAI({
      api_key: config.apiKey.trim(),
      base_url: config.baseUrl.trim(),
      model: config.model.trim(),
    })
    config.apiKey = ''
    showApiKey.value = false
    syncConfigDefaults()
    configNotice.value = 'AI 服务已配置。现在可在摘要工作台选择“AI 增强”。'
  } catch (reason) {
    configError.value =
      reason instanceof Error ? reason.message : 'AI 配置保存失败，请检查填写内容。'
  } finally {
    isSaving.value = false
  }
}

async function verifyAIConfig() {
  configError.value = ''
  configNotice.value = ''
  if (!config.apiKey.trim()) {
    configError.value = '请输入 API Key 后再测试连接。'
    return
  }

  isVerifying.value = true
  try {
    const result = await api.verifyAI({
      api_key: config.apiKey.trim(),
      base_url: config.baseUrl.trim(),
      model: config.model.trim(),
    })
    if (result.available) {
      configNotice.value = `连接成功，${result.model} 可以响应。配置尚未保存。`
    } else {
      configError.value = result.message
    }
  } catch (reason) {
    configError.value = reason instanceof Error ? reason.message : '连接测试失败，请检查填写内容。'
  } finally {
    isVerifying.value = false
  }
}

async function saveSearchConfig() {
  searchError.value = ''
  searchNotice.value = ''
  if (!searchConfig.apiKey.trim()) {
    searchError.value = `请输入 ${searchProviderLabel.value} API Key 后再保存。`
    return
  }
  isSavingSearch.value = true
  try {
    await store.configureSearch({
      provider: searchConfig.provider,
      api_key: searchConfig.apiKey.trim(),
    })
    searchConfig.apiKey = ''
    showSearchKey.value = false
    syncSearchConfigDefaults()
    searchNotice.value = `${searchProviderLabel.value} 已配置，可在核验线索中主动发起搜索。`
  } catch (reason) {
    searchError.value = reason instanceof Error ? reason.message : '搜索服务配置保存失败。'
  } finally {
    isSavingSearch.value = false
  }
}

async function verifySearchConfig() {
  searchError.value = ''
  searchNotice.value = ''
  if (!searchConfig.apiKey.trim()) {
    searchError.value = `请输入 ${searchProviderLabel.value} API Key 后再测试连接。`
    return
  }
  isVerifyingSearch.value = true
  try {
    const result = await api.verifySearch({
      provider: searchConfig.provider,
      api_key: searchConfig.apiKey.trim(),
    })
    if (result.available) {
      searchNotice.value = '连接成功。该 Key 尚未保存。'
    } else {
      searchError.value = result.message
    }
  } catch (reason) {
    searchError.value = reason instanceof Error ? reason.message : '搜索服务连接测试失败。'
  } finally {
    isVerifyingSearch.value = false
  }
}
</script>

<template>
  <section class="page-heading">
    <div>
      <p class="eyebrow">SYSTEM SETTINGS</p>
      <h1>设置与可信度</h1>
    </div>
  </section>

  <section class="settings-grid">
    <article class="setting-panel capability-panel">
      <div class="setting-icon"><Workflow :size="20" /></div>
      <div>
        <p class="panel-kicker">默认引擎</p>
        <h2>本地可靠摘要</h2>
        <p>中文分句、TF-IDF 句子相似度、PageRank 排序和 MMR 去重均在本机完成。</p>
      </div>
      <div class="setting-state success"><Check :size="16" />已启用</div>
    </article>

    <article class="setting-panel capability-panel">
      <div class="setting-icon ai-icon"><Sparkles :size="20" /></div>
      <div>
        <p class="panel-kicker">可选引擎</p>
        <h2>AI 增强摘要</h2>
        <p>{{ store.capabilities?.ai_engine.message ?? '正在读取服务状态。' }}</p>
      </div>
      <div class="setting-state" :class="store.aiEnabled ? 'success' : 'muted'">
        <Check v-if="store.aiEnabled" :size="16" /><CircleAlert v-else :size="16" />{{
          store.aiEnabled ? '已启用' : '未配置'
        }}
      </div>
    </article>

    <article id="ai-config" class="setting-panel wide ai-config-panel">
      <div class="setting-icon ai-icon"><KeyRound :size="20" /></div>
      <div class="ai-config-heading">
        <p class="panel-kicker">AI 服务配置</p>
        <h2>连接 DeepSeek 或 OpenAI 兼容服务</h2>
        <p>
          保存后立即生效。密钥只传给当前本机后端并写入
          <code>backend/.env</code>，页面不会回显、历史记录不会保存。
        </p>
      </div>
      <p class="ai-review-privacy-note">
        AI
        辅助证据解读只会在你主动点击后，接收当前核验的来源标题、等级、域名与短摘录；不会接收整篇新闻正文或完整网页内容。
      </p>

      <form class="ai-config-form" @submit.prevent="saveAIConfig">
        <label class="setting-field key-field">
          <span>API Key</span>
          <div class="secret-input">
            <input
              v-model="config.apiKey"
              :type="showApiKey ? 'text' : 'password'"
              autocomplete="off"
              placeholder="粘贴 API Key"
            />
            <button
              type="button"
              :title="showApiKey ? '隐藏 API Key' : '显示 API Key'"
              :aria-label="showApiKey ? '隐藏 API Key' : '显示 API Key'"
              @click="showApiKey = !showApiKey"
            >
              <EyeOff v-if="showApiKey" :size="17" /><Eye v-else :size="17" />
            </button>
          </div>
        </label>
        <label class="setting-field">
          <span>服务地址</span>
          <input
            v-model="config.baseUrl"
            type="url"
            inputmode="url"
            placeholder="https://api.deepseek.com"
          />
        </label>
        <label class="setting-field">
          <span>模型名称</span>
          <input v-model="config.model" type="text" placeholder="deepseek-chat" />
        </label>
        <div class="ai-config-actions">
          <button
            class="ai-verify-button"
            type="button"
            :disabled="isVerifying || !config.apiKey.trim()"
            @click="verifyAIConfig"
          >
            <span v-if="isVerifying" class="spinner teal"></span><RefreshCw v-else :size="16" />{{
              isVerifying ? '测试中' : '测试连接'
            }}
          </button>
          <button
            class="ai-save-button"
            type="submit"
            :disabled="isSaving || !config.apiKey.trim()"
          >
            <span v-if="isSaving" class="spinner dark"></span><Save v-else :size="16" />{{
              isSaving ? '正在保存' : '保存配置'
            }}
          </button>
        </div>
      </form>
      <p v-if="configError" class="message error-message ai-config-message">
        <CircleAlert :size="16" />{{ configError }}
      </p>
      <p v-else-if="configNotice" class="message notice-message ai-config-message">
        <ShieldCheck :size="16" />{{ configNotice }}
      </p>
    </article>

    <article id="search-config" class="setting-panel wide ai-config-panel">
      <div class="setting-icon"><Search :size="20" /></div>
      <div class="ai-config-heading">
        <p class="panel-kicker">公开来源检索</p>
        <h2>配置公开来源检索</h2>
        <p>
          默认使用适合中文新闻的博查 Web Search，也可切换为 Brave
          国际来源检索。仅在你点击“开始联网核验”时，系统才会发送由新闻主体、事件、时间或数值组成的短查询；不会上传整篇新闻正文。
          密钥仅保存到本机 <code>backend/.env</code>，不会出现在历史、导出文件或页面回显中。
        </p>
      </div>
      <form class="ai-config-form search-config-form" @submit.prevent="saveSearchConfig">
        <label class="setting-field">
          <span>搜索服务</span>
          <select v-model="searchConfig.provider" aria-label="选择公开来源检索服务">
            <option value="bocha">博查 Web Search（国内默认）</option>
            <option value="brave">Brave Search（国际来源）</option>
          </select>
        </label>
        <label class="setting-field key-field">
          <span>{{ searchProviderLabel }} API Key</span>
          <div class="secret-input">
            <input
              v-model="searchConfig.apiKey"
              :type="showSearchKey ? 'text' : 'password'"
              autocomplete="off"
              :placeholder="`粘贴 ${searchProviderLabel} API Key`"
            />
            <button
              type="button"
              :title="showSearchKey ? '隐藏 API Key' : '显示 API Key'"
              :aria-label="showSearchKey ? '隐藏 API Key' : '显示 API Key'"
              @click="showSearchKey = !showSearchKey"
            >
              <EyeOff v-if="showSearchKey" :size="17" /><Eye v-else :size="17" />
            </button>
          </div>
        </label>
        <p class="search-provider-note">
          <strong>{{ searchProviderHint }}</strong>
        </p>
        <div class="ai-config-actions">
          <button
            class="ai-verify-button"
            type="button"
            :disabled="isVerifyingSearch || !searchConfig.apiKey.trim()"
            @click="verifySearchConfig"
          >
            <span v-if="isVerifyingSearch" class="spinner teal"></span
            ><RefreshCw v-else :size="16" />{{ isVerifyingSearch ? '测试中' : '测试连接' }}
          </button>
          <button
            class="ai-save-button"
            type="submit"
            :disabled="isSavingSearch || !searchConfig.apiKey.trim()"
          >
            <span v-if="isSavingSearch" class="spinner dark"></span><Save v-else :size="16" />{{
              isSavingSearch ? '正在保存' : '保存配置'
            }}
          </button>
        </div>
      </form>
      <p v-if="searchError" class="message error-message ai-config-message">
        <CircleAlert :size="16" />{{ searchError }}
      </p>
      <p v-else-if="searchNotice" class="message notice-message ai-config-message">
        <ShieldCheck :size="16" />{{ searchNotice }}
      </p>
    </article>

    <article class="setting-panel wide">
      <div class="setting-icon"><LockKeyhole :size="20" /></div>
      <div>
        <p class="panel-kicker">数据与隐私</p>
        <h2>内容只保存在本机</h2>
        <p>
          历史记录保存于本机 SQLite 数据库。未启用 AI 时，新闻正文不会离开本地服务；启用 AI
          后仅在用户主动生成时发送给已配置服务。
        </p>
      </div>
    </article>
    <article class="setting-panel wide">
      <div class="setting-icon"><Database :size="20" /></div>
      <div>
        <p class="panel-kicker">处理限制</p>
        <h2>稳定优先</h2>
        <p>
          单篇新闻支持 {{ store.capabilities?.limits.min_characters ?? 80 }} 至
          {{ store.capabilities?.limits.max_characters ?? 8000 }} 个有效字符，最多
          {{ store.capabilities?.limits.max_sentences ?? 180 }} 个有效句。
        </p>
      </div>
    </article>
    <article class="setting-panel wide">
      <div class="setting-icon"><Network :size="20" /></div>
      <div>
        <p class="panel-kicker">摘要依据</p>
        <h2>每个本地结果均可回溯</h2>
        <p>
          本地模式只选择原文关键句，并在摘要结果中标注出处。AI
          模式必须返回依据句索引，校验失败时会自动切换到本地摘要。
        </p>
      </div>
    </article>
  </section>
</template>
