import { createRouter, createWebHashHistory } from 'vue-router'
import HistoryView from './views/HistoryView.vue'
import EvaluationView from './views/EvaluationView.vue'
import SettingsView from './views/SettingsView.vue'
import WorkbenchView from './views/WorkbenchView.vue'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'workbench', component: WorkbenchView },
    { path: '/history', name: 'history', component: HistoryView },
    { path: '/settings', name: 'settings', component: SettingsView },
    { path: '/evaluation', name: 'evaluation', component: EvaluationView },
  ],
})
