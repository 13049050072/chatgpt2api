import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import {
  authApi,
  type AuthCapabilities,
  type AuthCapability,
  type AuthSubject,
  type AuthView,
} from '@/api/auth'
import { getAuthToken } from '@/api/client'
import { setPreferenceScope } from '@/lib/preferences'

const AUTH_CACHE_MS = 60000

function isUnauthorizedError(error: unknown) {
  return Boolean(error && typeof error === 'object' && (error as { status?: number }).status === 401)
}

function emptyCapabilities(): AuthCapabilities {
  return { admin_console: false, studio: false }
}

export const useAuthStore = defineStore('auth', () => {
  const isLoggedIn = ref(false)
  const isLoading = ref(false)
  const subject = ref<AuthSubject | null>(null)
  const capabilities = ref<AuthCapabilities>(emptyCapabilities())
  const homeRoute = ref<AuthView['home_route']>('/login')
  const version = ref('')
  const lastCheckedAt = ref(0)
  let checkPromise: Promise<boolean> | null = null
  let sessionGeneration = 0

  const role = computed(() => subject.value?.role || '')
  const subjectId = computed(() => subject.value?.id || '')
  const name = computed(() => subject.value?.name || '')
  const isAdmin = computed(() => capabilities.value.admin_console)

  function hasCapability(capability: AuthCapability) {
    return capabilities.value[capability] === true
  }

  function applyStatus(status: AuthView) {
    if (!status.authenticated || !status.subject) {
      clearIdentity()
      version.value = status.version
      return false
    }
    const previousSubjectId = subject.value?.id
    if (previousSubjectId && previousSubjectId !== status.subject.id) {
      // Another tab switched keys; in-memory state still belongs to the old subject.
      window.location.reload()
      return false
    }
    isLoggedIn.value = true
    subject.value = status.subject
    capabilities.value = {
      admin_console: status.capabilities.admin_console === true,
      studio: status.capabilities.studio === true,
    }
    homeRoute.value = status.home_route
    version.value = status.version
    setPreferenceScope(status.subject.id, { claimLegacy: status.subject.role === 'admin' })
    return true
  }

  function clearIdentity() {
    isLoggedIn.value = false
    subject.value = null
    capabilities.value = emptyCapabilities()
    homeRoute.value = '/login'
    setPreferenceScope('')
  }

  async function login(password: string) {
    sessionGeneration += 1
    isLoading.value = true
    try {
      const status = await authApi.login({ password })
      const authenticated = applyStatus(status)
      lastCheckedAt.value = Date.now()
      return authenticated
    } catch (error) {
      clearIdentity()
      throw error
    } finally {
      isLoading.value = false
    }
  }

  async function logout() {
    sessionGeneration += 1
    try {
      await authApi.logout()
    } finally {
      clearIdentity()
      lastCheckedAt.value = 0
    }
  }

  async function checkAuth() {
    if (!getAuthToken()) {
      clearIdentity()
      lastCheckedAt.value = 0
      checkPromise = null
      return false
    }
    const now = Date.now()
    if (now - lastCheckedAt.value < AUTH_CACHE_MS) {
      return isLoggedIn.value
    }
    if (checkPromise) {
      return checkPromise
    }
    const generation = sessionGeneration
    // Shared by concurrent callers, so it resolves instead of rejecting.
    checkPromise = (async () => {
      try {
        const status = await authApi.checkAuth()
        // A check that started before logout or login must not apply its result.
        if (generation !== sessionGeneration || !getAuthToken()) return isLoggedIn.value
        const authenticated = applyStatus(status)
        lastCheckedAt.value = Date.now()
        return authenticated
      } catch (error) {
        // Timeouts, resets and 5xx say nothing about the key; only 401 ends the session.
        if (generation === sessionGeneration && isUnauthorizedError(error)) {
          clearIdentity()
        }
        lastCheckedAt.value = 0
        return isLoggedIn.value
      } finally {
        checkPromise = null
      }
    })()
    return checkPromise
  }

  return {
    isLoggedIn,
    isLoading,
    subject,
    capabilities,
    homeRoute,
    version,
    role,
    subjectId,
    name,
    isAdmin,
    hasCapability,
    login,
    logout,
    checkAuth,
    clearIdentity,
  }
})
