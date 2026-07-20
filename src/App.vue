<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { Archive, BookOpenText, ChartNoAxesCombined, Settings2 } from '@lucide/vue'
import { RouterLink, RouterView } from 'vue-router'

type LayoutMode = 'desktop' | 'tablet' | 'mobile'

const layoutMode = ref<LayoutMode>('desktop')

function updateLayoutMode() {
  const viewportWidth = window.innerWidth

  // Layout follows the available browser viewport, not pointer capabilities.
  // Some desktop browsers report touch input, which must not force a wide window
  // into the single-column tablet layout.
  layoutMode.value = viewportWidth < 720 ? 'mobile' : viewportWidth < 960 ? 'tablet' : 'desktop'
}

onMounted(() => {
  updateLayoutMode()
  window.addEventListener('resize', updateLayoutMode)
  window.addEventListener('orientationchange', updateLayoutMode)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', updateLayoutMode)
  window.removeEventListener('orientationchange', updateLayoutMode)
})
</script>

<template>
  <div class="app-shell" :data-layout="layoutMode">
    <header class="app-header">
      <RouterLink to="/" class="brand" aria-label="NewsBrief 首页"
        ><span class="brand-mark">N</span><span>NewsBrief</span></RouterLink
      >
      <nav aria-label="主导航">
        <RouterLink to="/" class="nav-link"><BookOpenText :size="17" />摘要工作台</RouterLink
        ><RouterLink to="/history" class="nav-link"><Archive :size="17" />历史记录</RouterLink
        ><RouterLink to="/evaluation" class="nav-link"
          ><ChartNoAxesCombined :size="17" />评测中心</RouterLink
        ><RouterLink to="/settings" class="nav-link"><Settings2 :size="17" />设置</RouterLink>
      </nav>
      <span class="header-note">中文新闻摘要工具</span>
    </header>
    <main class="app-main"><RouterView /></main>
    <footer class="app-footer"><span>NewsBrief</span><span>本地优先 · 原文可追溯</span></footer>
  </div>
</template>
