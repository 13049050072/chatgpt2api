export type StudioComposeMode = 'chat' | 'image' | 'search' | 'file'
export type StudioFileKind = 'ppt' | 'psd'
export type StudioRole = 'user' | 'assistant'
export type StudioMessageStatus = 'sending' | 'streaming' | 'queued' | 'running' | 'done' | 'error'
export type StudioConversationBadgeState = 'running' | 'done' | 'error'

export interface StudioConversationBadge {
  state: StudioConversationBadgeState
  label: string
  count?: number
}

export interface StudioMessage {
  id: string
  role: StudioRole
  mode: StudioComposeMode
  content: string
  createdAt: string
  status?: StudioMessageStatus
  model?: string
  imageSize?: string
  imageCount?: number
  taskId?: string
  fileTaskId?: string
  fileTaskDeleted?: boolean
  fileKind?: StudioFileKind
  error?: string
  attachments?: string[]
  referenceImages?: StudioReferenceImage[]
  /** Prompt actually sent upstream when it differs from the displayed content. */
  requestPrompt?: string
  /** Number of image files sent with the request; retries must resend the same images. */
  requestFileCount?: number
  inpaintSource?: StudioImageCompareSource
  searchSources?: StudioSearchSource[]
  searchImageGroups?: StudioSearchImageGroup[]
}

export interface StudioSearchSource {
  title?: string
  url?: string
  snippet?: string
}

export interface StudioSearchImageGroup {
  queries: string[]
  aspectRatio?: string
  numPerQuery?: number
}

export interface StudioConversation {
  id: string
  title: string
  createdAt: string
  updatedAt: string
  messages: StudioMessage[]
}

export interface StudioReferenceImage {
  id: string
  name: string
  type: string
  size: number
  dataUrl: string
}

export type StudioReference = StudioReferenceImage

export interface StudioImageAssetView {
  url: string
  path: string
  width?: number
  height?: number
}

export interface StudioImageCompareSource {
  src: string
  name: string
  localPath?: string
}

export interface StudioImageComparePreview {
  before: StudioImageCompareSource
  after: StudioImageCompareSource
}

export interface StudioImageForm {
  model: string
  size: string
  quality: string
  n: number
  outputFormat: string
  frameCount: string
  frameDurationMs: string
}

export interface StudioPreviewImage {
  src: string
  name: string
  localPath?: string
}
