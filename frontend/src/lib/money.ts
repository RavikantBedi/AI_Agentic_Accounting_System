/**
 * Money formatting and safe arithmetic for the client.
 *
 * Amounts arrive from the API as decimal *strings*. Anything that must stay
 * exact (totals, comparisons) is done in integer paise; JavaScript numbers are
 * only used for chart geometry, where a float is harmless.
 */

export type Money = string | number | null | undefined

/** Parse a decimal string into integer paise, without ever going via float. */
export function toPaise(value: Money): number {
  if (value === null || value === undefined || value === '') return 0
  const text = String(value).trim().replace(/[₹,\s]/g, '')
  if (!text || text === '-') return 0

  const negative = text.startsWith('-')
  const digits = negative ? text.slice(1) : text
  const [whole = '0', fraction = ''] = digits.split('.')

  const paise =
    Number(whole || '0') * 100 + Number((fraction + '00').slice(0, 2) || '0')
  return negative ? -paise : paise
}

export function fromPaise(paise: number): string {
  const negative = paise < 0
  const absolute = Math.abs(Math.round(paise))
  const text = `${Math.floor(absolute / 100)}.${String(absolute % 100).padStart(2, '0')}`
  return negative ? `-${text}` : text
}

/** Exact addition over decimal strings. */
export function sum(...values: Money[]): string {
  return fromPaise(values.reduce<number>((total, value) => total + toPaise(value), 0))
}

/** For charts and ratios only — never for a displayed total. */
export function toNumber(value: Money): number {
  return toPaise(value) / 100
}

/** Indian digit grouping: 1,23,45,678.90 */
function groupIndian(whole: string): string {
  if (whole.length <= 3) return whole
  const head = whole.slice(0, -3)
  const tail = whole.slice(-3)
  const groups: string[] = []
  let rest = head
  while (rest.length > 2) {
    groups.unshift(rest.slice(-2))
    rest = rest.slice(0, -2)
  }
  if (rest) groups.unshift(rest)
  return `${groups.join(',')},${tail}`
}

export interface FormatOptions {
  decimals?: boolean
  symbol?: boolean
  compact?: boolean
  signed?: boolean
}

export function formatMoney(value: Money, options: FormatOptions = {}): string {
  const { decimals = true, symbol = true, compact = false, signed = false } = options
  const paise = toPaise(value)
  const negative = paise < 0
  const absolute = Math.abs(paise)

  if (compact) {
    const rupees = absolute / 100
    let text: string
    if (rupees >= 1_00_00_000) text = `${(rupees / 1_00_00_000).toFixed(2)}Cr`
    else if (rupees >= 1_00_000) text = `${(rupees / 1_00_000).toFixed(2)}L`
    else if (rupees >= 1_000) text = `${(rupees / 1_000).toFixed(1)}K`
    else text = rupees.toFixed(0)
    const prefix = symbol ? '₹' : ''
    const sign = negative ? '-' : signed ? '+' : ''
    return `${sign}${prefix}${text}`
  }

  const whole = String(Math.floor(absolute / 100))
  const fraction = String(absolute % 100).padStart(2, '0')
  const body = decimals ? `${groupIndian(whole)}.${fraction}` : groupIndian(whole)
  const prefix = symbol ? '₹' : ''
  const sign = negative ? '-' : signed ? '+' : ''
  return `${sign}${prefix}${body}`
}

export function formatQuantity(value: Money, uom?: string): string {
  const numeric = Number(String(value ?? 0))
  // Trim trailing zeros: 5.0000 reads as 5, but 5.2500 reads as 5.25.
  const text = Number.isFinite(numeric)
    ? String(parseFloat(numeric.toFixed(4)))
    : String(value ?? '0')
  return uom ? `${text} ${uom}` : text
}

export function formatPercent(value: Money, decimals = 1): string {
  const numeric = Number(String(value ?? 0))
  if (!Number.isFinite(numeric)) return '—'
  return `${numeric.toFixed(decimals)}%`
}

export function isNegative(value: Money): boolean {
  return toPaise(value) < 0
}

export function isZero(value: Money): boolean {
  return toPaise(value) === 0
}

/** Percentage change, or null when there is no meaningful base to compare to. */
export function percentChange(current: Money, previous: Money): number | null {
  const previousPaise = toPaise(previous)
  if (previousPaise === 0) return null
  return ((toPaise(current) - previousPaise) / Math.abs(previousPaise)) * 100
}

const DATE_FORMAT = new Intl.DateTimeFormat('en-IN', {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
})

const DATETIME_FORMAT = new Intl.DateTimeFormat('en-IN', {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})

export function formatDate(value?: string | Date | null): string {
  if (!value) return '—'
  const date = typeof value === 'string' ? new Date(value) : value
  return Number.isNaN(date.getTime()) ? '—' : DATE_FORMAT.format(date)
}

export function formatDateTime(value?: string | Date | null): string {
  if (!value) return '—'
  const date = typeof value === 'string' ? new Date(value) : value
  return Number.isNaN(date.getTime()) ? '—' : DATETIME_FORMAT.format(date)
}

export function formatRelative(value?: string | Date | null): string {
  if (!value) return '—'
  const date = typeof value === 'string' ? new Date(value) : value
  if (Number.isNaN(date.getTime())) return '—'

  const seconds = Math.round((Date.now() - date.getTime()) / 1000)
  if (seconds < 60) return 'just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`
  return formatDate(date)
}

export function todayISO(): string {
  return new Date().toISOString().slice(0, 10)
}

export function monthStartISO(): string {
  const now = new Date()
  return new Date(now.getFullYear(), now.getMonth(), 1).toISOString().slice(0, 10)
}

/** Indian fiscal year starts on 1 April. */
export function fiscalYearStartISO(): string {
  const now = new Date()
  const year = now.getMonth() >= 3 ? now.getFullYear() : now.getFullYear() - 1
  return `${year}-04-01`
}

export function daysBetween(from: string, to: string): number {
  return Math.round(
    (new Date(to).getTime() - new Date(from).getTime()) / (1000 * 60 * 60 * 24),
  )
}
