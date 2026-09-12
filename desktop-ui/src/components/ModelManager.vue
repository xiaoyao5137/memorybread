<!-- 模型管理面板 -->
<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { getLocalServiceBaseUrl } from '../utils/localServices'

const modelApiBaseUrl = getLocalServiceBaseUrl('model_api')

interface Model {
  id: string
  name: string
  type: 'llm' | 'embedding'
  provider: string
  model_id: string
  size_gb: number
  description: string
  status: 'not_installed' | 'downloading' | 'installed' | 'active'
  is_active: boolean
  is_default: boolean
  requires_api_key: boolean
}

const models = ref<Model[]>([])
const loading = ref(false)
const selectedTab = ref<'llm' | 'embedding'>('llm')
const apiKey = ref('')
const showApiKeyDialog = ref(false)
const selectedProvider = ref('')

// 筛选模型
const filteredModels = computed(() => {
  return models.value.filter(m => m.type === selectedTab.value)
})

// 获取模型列表
async function fetchModels() {
  loading.value = true
  try {
    const response = await fetch(`${modelApiBaseUrl}/api/models`)
    const data = await response.json()
    if (data.status === 'ok') {
      models.value = data.models
    }
  } catch (error) {
    console.error('获取模型列表失败:', error)
  } finally {
    loading.value = false
  }
}

// 下载模型
async function downloadModel(modelId: string) {
  const model = models.value.find(m => m.id === modelId)
  if (!model) return

  // 如果需要 API Key，先弹出对话框
  if (model.requires_api_key) {
    selectedProvider.value = model.provider
    showApiKeyDialog.value = true
    return
  }

  try {
    model.status = 'downloading'
    const response = await fetch(`${modelApiBaseUrl}/api/models/${modelId}/download`, {
      method: 'POST'
    })
    const data = await response.json()

    if (data.status === 'ok') {
      alert(`模型 ${model.name} 下载成功！`)
      await fetchModels()
    } else {
      alert(`下载失败: ${data.message}`)
      model.status = 'not_installed'
    }
  } catch (error) {
    console.error('下载模型失败:', error)
    alert('下载失败，请查看日志')
    model.status = 'not_installed'
  }
}

// 激活模型
async function activateModel(modelId: string) {
  try {
    const response = await fetch(`${modelApiBaseUrl}/api/models/${modelId}/activate`, {
      method: 'POST'
    })
    const data = await response.json()

    if (data.status === 'ok') {
      alert(`模型已激活！`)
      await fetchModels()
    } else {
      alert(`激活失败: ${data.message}`)
    }
  } catch (error) {
    console.error('激活模型失败:', error)
    alert('激活失败，请查看日志')
  }
}

// 删除模型
async function deleteModel(modelId: string) {
  if (!confirm('确定要删除这个模型吗？')) return

  try {
    const response = await fetch(`${modelApiBaseUrl}/api/models/${modelId}/delete`, {
      method: 'DELETE'
    })
    const data = await response.json()

    if (data.status === 'ok') {
      alert('模型已删除')
      await fetchModels()
    } else {
      alert(`删除失败: ${data.message}`)
    }
  } catch (error) {
    console.error('删除模型失败:', error)
    alert('删除失败，请查看日志')
  }
}

// 设置 API Key
async function setApiKey() {
  if (!apiKey.value.trim()) {
    alert('请输入 API Key')
    return
  }

  try {
    const response = await fetch(`${modelApiBaseUrl}/api/models/config/api-key`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: selectedProvider.value,
        api_key: apiKey.value
      })
    })
    const data = await response.json()

    if (data.status === 'ok') {
      alert('API Key 已设置')
      showApiKeyDialog.value = false
      apiKey.value = ''
      await fetchModels()
    } else {
      alert(`设置失败: ${data.message}`)
    }
  } catch (error) {
    console.error('设置 API Key 失败:', error)
    alert('设置失败，请查看日志')
  }
}

// 获取状态标签
function getStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    'not_installed': '未安装',
    'downloading': '下载中...',
    'installed': '已安装',
    'active': '使用中'
  }
  return labels[status] || status
}

// 获取状态颜色
function getStatusColor(status: string): string {
  const colors: Record<string, string> = {
    'not_installed': '#999',
    'downloading': '#1890ff',
    'installed': '#52c41a',
    'active': '#722ed1'
  }
  return colors[status] || '#999'
}

onMounted(() => {
  fetchModels()
})
</script>

<template>
  <div class="model-manager">
    <div class="header">
      <h2>🤖 模型管理</h2>
      <button @click="fetchModels" class="refresh-btn">🔄 刷新</button>
    </div>

    <!-- 标签页 -->
    <div class="tabs">
      <button
        :class="['tab', { active: selectedTab === 'llm' }]"
        @click="selectedTab = 'llm'"
      >
        💬 文本推理模型
      </button>
      <button
        :class="['tab', { active: selectedTab === 'embedding' }]"
        @click="selectedTab = 'embedding'"
      >
        🔍 向量模型
      </button>
    </div>

    <!-- 模型列表 -->
    <div v-if="loading" class="loading">加载中...</div>

    <div v-else class="model-list">
      <div
        v-for="model in filteredModels"
        :key="model.id"
        class="model-card"
      >
        <div class="model-header">
          <div class="model-title">
            <h3>{{ model.name }}</h3>
            <span
              class="status-badge"
              :style="{ backgroundColor: getStatusColor(model.status) }"
            >
              {{ getStatusLabel(model.status) }}
            </span>
            <span v-if="model.is_default" class="default-badge">默认</span>
          </div>
          <div class="model-size">{{ model.size_gb > 0 ? `${model.size_gb} GB` : '云端 API' }}</div>
        </div>

        <p class="model-description">{{ model.description }}</p>

        <div class="model-meta">
          <span class="meta-item">📦 {{ model.provider }}</span>
          <span class="meta-item">🆔 {{ model.model_id }}</span>
        </div>

        <div class="model-actions">
          <!-- 未安装 -->
          <button
            v-if="model.status === 'not_installed'"
            @click="downloadModel(model.id)"
            class="btn btn-primary"
          >
            {{ model.requires_api_key ? '⚙️ 配置 API Key' : '⬇️ 下载' }}
          </button>

          <!-- 下载中 -->
          <button
            v-else-if="model.status === 'downloading'"
            class="btn btn-disabled"
            disabled
          >
            ⏳ 下载中...
          </button>

          <!-- 已安装但未激活 -->
          <template v-else-if="model.status === 'installed'">
            <button
              @click="activateModel(model.id)"
              class="btn btn-success"
            >
              ✅ 激活
            </button>
            <button
              @click="deleteModel(model.id)"
              class="btn btn-danger"
            >
              🗑️ 删除
            </button>
          </template>

          <!-- 使用中 -->
          <template v-else-if="model.status === 'active' || model.is_active">
            <button class="btn btn-active" disabled>
              ⭐ 使用中
            </button>
            <button
              v-if="!model.requires_api_key"
              @click="deleteModel(model.id)"
              class="btn btn-danger"
            >
              🗑️ 删除
            </button>
          </template>
        </div>
      </div>
    </div>

    <!-- API Key 对话框 -->
    <div v-if="showApiKeyDialog" class="dialog-overlay" @click="showApiKeyDialog = false">
      <div class="dialog" @click.stop>
        <h3>设置 {{ selectedProvider }} API Key</h3>
        <input
          v-model="apiKey"
          type="password"
          placeholder="请输入 API Key"
          class="api-key-input"
        />
        <div class="dialog-actions">
          <button @click="setApiKey" class="btn btn-primary">确定</button>
          <button @click="showApiKeyDialog = false" class="btn btn-secondary">取消</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.model-manager {
  padding: 20px;
  max-width: 1200px;
  margin: 0 auto;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}

.header h2 {
  margin: 0;
  font-size: 24px;
}

.refresh-btn {
  padding: 8px 16px;
  background: #1890ff;
  color: white;
  border: none;
  border-radius: 4px;
  cursor: pointer;
}

.refresh-btn:hover {
  background: #40a9ff;
}

.tabs {
  display: flex;
  gap: 10px;
  margin-bottom: 20px;
  border-bottom: 2px solid #f0f0f0;
}

.tab {
  padding: 10px 20px;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  font-size: 16px;
  color: #666;
  transition: all 0.3s;
}

.tab.active {
  color: #1890ff;
  border-bottom-color: #1890ff;
  font-weight: 600;
}

.loading {
  text-align: center;
  padding: 40px;
  color: #999;
}

.model-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
  gap: 20px;
}

.model-card {
  border: 1px solid #e8e8e8;
  border-radius: 8px;
  padding: 20px;
  background: white;
  transition: box-shadow 0.3s;
}

.model-card:hover {
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
}

.model-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 12px;
}

.model-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.model-title h3 {
  margin: 0;
  font-size: 18px;
}

.status-badge {
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 12px;
  color: white;
}

.default-badge {
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 12px;
  background: #faad14;
  color: white;
}

.model-size {
  font-size: 14px;
  color: #999;
  white-space: nowrap;
}

.model-description {
  color: #666;
  font-size: 14px;
  line-height: 1.6;
  margin: 12px 0;
}

.model-meta {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}

.meta-item {
  font-size: 12px;
  color: #999;
}

.model-actions {
  display: flex;
  gap: 8px;
}

.btn {
  padding: 8px 16px;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  font-size: 14px;
  transition: all 0.3s;
}

.btn-primary {
  background: #1890ff;
  color: white;
}

.btn-primary:hover {
  background: #40a9ff;
}

.btn-success {
  background: #52c41a;
  color: white;
}

.btn-success:hover {
  background: #73d13d;
}

.btn-danger {
  background: #ff4d4f;
  color: white;
}

.btn-danger:hover {
  background: #ff7875;
}

.btn-active {
  background: #722ed1;
  color: white;
}

.btn-disabled {
  background: #d9d9d9;
  color: #999;
  cursor: not-allowed;
}

.btn-secondary {
  background: #f0f0f0;
  color: #666;
}

.btn-secondary:hover {
  background: #e0e0e0;
}

.dialog-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.dialog {
  background: white;
  padding: 24px;
  border-radius: 8px;
  min-width: 400px;
}

.dialog h3 {
  margin: 0 0 16px 0;
}

.api-key-input {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid #d9d9d9;
  border-radius: 4px;
  font-size: 14px;
  margin-bottom: 16px;
}

.dialog-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
</style>
