/**
 * Design-system primitives.
 *
 * Hand-rolled rather than pulled from a component CLI, so every element is
 * theme-token driven, accessible by default, and consistent across the app.
 */
import {
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { createPortal } from 'react-dom'
import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  Check,
  ChevronDown,
  Info,
  Loader2,
  X,
  type LucideIcon,
} from 'lucide-react'
import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'
import { formatMoney, formatPercent, isNegative, type Money } from '@/lib/money'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/* ------------------------------------------------------------------ Button */

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
type ButtonSize = 'sm' | 'md' | 'lg'

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  icon?: LucideIcon
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', loading, icon: Icon, className, children, disabled, ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      className={cn(`btn-${variant}`, `btn-${size}`, className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading ? (
        <Loader2 className="h-4 w-4 shrink-0 animate-spin" aria-hidden />
      ) : Icon ? (
        <Icon className="h-4 w-4 shrink-0" aria-hidden />
      ) : null}
      {children}
    </button>
  )
})

/* -------------------------------------------------------------------- Card */

export function Card({
  title,
  description,
  actions,
  children,
  className,
  bodyClassName,
  footer,
}: {
  title?: ReactNode
  description?: ReactNode
  actions?: ReactNode
  children?: ReactNode
  className?: string
  bodyClassName?: string
  footer?: ReactNode
}) {
  return (
    <section className={cn('card flex flex-col', className)}>
      {(title || actions) && (
        <header className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            {title && <h2 className="truncate text-sm font-semibold text-strong">{title}</h2>}
            {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cn('flex-1', bodyClassName ?? 'p-4')}>{children}</div>
      {footer && <footer className="border-t border-border px-4 py-3">{footer}</footer>}
    </section>
  )
}

/* ------------------------------------------------------------------- Badge */

type Tone = 'neutral' | 'brand' | 'positive' | 'negative' | 'warning' | 'info'

const TONE_CLASS: Record<Tone, string> = {
  neutral: 'bg-raised text-muted',
  brand: 'bg-brand-soft text-brand',
  positive: 'bg-positive-soft text-positive',
  negative: 'bg-negative-soft text-negative',
  warning: 'bg-warning-soft text-warning',
  info: 'bg-info-soft text-info',
}

export function Badge({
  tone = 'neutral',
  children,
  className,
  icon: Icon,
}: {
  tone?: Tone
  children: ReactNode
  className?: string
  icon?: LucideIcon
}) {
  return (
    <span className={cn('badge', TONE_CLASS[tone], className)}>
      {Icon && <Icon className="h-3 w-3" aria-hidden />}
      {children}
    </span>
  )
}

/** Status vocabulary shared by documents, agents and exceptions. */
const STATUS_TONES: Record<string, Tone> = {
  PAID: 'positive',
  APPROVED: 'positive',
  COMPLETED: 'positive',
  POSTED: 'positive',
  MATCHED: 'positive',
  ACKNOWLEDGED: 'positive',
  RESOLVED: 'positive',
  ACTIVE: 'positive',
  FILED_EXTERNALLY: 'positive',
  OVERDUE: 'negative',
  FAILED: 'negative',
  CANCELLED: 'negative',
  BLOCKED: 'negative',
  EXCEPTION: 'negative',
  CRITICAL: 'negative',
  HIGH: 'negative',
  REJECTED: 'negative',
  PENDING: 'warning',
  PENDING_APPROVAL: 'warning',
  PARTIALLY_PAID: 'warning',
  PARTIAL: 'warning',
  NEEDS_REVIEW: 'warning',
  IN_PROGRESS: 'warning',
  AMBIGUOUS: 'warning',
  MEDIUM: 'warning',
  CLOSING: 'warning',
  READY_FOR_REVIEW: 'info',
  SENT: 'info',
  RUNNING: 'info',
  PREPARED: 'info',
  REVERSED: 'info',
  DRAFT: 'neutral',
  UNMATCHED: 'neutral',
  NOT_STARTED: 'neutral',
  OPEN: 'neutral',
  LOW: 'neutral',
  INFO: 'neutral',
}

export function StatusBadge({ status, className }: { status?: string; className?: string }) {
  if (!status) return <span className="text-subtle">—</span>
  const tone = STATUS_TONES[status.toUpperCase()] ?? 'neutral'
  return (
    <Badge tone={tone} className={className}>
      {status.replace(/_/g, ' ')}
    </Badge>
  )
}

/* ------------------------------------------------------------------- Money */

export function Amount({
  value,
  className,
  compact,
  colour = false,
  signed = false,
  decimals = true,
}: {
  value: Money
  className?: string
  compact?: boolean
  colour?: boolean
  signed?: boolean
  decimals?: boolean
}) {
  const negative = isNegative(value)
  return (
    <span
      className={cn(
        'tabular',
        colour && (negative ? 'text-negative' : 'text-positive'),
        className,
      )}
    >
      {formatMoney(value, { compact, signed, decimals })}
    </span>
  )
}

export function Delta({ percent, className }: { percent: number | null; className?: string }) {
  if (percent === null || !Number.isFinite(percent)) {
    return <span className={cn('text-2xs text-subtle', className)}>no prior period</span>
  }
  const up = percent >= 0
  const Icon = up ? ArrowUp : ArrowDown
  return (
    <span
      className={cn(
        'inline-flex items-center gap-0.5 text-2xs font-semibold tabular',
        up ? 'text-positive' : 'text-negative',
        className,
      )}
    >
      <Icon className="h-3 w-3" aria-hidden />
      {Math.abs(percent).toFixed(1)}%
    </span>
  )
}

/* --------------------------------------------------------------- KPI card */

export function StatCard({
  label,
  value,
  sub,
  delta,
  tone,
  icon: Icon,
  onClick,
  loading,
}: {
  label: string
  value: ReactNode
  sub?: ReactNode
  delta?: number | null
  tone?: Tone
  icon?: LucideIcon
  onClick?: () => void
  loading?: boolean
}) {
  const Wrapper: any = onClick ? 'button' : 'div'
  return (
    <Wrapper
      onClick={onClick}
      className={cn(
        'card flex flex-col gap-1 p-3.5 text-left',
        onClick && 'transition-colors hover:bg-raised focus-visible:bg-raised',
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-2xs font-medium uppercase tracking-wider text-muted">{label}</span>
        {Icon && (
          <span className={cn('rounded-md p-1', TONE_CLASS[tone ?? 'neutral'])}>
            <Icon className="h-3.5 w-3.5" aria-hidden />
          </span>
        )}
      </div>
      {loading ? (
        <div className="skeleton h-7 w-28" />
      ) : (
        <div className="truncate text-xl font-semibold tabular text-strong">{value}</div>
      )}
      <div className="flex items-center gap-2">
        {delta !== undefined && <Delta percent={delta ?? null} />}
        {sub && <span className="truncate text-2xs text-muted">{sub}</span>}
      </div>
    </Wrapper>
  )
}

/* ------------------------------------------------------------------ Table */

export interface Column<T> {
  key: string
  header: ReactNode
  render: (row: T, index: number) => ReactNode
  align?: 'left' | 'right' | 'center'
  width?: string
  /** Hidden below md — used to keep dense tables readable on small screens. */
  secondary?: boolean
}

export function DataTable<T>({
  columns,
  rows,
  loading,
  empty,
  onRowClick,
  rowKey,
  footer,
  dense,
  stickyHeader = true,
}: {
  columns: Column<T>[]
  rows: T[]
  loading?: boolean
  empty?: ReactNode
  onRowClick?: (row: T) => void
  rowKey: (row: T, index: number) => string
  footer?: ReactNode
  dense?: boolean
  stickyHeader?: boolean
}) {
  if (loading) {
    return (
      <div className="space-y-2 p-4" aria-busy="true" aria-label="Loading">
        {Array.from({ length: 6 }).map((_, index) => (
          <div key={index} className="skeleton h-9 w-full" />
        ))}
      </div>
    )
  }

  if (!rows.length) {
    return <div className="p-8">{empty ?? <EmptyState title="Nothing to show yet" />}</div>
  }

  return (
    <div className="scroll-x">
      <table className="w-full min-w-full border-collapse">
        <thead className={cn(stickyHeader && 'sticky top-0 z-10')}>
          <tr className="table-head">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                style={column.width ? { width: column.width } : undefined}
                className={cn(
                  'px-3 py-2 text-left font-semibold',
                  column.align === 'right' && 'text-right',
                  column.align === 'center' && 'text-center',
                  column.secondary && 'hidden md:table-cell',
                )}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {rows.map((row, index) => (
            <tr
              key={rowKey(row, index)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              tabIndex={onRowClick ? 0 : undefined}
              onKeyDown={
                onRowClick
                  ? (event) => {
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault()
                        onRowClick(row)
                      }
                    }
                  : undefined
              }
              className={cn(
                'transition-colors',
                onRowClick && 'cursor-pointer hover:bg-raised focus:bg-raised focus:outline-none',
              )}
            >
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={cn(
                    dense ? 'px-3 py-1.5 text-sm' : 'cell',
                    column.align === 'right' && 'text-right',
                    column.align === 'center' && 'text-center',
                    column.secondary && 'hidden md:table-cell',
                  )}
                >
                  {column.render(row, index)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
        {footer && <tfoot className="border-t-2 border-border bg-raised font-semibold">{footer}</tfoot>}
      </table>
    </div>
  )
}

/* ------------------------------------------------------------ Empty/Error */

export function EmptyState({
  title,
  description,
  action,
  icon: Icon,
}: {
  title: string
  description?: string
  action?: ReactNode
  icon?: LucideIcon
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-8 text-center">
      {Icon && (
        <span className="rounded-full bg-raised p-3">
          <Icon className="h-5 w-5 text-subtle" aria-hidden />
        </span>
      )}
      <p className="text-sm font-medium text-strong">{title}</p>
      {description && <p className="max-w-sm text-xs text-muted">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const message =
    (error as any)?.message ?? 'Something went wrong while loading this view.'
  const code = (error as any)?.code
  return (
    <div
      role="alert"
      className="flex flex-col items-center justify-center gap-2 py-8 text-center"
    >
      <span className="rounded-full bg-negative-soft p-3">
        <AlertTriangle className="h-5 w-5 text-negative" aria-hidden />
      </span>
      <p className="text-sm font-medium text-strong">Could not load this</p>
      <p className="max-w-md text-xs text-muted">{message}</p>
      {code && <p className="text-2xs text-subtle">Error code: {code}</p>}
      {retry && (
        <Button size="sm" onClick={retry} className="mt-2">
          Try again
        </Button>
      )}
    </div>
  )
}

export function Spinner({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      <span>{label}</span>
    </div>
  )
}

/* ------------------------------------------------------------------ Modal */

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
}: {
  open: boolean
  onClose: () => void
  title: string
  description?: string
  children: ReactNode
  footer?: ReactNode
  size?: 'sm' | 'md' | 'lg' | 'xl'
}) {
  const titleId = useId()
  const panelRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
      if (event.key === 'Tab' && panelRef.current) {
        // Keep focus inside the dialog.
        const focusable = panelRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select, textarea, [tabindex]:not([tabindex="-1"])',
        )
        if (!focusable.length) return
        const first = focusable[0]
        const last = focusable[focusable.length - 1]
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault()
          last.focus()
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault()
          first.focus()
        }
      }
    }
    document.addEventListener('keydown', onKeyDown)
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    // Move focus into the dialog on open.
    window.setTimeout(() => {
      panelRef.current
        ?.querySelector<HTMLElement>('input, button, textarea, select')
        ?.focus()
    }, 30)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = previous
    }
  }, [open, onClose])

  if (!open) return null

  const widths = { sm: 'max-w-sm', md: 'max-w-lg', lg: 'max-w-3xl', xl: 'max-w-5xl' }

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-end justify-center sm:items-center">
      <div
        className="absolute inset-0 bg-slate-900/50 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={cn(
          'relative flex max-h-[92vh] w-full flex-col rounded-t-xl bg-surface shadow-pop animate-fade-in sm:rounded-xl',
          widths[size],
        )}
      >
        <header className="flex items-start justify-between gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h2 id={titleId} className="text-sm font-semibold text-strong">
              {title}
            </h2>
            {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
          </div>
          <button
            onClick={onClose}
            className="btn-ghost btn-sm -mr-1 -mt-1 h-8 w-8 p-0"
            aria-label="Close dialog"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-4 py-4">{children}</div>
        {footer && (
          <footer className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
            {footer}
          </footer>
        )}
      </div>
    </div>,
    document.body,
  )
}

/* ------------------------------------------------------------------ Field */

export function Field({
  label,
  hint,
  error,
  required,
  children,
  className,
}: {
  label?: string
  hint?: string
  error?: string
  required?: boolean
  children: ReactNode
  className?: string
}) {
  return (
    <div className={className}>
      {label && (
        <label className="label">
          {label}
          {required && <span className="ml-0.5 text-negative">*</span>}
        </label>
      )}
      {children}
      {error ? (
        <p className="mt-1 text-2xs text-negative">{error}</p>
      ) : hint ? (
        <p className="mt-1 text-2xs text-subtle">{hint}</p>
      ) : null}
    </div>
  )
}

export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return <input ref={ref} className={cn('input', className)} {...props} />
  },
)

export const Select = forwardRef<
  HTMLSelectElement,
  React.SelectHTMLAttributes<HTMLSelectElement>
>(function Select({ className, children, ...props }, ref) {
  return (
    <div className="relative">
      <select ref={ref} className={cn('input appearance-none pr-8', className)} {...props}>
        {children}
      </select>
      <ChevronDown
        className="pointer-events-none absolute right-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-subtle"
        aria-hidden
      />
    </div>
  )
})

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, ...props }, ref) {
  return (
    <textarea
      ref={ref}
      className={cn('input h-auto min-h-[76px] resize-y py-2', className)}
      {...props}
    />
  )
})

/* ------------------------------------------------------------------ Tabs */

export function Tabs({
  tabs,
  active,
  onChange,
  className,
}: {
  tabs: { id: string; label: string; count?: number }[]
  active: string
  onChange: (id: string) => void
  className?: string
}) {
  return (
    <div role="tablist" className={cn('flex gap-1 overflow-x-auto border-b border-border', className)}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          role="tab"
          aria-selected={active === tab.id}
          onClick={() => onChange(tab.id)}
          className={cn(
            'flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2 text-sm font-medium transition-colors',
            active === tab.id
              ? 'border-brand text-brand'
              : 'border-transparent text-muted hover:text-strong',
          )}
        >
          {tab.label}
          {tab.count !== undefined && (
            <span className="rounded bg-raised px-1.5 py-0.5 text-2xs tabular text-muted">
              {tab.count}
            </span>
          )}
        </button>
      ))}
    </div>
  )
}

/* ---------------------------------------------------------------- Toasts */

type Toast = { id: number; title: string; description?: string; tone: Tone }

const ToastContext = createContext<{
  push: (toast: Omit<Toast, 'id'>) => void
} | null>(null)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const push = useCallback((toast: Omit<Toast, 'id'>) => {
    const id = Date.now() + Math.random()
    setToasts((current) => [...current, { ...toast, id }])
    window.setTimeout(() => {
      setToasts((current) => current.filter((item) => item.id !== id))
    }, 6000)
  }, [])

  const value = useMemo(() => ({ push }), [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div
        className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-full max-w-sm flex-col gap-2"
        role="status"
        aria-live="polite"
      >
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className="pointer-events-auto flex items-start gap-2.5 rounded-lg border border-border bg-surface p-3 shadow-pop animate-fade-in"
          >
            <span className={cn('mt-0.5 rounded-md p-1', TONE_CLASS[toast.tone])}>
              {toast.tone === 'negative' ? (
                <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
              ) : toast.tone === 'positive' ? (
                <Check className="h-3.5 w-3.5" aria-hidden />
              ) : (
                <Info className="h-3.5 w-3.5" aria-hidden />
              )}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium text-strong">{toast.title}</p>
              {toast.description && (
                <p className="mt-0.5 break-words text-xs text-muted">{toast.description}</p>
              )}
            </div>
            <button
              onClick={() => setToasts((c) => c.filter((i) => i.id !== toast.id))}
              className="btn-ghost h-6 w-6 shrink-0 p-0"
              aria-label="Dismiss"
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast must be used inside a ToastProvider')
  return context
}

/* --------------------------------------------------------------- Progress */

export function Progress({
  value,
  tone = 'brand',
  className,
  showLabel,
}: {
  value: number
  tone?: Tone
  className?: string
  showLabel?: boolean
}) {
  const clamped = Math.max(0, Math.min(100, value))
  const fills: Record<Tone, string> = {
    neutral: 'bg-subtle',
    brand: 'bg-brand',
    positive: 'bg-positive',
    negative: 'bg-negative',
    warning: 'bg-warning',
    info: 'bg-info',
  }
  return (
    <div className={cn('flex items-center gap-2', className)}>
      <div
        className="h-1.5 flex-1 overflow-hidden rounded-full bg-raised"
        role="progressbar"
        aria-valuenow={Math.round(clamped)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <div
          className={cn('h-full rounded-full transition-all', fills[tone])}
          style={{ width: `${clamped}%` }}
        />
      </div>
      {showLabel && (
        <span className="shrink-0 text-2xs font-semibold tabular text-muted">
          {clamped.toFixed(0)}%
        </span>
      )}
    </div>
  )
}

/* ----------------------------------------------------------- Confidence */

/**
 * Confidence is always shown *labelled as confidence*, never as accuracy.
 * Where measured accuracy exists it is shown next to it, explicitly separate.
 */
export function ConfidenceBadge({
  value,
  label,
  measuredAccuracy,
  sampleSize,
}: {
  value: Money
  label?: string
  measuredAccuracy?: string | null
  sampleSize?: number
}) {
  const percent = Number(String(value ?? 0)) * 100
  const tone: Tone = percent >= 90 ? 'positive' : percent >= 70 ? 'warning' : 'neutral'
  const accuracyText =
    measuredAccuracy != null
      ? `Measured accuracy on ${sampleSize ?? 0} decided items: ${(Number(measuredAccuracy) * 100).toFixed(0)}%`
      : 'Measured accuracy: not enough decided items yet'

  return (
    <span
      className="inline-flex items-center gap-1"
      title={`AI confidence ${percent.toFixed(0)}% — the model's own estimate. ${accuracyText}`}
    >
      <Badge tone={tone}>{label ?? `${percent.toFixed(0)}% confidence`}</Badge>
    </span>
  )
}

export function SeverityDot({ severity }: { severity: string }) {
  const map: Record<string, string> = {
    CRITICAL: 'bg-negative',
    HIGH: 'bg-negative',
    MEDIUM: 'bg-warning',
    LOW: 'bg-info',
    INFO: 'bg-subtle',
  }
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={cn('h-1.5 w-1.5 shrink-0 rounded-full', map[severity] ?? 'bg-subtle')}
        aria-hidden
      />
      <span className="text-2xs font-medium uppercase tracking-wide text-muted">
        {severity}
      </span>
    </span>
  )
}

/* --------------------------------------------------------------- Toolbar */

export function Toolbar({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn('flex flex-wrap items-center gap-2', className)}>{children}</div>
  )
}

export function PageHeader({
  title,
  description,
  actions,
  children,
}: {
  title: string
  description?: string
  actions?: ReactNode
  children?: ReactNode
}) {
  return (
    <div className="mb-4 flex flex-col gap-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold tracking-tight text-strong">{title}</h1>
          {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
        </div>
        {actions && <Toolbar className="shrink-0">{actions}</Toolbar>}
      </div>
      {children}
    </div>
  )
}

/* ------------------------------------------------------------ Pagination */

export function Pagination({
  total,
  limit,
  offset,
  onChange,
}: {
  total: number
  limit: number
  offset: number
  onChange: (offset: number) => void
}) {
  if (total <= limit) return null
  const page = Math.floor(offset / limit) + 1
  const pages = Math.ceil(total / limit)

  return (
    <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-2.5">
      <p className="text-xs text-muted tabular">
        {offset + 1}–{Math.min(offset + limit, total)} of {total.toLocaleString('en-IN')}
      </p>
      <div className="flex items-center gap-1.5">
        <Button
          size="sm"
          onClick={() => onChange(Math.max(0, offset - limit))}
          disabled={offset === 0}
        >
          Previous
        </Button>
        <span className="px-1 text-xs text-muted tabular">
          {page} / {pages}
        </span>
        <Button
          size="sm"
          onClick={() => onChange(offset + limit)}
          disabled={offset + limit >= total}
        >
          Next
        </Button>
      </div>
    </div>
  )
}

export function Alert({
  tone = 'info',
  title,
  children,
  className,
}: {
  tone?: Tone
  title?: string
  children?: ReactNode
  className?: string
}) {
  const Icon = tone === 'negative' || tone === 'warning' ? AlertTriangle : Info
  return (
    <div
      className={cn('flex items-start gap-2.5 rounded-lg p-3', TONE_CLASS[tone], className)}
      role={tone === 'negative' ? 'alert' : undefined}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="min-w-0 text-xs">
        {title && <p className="font-semibold">{title}</p>}
        {children && <div className={cn(title && 'mt-0.5', 'opacity-90')}>{children}</div>}
      </div>
    </div>
  )
}

export { formatMoney, formatPercent }
