<template>
  <div class="space-y-4">
    <SurfaceBox density="compact">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0">
          <p class="ui-section-title">对话模型</p>
          <p class="mt-1 max-w-3xl text-xs leading-5 text-muted-foreground">
            从一个有效的 ChatGPT Web 账号读取上游目录。按你的确认，号池账号返回的目录相同；获取后勾选要开放给调用方的模型，再点页面顶部“保存设置”。
          </p>
          <p v-if="fetchedAt" class="mt-1 text-xs text-muted-foreground">
            上次读取：{{ formattedFetchedAt }} · {{ upstreamModels.length }} 个模型
          </p>
        </div>
        <Button size="sm" variant="outline" :disabled="loading" @click="discoverModels">
          {{ loading ? '读取中...' : '从上游获取模型' }}
        </Button>
      </div>
    </SurfaceBox>

    <SurfaceBox v-if="errorMessage" density="compact" tone="danger" role="alert">
      {{ errorMessage }}
    </SurfaceBox>
    <p v-if="selectionMessage" role="status" class="text-sm text-muted-foreground">
      {{ selectionMessage }}
    </p>

    <SurfaceBox density="compact">
      <div class="space-y-3">
        <div>
          <p class="ui-section-title">可用列表</p>
          <p class="mt-1 text-xs leading-5 text-muted-foreground">
            当前已启用但上游目录未列出的模型仍会保留，避免重新获取时意外移除手动配置的别名。图片模型列表不在此处修改。
          </p>
        </div>

        <div v-if="modelOptions.length" class="divide-y divide-border">
          <label
            v-for="model in modelOptions"
            :key="model"
            class="flex min-w-0 items-center justify-between gap-4 py-3"
          >
            <span class="min-w-0 break-all font-mono text-sm text-foreground">
              {{ model }}
              <span v-if="isEnabled(model) && !upstreamModels.includes(model)" class="ml-2 font-sans text-xs text-muted-foreground">
                上游未列出
              </span>
            </span>
            <Checkbox
              :model-value="isEnabled(model)"
              :disabled="isOnlyEnabled(model)"
              @update:model-value="setEnabled(model, Boolean($event))"
            >
              启用
            </Checkbox>
          </label>
        </div>
        <p v-else class="text-sm text-muted-foreground">暂无模型，请从上游获取或手动添加模型 ID。</p>

        <div class="flex flex-col gap-2 border-t border-border pt-3 sm:flex-row">
          <Input
            v-model.trim="newModel"
            block
            aria-label="手动添加模型 ID"
            placeholder="手动添加模型 ID，例如 gpt-6-luna"
            @keydown.enter.prevent="addModel"
          />
          <Button size="sm" variant="outline" :disabled="!newModel.trim()" @click="addModel">
            添加并启用
          </Button>
        </div>
        <p class="text-xs text-muted-foreground">至少保留一个启用的对话模型。变更会跟随页面顶部的“保存设置”写入服务端。</p>
      </div>
    </SurfaceBox>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { Button, Checkbox, Input } from 'nanocat-ui'
import { modelsApi } from '@/api/models'
import SurfaceBox from '@/components/ai/SurfaceBox.vue'
import { errorMessage as formatErrorMessage } from '@/lib/errorMessage'

const props = defineProps<{ models: string[] }>()
const emit = defineEmits<{ 'update:models': [models: string[]] }>()

const loading = ref(false)
const errorMessage = ref('')
const selectionMessage = ref('')
const upstreamModels = ref<string[]>([])
const fetchedAt = ref('')
const newModel = ref('')

const enabledModels = computed(() => props.models)
const modelOptions = computed(() => Array.from(new Set([
  ...enabledModels.value,
  ...upstreamModels.value,
])))
const formattedFetchedAt = computed(() => {
  const value = new Date(fetchedAt.value)
  return Number.isNaN(value.getTime()) ? fetchedAt.value : value.toLocaleString('zh-CN')
})

function isEnabled(model: string) {
  return enabledModels.value.includes(model)
}

function isOnlyEnabled(model: string) {
  return isEnabled(model) && enabledModels.value.length <= 1
}

function setEnabled(model: string, enabled: boolean) {
  selectionMessage.value = ''
  if (enabled) {
    if (!isEnabled(model)) emit('update:models', [...enabledModels.value, model])
    return
  }
  const next = enabledModels.value.filter((item) => item !== model)
  if (!next.length) {
    selectionMessage.value = '至少保留一个启用的对话模型。'
    return
  }
  emit('update:models', next)
}

async function discoverModels() {
  loading.value = true
  errorMessage.value = ''
  selectionMessage.value = ''
  try {
    const response = await modelsApi.discover()
    upstreamModels.value = Array.from(new Set(response.models.map((model) => model.trim()).filter(Boolean)))
    fetchedAt.value = response.fetched_at
  } catch (error) {
    errorMessage.value = formatErrorMessage(error, '获取上游模型失败，请检查 ChatGPT Web 账号状态后重试。')
  } finally {
    loading.value = false
  }
}

function addModel() {
  const model = newModel.value.trim()
  if (!model) return
  selectionMessage.value = ''
  if (!isEnabled(model)) emit('update:models', [...enabledModels.value, model])
  if (!modelOptions.value.includes(model)) upstreamModels.value = [...upstreamModels.value, model]
  newModel.value = ''
}
</script>
