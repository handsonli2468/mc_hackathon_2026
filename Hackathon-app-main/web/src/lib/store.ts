// Per-browser conveniences only (prompt per run, the open conversation). Storage may be unavailable.
import type { ChatReply } from '../api/types'

const PROMPTS = 'hackathon-app:prompts'
const CHAT = 'hackathon-app:chat'

function read<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : fallback
  } catch {
    return fallback
  }
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* storage unavailable or full */
  }
}

export function rememberPrompt(runId: string, prompt: string): void {
  const all = read<Record<string, string>>(PROMPTS, {})
  all[runId] = prompt
  const keys = Object.keys(all)
  for (const k of keys.slice(0, Math.max(0, keys.length - 50))) delete all[k]
  write(PROMPTS, all)
}

export function promptFor(runId: string): string {
  return read<Record<string, string>>(PROMPTS, {})[runId] ?? ''
}

export interface ChatMessage {
  role: 'user' | 'agent'
  text: string
  reply?: ChatReply
}

export interface ChatState {
  sessionId: string | null
  messages: ChatMessage[]
}

export function loadChat(): ChatState {
  return read<ChatState>(CHAT, { sessionId: null, messages: [] })
}

export function saveChat(state: ChatState): void {
  // Only the last 40 messages; drop the XML of older plans to stay small.
  const messages = state.messages.slice(-40).map((m, i, arr) =>
    m.reply && i < arr.length - 1 ? { ...m, reply: { ...m.reply, bt_xml: null } } : m,
  )
  write(CHAT, { ...state, messages })
}

export function clearChat(): void {
  try {
    localStorage.removeItem(CHAT)
  } catch {
    /* storage unavailable */
  }
}
