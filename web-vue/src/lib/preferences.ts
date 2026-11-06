export const preferenceKeys = {
  sidebarCollapsed: 'sidebar-collapsed',
  accountsViewMode: 'accounts-view-mode',
  accountsPageSize: 'accounts-page-size',
  systemLogLimit: 'system-log-limit',
  galleryPageSize: 'gallery-page-size',
  listLayoutMode: 'list-layout-mode',
  publicLogFoldState: 'public-log-fold-state',
  imageTaskLocalIds: 'image-task-local-ids',
  imageTaskConversations: 'image-task-conversations',
  imageTaskActiveConversationId: 'image-task-active-conversation-id',
  studioActiveMode: 'studio-active-mode',
  studioActiveConversationId: 'studio-active-conversation-id',
  studioChatModel: 'studio-chat-model',
  studioChatReasoningEffort: 'studio-chat-reasoning-effort',
  studioConversationBadges: 'studio-conversation-badges',
  studioConversations: 'studio-conversations',
  studioFullscreen: 'studio-fullscreen',
  studioFileKind: 'studio-file-kind',
  studioImageModel: 'studio-image-model',
  studioSidebarWidth: 'studio-sidebar-width',
  themeMode: 'theme-mode',
  updateActiveTaskId: 'update-active-task-id',
  updateDismissedTag: 'update-dismissed-tag',
} as const

type PreferenceKey = typeof preferenceKeys[keyof typeof preferenceKeys]

// Conversation history and task ids belong to one signed-in key, never to the browser.
const userScopedKeys: ReadonlySet<PreferenceKey> = new Set([
  preferenceKeys.imageTaskLocalIds,
  preferenceKeys.imageTaskConversations,
  preferenceKeys.imageTaskActiveConversationId,
  preferenceKeys.studioActiveConversationId,
  preferenceKeys.studioConversationBadges,
  preferenceKeys.studioConversations,
])

let preferenceScope = ''

function storage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function setPreferenceScope(scope: string, options: { claimLegacy?: boolean } = {}): void {
  preferenceScope = encodeURIComponent(scope.trim())
  if (!preferenceScope || !options.claimLegacy) return
  const store = storage()
  if (!store) return
  for (const key of userScopedKeys) {
    try {
      const legacy = store.getItem(key)
      const scopedKey = `${key}:${preferenceScope}`
      if (legacy == null || store.getItem(scopedKey) != null) continue
      // Remove first: a full storage has room for the same value once it is freed.
      store.removeItem(key)
      if (!writeStoredValue(scopedKey, legacy)) writeStoredValue(key, legacy)
    } catch {
      // Leave legacy data in place when it cannot be moved.
    }
  }
}

export function preferenceStorageKey(key: PreferenceKey): string | null {
  if (!userScopedKeys.has(key)) return key
  return preferenceScope ? `${key}:${preferenceScope}` : null
}

export function readStoredValue(storageKey: string | null): string | null {
  if (!storageKey) return null
  try {
    return storage()?.getItem(storageKey) ?? null
  } catch {
    return null
  }
}

export function writeStoredValue(storageKey: string | null, value: string): boolean {
  const store = storage()
  if (!storageKey || !store) return false
  try {
    store.setItem(storageKey, value)
    return true
  } catch {
    return false
  }
}

export function getStringPreference(key: PreferenceKey, fallback = ''): string {
  return readStoredValue(preferenceStorageKey(key)) ?? fallback
}

export function setStringPreference(key: PreferenceKey, value: string): boolean {
  return writeStoredValue(preferenceStorageKey(key), value)
}

export function getBooleanPreference(key: PreferenceKey, fallback = false): boolean {
  const value = getStringPreference(key)
  if (!value) return fallback
  return value === 'true'
}

export function setBooleanPreference(key: PreferenceKey, value: boolean): void {
  setStringPreference(key, value ? 'true' : 'false')
}

export function getNumberPreference(
  key: PreferenceKey,
  fallback: number,
  options: { allowed?: readonly number[]; min?: number; max?: number } = {},
): number {
  const parsed = Number(getStringPreference(key))
  if (!Number.isFinite(parsed)) return fallback
  const next = Math.trunc(parsed)
  if (options.allowed && !options.allowed.includes(next)) return fallback
  if (typeof options.min === 'number' && next < options.min) return fallback
  if (typeof options.max === 'number' && next > options.max) return fallback
  return next
}

export function setNumberPreference(key: PreferenceKey, value: number): void {
  setStringPreference(key, String(value))
}

export function getJsonPreference<T>(key: PreferenceKey, fallback: T): T {
  const value = getStringPreference(key)
  if (!value) return fallback
  try {
    return JSON.parse(value) as T
  } catch {
    return fallback
  }
}

export function setJsonPreference(key: PreferenceKey, value: unknown): boolean {
  return setStringPreference(key, JSON.stringify(value))
}

export function removePreference(key: PreferenceKey): void {
  const storageKey = preferenceStorageKey(key)
  if (!storageKey) return
  try {
    storage()?.removeItem(storageKey)
  } catch {
    // Storage can be unavailable in private browsing modes.
  }
}
