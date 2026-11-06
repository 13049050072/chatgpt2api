import { nextTick, type Ref } from 'vue'
import { editableFileTasksApi } from '@/api/editableFileTasks'
import { normalizeImageCount } from '@/api/imageTasks'
import type {
  StudioComposeMode,
  StudioConversation,
  StudioFileKind,
  StudioImageCompareSource,
  StudioImageForm,
  StudioMessage,
  StudioReferenceImage,
} from '@/components/studio/types'
import type { useToast } from '@/composables/useToast'
import { dataUrlToFile } from '@/lib/files'
import { createStudioImageTask, runStudioSearchRequest, studioErrorMessage, studioModeRequestErrorFallback, studioModeRetryErrorFallback } from './studioRequestView'
import type { useStudioChatStreamRuntime } from './studioChatStreamRuntime'
import type { useStudioComposerRuntime } from './studioComposerRuntime'
import type { useStudioFileTaskRuntime } from './studioFileTaskRuntime'
import type { useStudioImageTaskRuntime } from './studioImageTaskRuntime'
import type { useStudioMessageRuntime } from './studioMessageRuntime'
import type { useStudioReferenceRuntime } from './studioReferenceRuntime'

export type StudioSendRuntimeHooks = {
  activeConversationId: Ref<string>
  ensureConversation: (content?: string) => StudioConversation
  markConversationNotice: (conversationId: string, state: 'done' | 'error' | 'running') => void
  clearConversationNotice: (conversationId: string) => void
  touchConversation: (conversation: StudioConversation) => void
  scheduleScrollToBottom: () => void
}

export type StudioSendRuntimeInput = {
  composerRuntime: ReturnType<typeof useStudioComposerRuntime>
  referenceRuntime: ReturnType<typeof useStudioReferenceRuntime>
  messageRuntime: ReturnType<typeof useStudioMessageRuntime>
  chatStreamRuntime: ReturnType<typeof useStudioChatStreamRuntime>
  imageTaskRuntime: Pick<ReturnType<typeof useStudioImageTaskRuntime>, 'rememberTask' | 'merge' | 'schedulePoll'>
  fileTaskRuntime: Pick<ReturnType<typeof useStudioFileTaskRuntime>, 'merge' | 'schedulePoll'>
  chatModel: Ref<string>
  chatReasoningEffort: Ref<string>
  imageForm: StudioImageForm
  toast: Pick<ReturnType<typeof useToast>, 'success' | 'error'>
  hooks: StudioSendRuntimeHooks
}

export type StudioImageEditRequest = {
  prompt: string
  files: File[]
  userContent?: string
  referenceImages?: StudioReferenceImage[]
  assistantContent?: string
  inpaintSource?: StudioImageCompareSource
  imageCount?: number
}

const REQUEST_FILE_CACHE_LIMIT = 12
const MISSING_SOURCE_IMAGES_MESSAGE = '原图没有保存在本地，请重新上传原图或重新进行局部修改后再提交'
// Exact files of recent image requests; persisted history may only keep previews.
const requestFilesByMessageId = new Map<string, File[]>()

function rememberRequestFiles(messageId: string, files: File[]) {
  if (!messageId || !files.length) return
  requestFilesByMessageId.delete(messageId)
  requestFilesByMessageId.set(messageId, files.slice())
  while (requestFilesByMessageId.size > REQUEST_FILE_CACHE_LIMIT) {
    const oldest = requestFilesByMessageId.keys().next().value
    if (oldest === undefined) break
    requestFilesByMessageId.delete(oldest)
  }
}

function expectedRequestFileCount(message: StudioMessage) {
  if (message.mode !== 'image') return 0
  return message.requestFileCount ?? message.attachments?.length ?? 0
}

function referenceImageToFile(image: StudioReferenceImage) {
  return dataUrlToFile(image.dataUrl, image.name || 'reference.png', image.type || 'image/png')
}

/** Files originally sent with an image request, or null when they cannot be rebuilt. */
async function recoverRequestFiles(message: StudioMessage): Promise<File[] | null> {
  const expected = expectedRequestFileCount(message)
  if (!expected) return []
  const cached = requestFilesByMessageId.get(message.id)
  if (cached?.length === expected) return cached
  const images = (message.referenceImages || []).filter((image) => image.dataUrl)
  if (images.length !== expected) return null
  try {
    const files = await Promise.all(images.map(referenceImageToFile))
    rememberRequestFiles(message.id, files)
    return files
  } catch {
    return null
  }
}

export function useStudioSendRuntime(input: StudioSendRuntimeInput) {
  const {
    chatModel,
    chatReasoningEffort,
    chatStreamRuntime,
    composerRuntime,
    hooks,
    imageForm,
    imageTaskRuntime,
    fileTaskRuntime,
    messageRuntime,
    referenceRuntime,
    toast,
  } = input

  async function sendMessage() {
    const content = composerRuntime.composerText.value.trim()
    if (!content || composerRuntime.isSending.value || chatStreamRuntime.isStreaming.value) return

    if (composerRuntime.editingMessageId.value) {
      await sendEditedMessage(content)
      return
    }

    const mode = composerRuntime.composeMode.value
    if (!validateFileRequest(mode)) return
    const conversation = hooks.ensureConversation(content)
    const withReferences = mode === 'image' || mode === 'chat' || mode === 'file'
    const files = referenceRuntime.selectedFiles()
    const attachments = referenceRuntime.attachmentNames()
    const referenceImages = withReferences ? referenceRuntime.messageReferenceImages() : []
    const clearReferencesImmediately = mode === 'chat' && referenceImages.length > 0

    const userMessage = messageRuntime.addMessage(conversation, {
      role: 'user',
      mode,
      content,
      status: 'done',
      fileKind: mode === 'file' ? composerRuntime.fileKind.value : undefined,
      attachments: withReferences && attachments.length ? attachments : undefined,
      referenceImages: buildMessageReferenceImages(referenceImages),
      requestFileCount: mode === 'image' && files.length ? files.length : undefined,
    })
    if (mode === 'image') rememberRequestFiles(userMessage.id, files)
    composerRuntime.composerText.value = ''
    if (clearReferencesImmediately) referenceRuntime.clear()
    await runRequestWithComposerState({
      mode,
      conversation,
      prompt: content,
      files,
      requestErrorFallback: studioModeRequestErrorFallback(mode),
      clearReferencesOnSuccess: withReferences && !clearReferencesImmediately,
    })
  }

  async function sendEditedMessage(content: string) {
    const target = messageRuntime.findMessage(composerRuntime.editingMessageId.value)
    if (!target || target.message.role !== 'user') {
      composerRuntime.editingMessageId.value = ''
      return
    }

    const { conversation, message } = target
    const mode = composerRuntime.composeMode.value
    if (!validateFileRequest(mode)) return
    const withReferences = mode === 'image' || mode === 'chat' || mode === 'file'
    const files = referenceRuntime.selectedFiles()
    if (mode === 'image' && message.requestPrompt && !files.length) {
      // The inpainting prompt and marked image only exist in the inpaint panel.
      toast.error('局部修改请在图片上重新打开局部修改面板提交')
      return
    }
    const attachments = referenceRuntime.attachmentNames()
    const referenceImages = withReferences ? referenceRuntime.messageReferenceImages() : []
    const clearReferencesImmediately = mode === 'chat' && referenceImages.length > 0
    const editedMessage: StudioMessage = {
      ...message,
      mode,
      content,
      status: 'done',
      error: undefined,
      fileKind: mode === 'file' ? composerRuntime.fileKind.value : undefined,
      attachments: withReferences && attachments.length ? attachments : undefined,
      referenceImages: buildMessageReferenceImages(referenceImages),
      requestPrompt: undefined,
      requestFileCount: mode === 'image' && files.length ? files.length : undefined,
    }
    if (mode === 'image') rememberRequestFiles(editedMessage.id, files)

    hooks.activeConversationId.value = conversation.id
    messageRuntime.replaceFromTarget(target, editedMessage)
    composerRuntime.editingMessageId.value = ''
    composerRuntime.composerText.value = ''
    if (clearReferencesImmediately) referenceRuntime.clear()
    hooks.clearConversationNotice(conversation.id)

    await runRequestWithComposerState({
      mode,
      conversation,
      prompt: content,
      files,
      requestErrorFallback: studioModeRequestErrorFallback(mode),
      clearReferencesOnSuccess: withReferences && !clearReferencesImmediately,
    })
  }

  async function sendImageEditRequest(request: StudioImageEditRequest) {
    const prompt = request.prompt.trim()
    if (!prompt || composerRuntime.isSending.value || chatStreamRuntime.isStreaming.value) return false

    const userContent = request.userContent?.trim() || prompt
    const conversation = hooks.ensureConversation(userContent)
    hooks.activeConversationId.value = conversation.id
    composerRuntime.cancelMessageEdit(false)
    composerRuntime.activateImageMode()

    const userMessage = messageRuntime.addMessage(conversation, {
      role: 'user',
      mode: 'image',
      content: userContent,
      status: 'done',
      attachments: request.files.length ? request.files.map((file) => file.name).filter(Boolean) : undefined,
      referenceImages: buildMessageReferenceImages(request.referenceImages || []),
      requestPrompt: prompt !== userContent ? prompt : undefined,
      requestFileCount: request.files.length || undefined,
    })
    rememberRequestFiles(userMessage.id, request.files)

    return runRequestWithComposerState({
      mode: 'image',
      conversation,
      prompt,
      files: request.files,
      requestErrorFallback: '图片编辑任务提交失败',
      assistantContent: request.assistantContent,
      inpaintSource: request.inpaintSource,
      imageCount: request.imageCount,
    })
  }

  function fillComposerFromMessage(message: StudioMessage) {
    composerRuntime.cancelMessageEdit(false)
    composerRuntime.fillFromMessage(message)
  }

  async function editMessage(message: StudioMessage) {
    const target = messageRuntime.findMessage(message.id)
    if (!target || target.message.role !== 'user') return
    hooks.activeConversationId.value = target.conversation.id
    composerRuntime.startEdit(target.message)
    referenceRuntime.clear()
    if (target.message.mode === 'file' && (target.message.fileKind === 'psd' || target.message.referenceImages?.length)) {
      toast.error('请重新上传原图后提交文件任务')
    }
    if (expectedRequestFileCount(target.message)) {
      // Inpainting prompts are built outside the composer and cannot be edited here.
      const files = target.message.requestPrompt ? null : await recoverRequestFiles(target.message)
      if (files?.length) await referenceRuntime.append(files)
      else toast.error(MISSING_SOURCE_IMAGES_MESSAGE)
    }
    hooks.scheduleScrollToBottom()
  }

  async function resendMessage(message: StudioMessage) {
    if (composerRuntime.isSending.value || chatStreamRuntime.isStreaming.value) return
    if (message.mode === 'file') {
      referenceRuntime.clear()
      if (message.fileKind === 'psd' || message.referenceImages?.length) {
        fillComposerFromMessage(message)
        toast.error('请重新上传原图后提交文件任务')
        return
      }
    }
    if (expectedRequestFileCount(message)) {
      const files = await recoverRequestFiles(message)
      if (!files) {
        fillComposerFromMessage(message)
        toast.error(MISSING_SOURCE_IMAGES_MESSAGE)
        return
      }
      if (message.requestPrompt) {
        // Built outside the composer (e.g. inpainting): resend the same request.
        const target = messageRuntime.findMessage(message.id)
        const reply = target?.conversation.messages
          .slice(target.index + 1)
          .find((item) => item.role === 'assistant')
        await sendImageEditRequest({
          prompt: message.requestPrompt,
          files,
          userContent: message.content,
          referenceImages: message.referenceImages,
          assistantContent: reply?.inpaintSource ? '局部修改任务已提交' : undefined,
          inpaintSource: reply?.inpaintSource,
          imageCount: reply?.imageCount,
        })
        return
      }
      referenceRuntime.clear()
      await referenceRuntime.append(files)
    }
    fillComposerFromMessage(message)
    await nextTick()
    await sendMessage()
  }

  async function retryAssistantMessage(message: StudioMessage) {
    if (composerRuntime.isSending.value || chatStreamRuntime.isStreaming.value) return
    const target = messageRuntime.findMessage(message.id)
    if (!target) return
    const previousUserMessage = messageRuntime.findPreviousUserMessage(target)
    if (!previousUserMessage) return

    if (previousUserMessage.mode === 'file' && (previousUserMessage.fileKind === 'psd' || previousUserMessage.referenceImages?.length)) {
      referenceRuntime.clear()
      fillComposerFromMessage(previousUserMessage)
      toast.error('请重新上传原图后提交文件任务')
      return
    }

    if (previousUserMessage.mode === 'file') referenceRuntime.clear()

    // Image edits must be retried with their original images; without them the
    // request silently becomes a text-to-image generation.
    const files = await recoverRequestFiles(previousUserMessage)
    if (!files) {
      toast.error(MISSING_SOURCE_IMAGES_MESSAGE)
      return
    }

    hooks.activeConversationId.value = target.conversation.id
    messageRuntime.pruneAfterTarget(target)
    hooks.clearConversationNotice(target.conversation.id)

    await runRequestWithComposerState({
      mode: previousUserMessage.mode,
      conversation: target.conversation,
      prompt: previousUserMessage.requestPrompt || previousUserMessage.content,
      files,
      requestErrorFallback: studioModeRetryErrorFallback(previousUserMessage.mode),
      fileKind: previousUserMessage.fileKind,
      assistantContent: message.inpaintSource ? '局部修改任务已提交' : undefined,
      inpaintSource: message.inpaintSource,
      imageCount: message.imageCount,
    })
  }

  async function runRequestWithComposerState(input: {
    mode: StudioComposeMode
    conversation: StudioConversation
    prompt: string
    files: File[]
    requestErrorFallback: string
    clearReferencesOnSuccess?: boolean
    assistantContent?: string
    inpaintSource?: StudioImageCompareSource
    imageCount?: number
    fileKind?: StudioFileKind
  }) {
    composerRuntime.setSending(true)
    try {
      const success = await sendByMode(input.conversation, input.mode, input.prompt, input.files, {
        assistantContent: input.assistantContent,
        inpaintSource: input.inpaintSource,
        imageCount: input.imageCount,
        fileKind: input.fileKind,
      })
      if (success && input.clearReferencesOnSuccess) {
        referenceRuntime.clear()
      }
      return success
    } catch (error) {
      const message = studioErrorMessage(error, input.requestErrorFallback)
      hooks.markConversationNotice(input.conversation.id, 'error')
      messageRuntime.addMessage(input.conversation, {
        role: 'assistant',
        mode: input.mode,
        content: message,
        status: 'error',
        error: message,
      })
      return false
    } finally {
      composerRuntime.setSending(false)
      hooks.scheduleScrollToBottom()
    }
  }

  async function sendByMode(
    conversation: StudioConversation,
    mode: StudioComposeMode,
    prompt: string,
    files: File[],
    imageOptions: {
      assistantContent?: string
      inpaintSource?: StudioImageCompareSource
      imageCount?: number
      fileKind?: StudioFileKind
    } = {},
  ) {
    if (mode === 'chat') {
      await sendTextMessage(conversation)
      return true
    } else if (mode === 'search') {
      return sendSearchMessage(conversation, prompt)
    } else if (mode === 'file') {
      return sendFileMessage(conversation, prompt, imageOptions.fileKind || composerRuntime.fileKind.value)
    } else {
      return sendImageMessage(conversation, prompt, files, imageOptions)
    }
  }

  async function sendTextMessage(conversation: StudioConversation) {
    const assistantMessage = messageRuntime.addMessage(conversation, {
      role: 'assistant',
      mode: 'chat',
      content: '',
      status: 'streaming',
      model: chatModel.value,
    })

    await chatStreamRuntime.stream({
      conversation,
      assistantMessage,
      model: chatModel.value,
      reasoningEffort: chatReasoningEffort.value,
    })
  }

  async function sendSearchMessage(conversation: StudioConversation, prompt: string) {
    const assistantMessage = messageRuntime.addMessage(conversation, {
      role: 'assistant',
      mode: 'search',
      content: '正在搜索...',
      status: 'sending',
      model: 'search',
    })

    try {
      const result = await runStudioSearchRequest(prompt, assistantMessage.id)
      assistantMessage.searchSources = result.sources
      assistantMessage.searchImageGroups = result.imageGroups
      assistantMessage.content = result.content
      assistantMessage.status = 'done'
      hooks.markConversationNotice(conversation.id, 'done')
      return true
    } catch (error) {
      const message = studioErrorMessage(error, '搜索请求失败')
      assistantMessage.status = 'error'
      assistantMessage.content = message
      assistantMessage.error = message
      hooks.markConversationNotice(conversation.id, 'error')
      return false
    } finally {
      hooks.touchConversation(conversation)
      hooks.scheduleScrollToBottom()
    }
  }

  async function sendImageMessage(
    conversation: StudioConversation,
    prompt: string,
    files: File[],
    options: {
      assistantContent?: string
      inpaintSource?: StudioImageCompareSource
      imageCount?: number
    } = {},
  ) {
    const imageCount = normalizeImageCount(options.imageCount ?? imageForm.n)
    const assistantMessage = messageRuntime.addMessage(conversation, {
      role: 'assistant',
      mode: 'image',
      content: options.assistantContent || (files.length ? '图像编辑任务已提交' : '图片任务已提交'),
      status: 'queued',
      model: imageForm.model,
      imageSize: imageForm.size,
      imageCount,
      inpaintSource: options.inpaintSource,
    })

    try {
      const task = await createStudioImageTask({
        prompt,
        files,
        imageForm: {
          ...imageForm,
          n: imageCount,
        },
      })
      assistantMessage.taskId = task.id
      assistantMessage.status = 'running'
      hooks.touchConversation(conversation)
      imageTaskRuntime.rememberTask(task.id)
      imageTaskRuntime.merge([task])
      toast.success('图片任务已提交')
      imageTaskRuntime.schedulePoll()
      return true
    } catch (error) {
      const message = studioErrorMessage(error, '图片任务提交失败')
      assistantMessage.status = 'error'
      assistantMessage.content = message
      assistantMessage.error = message
      hooks.touchConversation(conversation)
      hooks.markConversationNotice(conversation.id, 'error')
      return false
    }
  }

  async function sendFileMessage(
    conversation: StudioConversation,
    prompt: string,
    kind: StudioFileKind,
  ) {
    const label = kind === 'psd' ? 'PSD' : 'PPT'
    const assistantMessage = messageRuntime.addMessage(conversation, {
      role: 'assistant',
      mode: 'file',
      content: `${label} 文件任务已提交`,
      status: 'queued',
      fileKind: kind,
    })

    try {
      const task = await editableFileTasksApi.create({
        kind,
        prompt,
        base64Images: referenceRuntime.references.value.map((reference) => reference.dataUrl).filter(Boolean),
      })
      assistantMessage.fileTaskId = task.id
      assistantMessage.status = task.status === 'success' ? 'done' : task.status === 'error' ? 'error' : task.status
      assistantMessage.error = task.status === 'error' ? task.error : undefined
      hooks.touchConversation(conversation)
      fileTaskRuntime.merge([task])
      if (task.status === 'error') {
        hooks.markConversationNotice(conversation.id, 'error')
        return false
      }
      if (task.status === 'success') {
        hooks.markConversationNotice(conversation.id, 'done')
      } else {
        hooks.markConversationNotice(conversation.id, 'running')
        fileTaskRuntime.schedulePoll()
      }
      toast.success(`${label} 文件任务已提交`)
      return true
    } catch (error) {
      const message = studioErrorMessage(error, '文件任务提交失败')
      assistantMessage.status = 'error'
      assistantMessage.content = message
      assistantMessage.error = message
      hooks.touchConversation(conversation)
      hooks.markConversationNotice(conversation.id, 'error')
      return false
    }
  }

  function validateFileRequest(mode: StudioComposeMode) {
    if (mode !== 'file' || composerRuntime.fileKind.value !== 'psd' || referenceRuntime.references.value.length) {
      return true
    }
    toast.error('PSD 文件任务至少需要上传一张原图')
    return false
  }

  function buildMessageReferenceImages(referenceImages: StudioReferenceImage[]) {
    return referenceImages.length ? referenceImages : undefined
  }

  return {
    editMessage,
    fillComposerFromMessage,
    resendMessage,
    retryAssistantMessage,
    sendImageEditRequest,
    sendMessage,
  }
}
