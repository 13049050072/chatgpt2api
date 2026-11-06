import { type Ref, watch } from 'vue'
import {
  preferenceKeys,
  preferenceStorageKey,
  readStoredValue,
  writeStoredValue,
} from '@/lib/preferences'
import { scheduleIdleTask, type IdleTaskHandle } from '@/lib/idleTask'
import type { StudioConversation, StudioConversationBadgeState } from '@/components/studio/types'
import {
  isStudioConversationBusy,
  loadStudioConversationNotices,
  loadStudioConversations,
  parseStudioConversations,
  persistStudioConversationNotices,
  persistStudioConversations,
  type StudioConversationPersistStatus,
} from './studioConversationState'

type StudioConversationIdSetRef = {
  value: Set<string>
}

export type StudioConversationPersistenceRuntimeInput = {
  conversations: Ref<StudioConversation[]>
  conversationNotices: Ref<Record<string, StudioConversationBadgeState>>
  activeConversationId: Ref<string>
  validConversationIds: StudioConversationIdSetRef
  onStorageLimited?: (status: Exclude<StudioConversationPersistStatus, 'saved'>) => void
}

function studioStorageKeys() {
  return {
    conversations: preferenceStorageKey(preferenceKeys.studioConversations),
    notices: preferenceStorageKey(preferenceKeys.studioConversationBadges),
    activeConversationId: preferenceStorageKey(preferenceKeys.studioActiveConversationId),
  }
}

export function loadStudioConversationPersistenceState() {
  const keys = studioStorageKeys()
  return {
    activeConversationId: readStoredValue(keys.activeConversationId) || '',
    conversationNotices: loadStudioConversationNotices(keys.notices),
    conversations: loadStudioConversations(keys.conversations),
  }
}

export type StudioConversationPersistenceRuntime = ReturnType<typeof useStudioConversationPersistenceRuntime>

export function useStudioConversationPersistenceRuntime(input: StudioConversationPersistenceRuntimeInput) {
  // History belongs to the key that loaded it. Writes after logout or after another
  // tab switched keys must still land in this key's partition, never the new one.
  const keys = studioStorageKeys()
  let disposed = false
  let conversationsTimer: number | null = null
  let conversationNoticesTimer: number | null = null
  let activeConversationTimer: number | null = null
  let conversationsIdleTask: IdleTaskHandle | null = null
  let conversationNoticesIdleTask: IdleTaskHandle | null = null
  // Ids this tab has held in memory; a known id that is gone was deleted here, while
  // an unknown id in storage was created by another tab and must be kept.
  const knownConversationIds = new Set(input.conversations.value.map((item) => item.id))
  let lastStoredConversationIds = new Set(knownConversationIds)
  let lastWrittenRaw: string | null = null

  const stopConversationWatch = watch(input.conversations, scheduleConversations)
  const stopConversationNoticeWatch = watch(input.conversationNotices, scheduleConversationNotices)
  const stopActiveConversationWatch = watch(input.activeConversationId, scheduleActiveConversationId)
  window.addEventListener('storage', handleStorageEvent)

  function persistConversations() {
    if (!keys.conversations) return
    const local = input.conversations.value
    local.forEach((item) => knownConversationIds.add(item.id))
    const raw = readStoredValue(keys.conversations)
    // Unchanged since our own last write: nothing from other tabs to merge.
    const stored = raw === lastWrittenRaw ? [] : parseStudioConversations(raw)
    const storedById = new Map(stored.map((item) => [item.id, item]))
    const fromOtherTabs = stored.filter((item) => !knownConversationIds.has(item.id))
    const merged = [
      ...fromOtherTabs,
      ...local.map((item) => {
        const storedItem = storedById.get(item.id)
        return storedItem && storedItem.updatedAt > item.updatedAt ? storedItem : item
      }),
    ]
    const result = persistStudioConversations(merged, keys.conversations)
    if (result.status !== 'failed') {
      lastStoredConversationIds = new Set(result.storedIds)
      lastWrittenRaw = result.raw
    }
    if (result.status !== 'saved') input.onStorageLimited?.(result.status)
  }

  function handleStorageEvent(event: StorageEvent) {
    if (!keys.conversations || event.key !== keys.conversations) return
    const stored = parseStudioConversations(event.newValue)
    const storedById = new Map(stored.map((item) => [item.id, item]))
    let changed = false
    let next = input.conversations.value.filter((item) => {
      const deletedElsewhere = lastStoredConversationIds.has(item.id)
        && !storedById.has(item.id)
        && !isStudioConversationBusy(item)
      if (deletedElsewhere) changed = true
      return !deletedElsewhere
    })
    next = next.map((item) => {
      const storedItem = storedById.get(item.id)
      if (!storedItem || storedItem.updatedAt <= item.updatedAt || isStudioConversationBusy(item)) return item
      changed = true
      return storedItem
    })
    const inMemory = new Set(next.map((item) => item.id))
    const additions = stored.filter((item) => !inMemory.has(item.id) && !knownConversationIds.has(item.id))
    if (additions.length) {
      changed = true
      next = [...additions, ...next]
      additions.forEach((item) => knownConversationIds.add(item.id))
    }
    lastStoredConversationIds = new Set(storedById.keys())
    lastWrittenRaw = null
    if (changed) input.conversations.value = next
  }

  function persistConversationNotices() {
    if (!keys.notices) return
    persistStudioConversationNotices(input.conversationNotices.value, input.validConversationIds.value, keys.notices)
  }

  function persistActiveConversationId() {
    writeStoredValue(keys.activeConversationId, input.activeConversationId.value)
  }

  function scheduleConversations() {
    if (disposed || conversationsTimer !== null) return
    conversationsTimer = window.setTimeout(() => {
      conversationsTimer = null
      conversationsIdleTask?.cancel()
      conversationsIdleTask = scheduleIdleTask(() => {
        conversationsIdleTask = null
        persistConversations()
      }, 1200)
    }, 300)
  }

  function scheduleConversationNotices() {
    if (disposed || conversationNoticesTimer !== null) return
    conversationNoticesTimer = window.setTimeout(() => {
      conversationNoticesTimer = null
      conversationNoticesIdleTask?.cancel()
      conversationNoticesIdleTask = scheduleIdleTask(() => {
        conversationNoticesIdleTask = null
        persistConversationNotices()
      }, 1200)
    }, 300)
  }

  function scheduleActiveConversationId() {
    if (disposed) return
    if (activeConversationTimer !== null) {
      window.clearTimeout(activeConversationTimer)
    }
    activeConversationTimer = window.setTimeout(() => {
      activeConversationTimer = null
      persistActiveConversationId()
    }, 200)
  }

  function flushConversations() {
    if (conversationsTimer !== null) {
      window.clearTimeout(conversationsTimer)
      conversationsTimer = null
    }
    if (conversationsIdleTask) {
      conversationsIdleTask.flush()
      conversationsIdleTask = null
      return
    }
    persistConversations()
  }

  function flushConversationNotices() {
    if (conversationNoticesTimer !== null) {
      window.clearTimeout(conversationNoticesTimer)
      conversationNoticesTimer = null
    }
    if (conversationNoticesIdleTask) {
      conversationNoticesIdleTask.flush()
      conversationNoticesIdleTask = null
      return
    }
    persistConversationNotices()
  }

  function flushActiveConversationId() {
    if (activeConversationTimer !== null) {
      window.clearTimeout(activeConversationTimer)
      activeConversationTimer = null
    }
    persistActiveConversationId()
  }

  function flush() {
    if (conversationsTimer !== null || conversationsIdleTask) flushConversations()
    if (conversationNoticesTimer !== null || conversationNoticesIdleTask) flushConversationNotices()
    if (activeConversationTimer !== null) flushActiveConversationId()
  }

  function dispose() {
    window.removeEventListener('storage', handleStorageEvent)
    stopConversationWatch()
    stopConversationNoticeWatch()
    stopActiveConversationWatch()
    flush()
    disposed = true
  }

  return {
    flush,
    scheduleConversationNotices,
    scheduleConversations,
    dispose,
  }
}
