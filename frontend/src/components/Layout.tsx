/** Application shell: sidebar navigation, top bar, command palette. */
import { useEffect, useMemo, useState } from 'react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Banknote,
  Bell,
  BookOpen,
  Bot,
  Building2,
  ChevronRight,
  ClipboardCheck,
  FileText,
  LayoutDashboard,
  LogOut,
  Menu,
  Moon,
  Package,
  Percent,
  Receipt,
  Search,
  Settings,
  ShieldAlert,
  Sparkles,
  Sun,
  TrendingUp,
  Users,
  Wallet,
  X,
  type LucideIcon,
} from 'lucide-react'
import { api, logout } from '@/lib/api'
import { formatRelative } from '@/lib/money'
import { Badge, cn } from '@/components/ui'

interface NavItem {
  label: string
  to?: string
  icon?: LucideIcon
  children?: { label: string; to: string }[]
  badgeKey?: 'exceptions' | 'unreconciled'
}

const NAVIGATION: NavItem[] = [
  { label: 'Overview', to: '/', icon: LayoutDashboard },
  { label: 'AI Command Center', to: '/ai', icon: Sparkles },
  {
    label: 'Accounting',
    icon: BookOpen,
    children: [
      { label: 'Journal Entries', to: '/accounting/journals' },
      { label: 'Chart of Accounts', to: '/accounting/accounts' },
      { label: 'Trial Balance', to: '/accounting/trial-balance' },
      { label: 'General Ledger', to: '/accounting/ledger' },
    ],
  },
  {
    label: 'Banking',
    icon: Banknote,
    badgeKey: 'unreconciled',
    children: [
      { label: 'Bank Accounts', to: '/banking' },
      { label: 'Transactions', to: '/banking/transactions' },
      { label: 'Reconciliation', to: '/banking/reconciliation' },
    ],
  },
  {
    label: 'Sales',
    icon: Receipt,
    children: [
      { label: 'Invoices', to: '/sales/invoices' },
      { label: 'Customers', to: '/sales/customers' },
      { label: 'Receipts', to: '/sales/receipts' },
    ],
  },
  {
    label: 'Purchases',
    icon: FileText,
    children: [
      { label: 'Bills', to: '/purchases/bills' },
      { label: 'Vendors', to: '/purchases/vendors' },
      { label: 'Payments', to: '/purchases/payments' },
    ],
  },
  { label: 'Inventory', to: '/inventory', icon: Package },
  {
    label: 'Tax',
    icon: Percent,
    children: [
      { label: 'GST', to: '/tax/gst' },
      { label: 'TDS', to: '/tax/tds' },
      { label: 'e-Invoice & e-Way Bill', to: '/tax/edocs' },
    ],
  },
  { label: 'Reports', to: '/reports', icon: TrendingUp },
  { label: 'Cash Flow', to: '/cash-flow', icon: Wallet },
  { label: 'Month-End Close', to: '/close', icon: ClipboardCheck },
  { label: 'AI Agents', to: '/agents', icon: Bot },
  { label: 'Exceptions', to: '/exceptions', icon: ShieldAlert, badgeKey: 'exceptions' },
  { label: 'Audit Log', to: '/audit', icon: Users },
  { label: 'Settings', to: '/settings', icon: Settings },
]

function useTheme() {
  const [theme, setTheme] = useState<'light' | 'dark'>(
    () => (localStorage.getItem('ailedger.theme') as 'light' | 'dark') || 'light',
  )
  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    localStorage.setItem('ailedger.theme', theme)
  }, [theme])
  return { theme, toggle: () => setTheme((t) => (t === 'light' ? 'dark' : 'light')) }
}

export default function Layout({ children }: { children: React.ReactNode }) {
  const location = useLocation()
  const navigate = useNavigate()
  const { theme, toggle } = useTheme()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [paletteOpen, setPaletteOpen] = useState(false)
  const [notificationsOpen, setNotificationsOpen] = useState(false)

  const { data: me } = useQuery({ queryKey: ['me'], queryFn: () => api.get('/auth/me') })
  const { data: attention } = useQuery({
    queryKey: ['exception-summary'],
    queryFn: () => api.get('/exceptions/summary'),
    refetchInterval: 60_000,
  })
  const { data: recon } = useQuery({
    queryKey: ['bank-summary'],
    queryFn: () => api.get('/banking/summary'),
    refetchInterval: 60_000,
  })
  const { data: notifications } = useQuery({
    queryKey: ['notifications'],
    queryFn: () => api.get('/notifications', { limit: 10 }),
    refetchInterval: 60_000,
  })

  const badges = {
    exceptions: attention?.total_open ?? 0,
    unreconciled: recon?.needs_attention_count ?? 0,
  }

  // Close the mobile drawer whenever the route changes.
  useEffect(() => setSidebarOpen(false), [location.pathname])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        setPaletteOpen((open) => !open)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  const handleSignOut = async () => {
    await logout()
    navigate('/login')
  }

  return (
    <div className="flex h-full">
      {/* Mobile drawer backdrop */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-slate-900/50 lg:hidden"
          onClick={() => setSidebarOpen(false)}
          aria-hidden
        />
      )}

      <Sidebar
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        company={me?.company}
        badges={badges}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-2 border-b border-border bg-surface/90 px-3 backdrop-blur lg:px-5">
          <button
            onClick={() => setSidebarOpen(true)}
            className="btn-ghost btn-sm h-9 w-9 p-0 lg:hidden"
            aria-label="Open navigation"
          >
            <Menu className="h-5 w-5" aria-hidden />
          </button>

          <button
            onClick={() => setPaletteOpen(true)}
            className="flex h-9 min-w-0 flex-1 items-center gap-2 rounded-lg border border-border bg-raised px-3 text-left text-sm text-subtle transition-colors hover:border-brand/40 sm:max-w-md"
          >
            <Search className="h-4 w-4 shrink-0" aria-hidden />
            <span className="truncate">Search invoices, vendors, accounts…</span>
            <kbd className="ml-auto hidden shrink-0 rounded border border-border bg-surface px-1.5 py-0.5 text-2xs text-muted sm:block">
              ⌘K
            </kbd>
          </button>

          <div className="ml-auto flex items-center gap-1">
            {me?.company?.is_demo && (
              <Badge tone="warning" className="mr-1 hidden sm:inline-flex">
                Demo data
              </Badge>
            )}

            <div className="relative">
              <button
                onClick={() => setNotificationsOpen((open) => !open)}
                className="btn-ghost btn-sm relative h-9 w-9 p-0"
                aria-label={`Notifications${notifications?.unread_count ? `, ${notifications.unread_count} unread` : ''}`}
              >
                <Bell className="h-4 w-4" aria-hidden />
                {notifications?.unread_count > 0 && (
                  <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-negative" />
                )}
              </button>
              {notificationsOpen && (
                <NotificationPanel
                  notifications={notifications?.notifications ?? []}
                  onClose={() => setNotificationsOpen(false)}
                />
              )}
            </div>

            <button
              onClick={toggle}
              className="btn-ghost btn-sm h-9 w-9 p-0"
              aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} theme`}
            >
              {theme === 'light' ? (
                <Moon className="h-4 w-4" aria-hidden />
              ) : (
                <Sun className="h-4 w-4" aria-hidden />
              )}
            </button>

            <div className="ml-1 flex items-center gap-2 border-l border-border pl-2">
              <div className="hidden text-right sm:block">
                <p className="text-xs font-medium leading-tight text-strong">
                  {me?.user?.full_name ?? '—'}
                </p>
                <p className="text-2xs leading-tight text-muted">{me?.role ?? ''}</p>
              </div>
              <button
                onClick={handleSignOut}
                className="btn-ghost btn-sm h-9 w-9 p-0"
                aria-label="Sign out"
                title="Sign out"
              >
                <LogOut className="h-4 w-4" aria-hidden />
              </button>
            </div>
          </div>
        </header>

        <main className="min-w-0 flex-1 overflow-y-auto p-3 lg:p-5">{children}</main>
      </div>

      {paletteOpen && <CommandPalette onClose={() => setPaletteOpen(false)} />}
    </div>
  )
}

function Sidebar({
  open,
  onClose,
  company,
  badges,
}: {
  open: boolean
  onClose: () => void
  company?: any
  badges: Record<string, number>
}) {
  const location = useLocation()
  const [expanded, setExpanded] = useState<string[]>(() => {
    const active = NAVIGATION.find((item) =>
      item.children?.some((child) => location.pathname.startsWith(child.to)),
    )
    return active ? [active.label] : ['Accounting']
  })

  const toggle = (label: string) =>
    setExpanded((current) =>
      current.includes(label)
        ? current.filter((item) => item !== label)
        : [...current, label],
    )

  return (
    <aside
      className={cn(
        'fixed inset-y-0 left-0 z-50 flex w-64 shrink-0 flex-col border-r border-border bg-surface transition-transform lg:static lg:translate-x-0',
        open ? 'translate-x-0' : '-translate-x-full',
      )}
      aria-label="Main navigation"
    >
      <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-border px-4">
        <span className="grid h-8 w-8 place-items-center rounded-lg bg-brand text-brand-fg">
          <Sparkles className="h-4 w-4" aria-hidden />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-sm font-semibold leading-tight text-strong">AI Ledger</p>
          <p className="truncate text-2xs leading-tight text-muted">
            {company?.name ?? 'Your books, managed by AI'}
          </p>
        </div>
        <button
          onClick={onClose}
          className="btn-ghost btn-sm h-8 w-8 p-0 lg:hidden"
          aria-label="Close navigation"
        >
          <X className="h-4 w-4" aria-hidden />
        </button>
      </div>

      <nav className="flex-1 space-y-0.5 overflow-y-auto p-2">
        {NAVIGATION.map((item) => {
          const count = item.badgeKey ? badges[item.badgeKey] : 0

          if (item.children) {
            const isOpen = expanded.includes(item.label)
            const hasActiveChild = item.children.some((child) =>
              location.pathname.startsWith(child.to),
            )
            return (
              <div key={item.label}>
                <button
                  onClick={() => toggle(item.label)}
                  aria-expanded={isOpen}
                  className={cn(
                    'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium transition-colors',
                    hasActiveChild ? 'text-strong' : 'text-muted hover:bg-raised hover:text-strong',
                  )}
                >
                  {item.icon && <item.icon className="h-4 w-4 shrink-0" aria-hidden />}
                  <span className="flex-1 text-left">{item.label}</span>
                  {count > 0 && (
                    <span className="rounded bg-negative-soft px-1.5 py-0.5 text-2xs font-semibold tabular text-negative">
                      {count}
                    </span>
                  )}
                  <ChevronRight
                    className={cn('h-3.5 w-3.5 shrink-0 transition-transform', isOpen && 'rotate-90')}
                    aria-hidden
                  />
                </button>
                {isOpen && (
                  <div className="ml-4 mt-0.5 space-y-0.5 border-l border-border pl-2">
                    {item.children.map((child) => (
                      <NavLink
                        key={child.to}
                        to={child.to}
                        className={({ isActive }) =>
                          cn(
                            'block rounded-lg px-2.5 py-1.5 text-sm transition-colors',
                            isActive
                              ? 'bg-brand-soft font-medium text-brand'
                              : 'text-muted hover:bg-raised hover:text-strong',
                          )
                        }
                      >
                        {child.label}
                      </NavLink>
                    ))}
                  </div>
                )}
              </div>
            )
          }

          return (
            <NavLink
              key={item.to}
              to={item.to!}
              end={item.to === '/'}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm font-medium transition-colors',
                  isActive
                    ? 'bg-brand-soft text-brand'
                    : 'text-muted hover:bg-raised hover:text-strong',
                )
              }
            >
              {item.icon && <item.icon className="h-4 w-4 shrink-0" aria-hidden />}
              <span className="flex-1">{item.label}</span>
              {count > 0 && (
                <span className="rounded bg-negative-soft px-1.5 py-0.5 text-2xs font-semibold tabular text-negative">
                  {count}
                </span>
              )}
            </NavLink>
          )
        })}
      </nav>

      <div className="shrink-0 border-t border-border p-3">
        <Link
          to="/settings"
          className="flex items-center gap-2 rounded-lg p-1.5 text-xs text-muted transition-colors hover:bg-raised"
        >
          <Building2 className="h-4 w-4 shrink-0" aria-hidden />
          <span className="truncate">{company?.legal_name ?? 'Company settings'}</span>
        </Link>
      </div>
    </aside>
  )
}

function NotificationPanel({
  notifications,
  onClose,
}: {
  notifications: any[]
  onClose: () => void
}) {
  return (
    <>
      <div className="fixed inset-0 z-40" onClick={onClose} aria-hidden />
      <div className="absolute right-0 top-11 z-50 w-80 rounded-xl border border-border bg-surface shadow-pop animate-fade-in">
        <div className="flex items-center justify-between border-b border-border px-3 py-2">
          <p className="text-sm font-semibold">Notifications</p>
          {notifications.length > 0 && (
            <button
              onClick={async () => {
                await api.post('/notifications/read-all')
                onClose()
              }}
              className="text-2xs text-brand hover:underline"
            >
              Mark all read
            </button>
          )}
        </div>
        <div className="max-h-80 overflow-y-auto">
          {notifications.length === 0 ? (
            <p className="px-3 py-6 text-center text-xs text-muted">
              Nothing needs your attention.
            </p>
          ) : (
            notifications.map((item) => (
              <Link
                key={item.id}
                to={item.action_url || '/exceptions'}
                onClick={onClose}
                className="block border-b border-border px-3 py-2.5 last:border-0 hover:bg-raised"
              >
                <div className="flex items-start justify-between gap-2">
                  <p className="text-xs font-medium text-strong">{item.title}</p>
                  {!item.is_read && (
                    <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-brand" />
                  )}
                </div>
                {item.message && (
                  <p className="mt-0.5 line-clamp-2 text-2xs text-muted">{item.message}</p>
                )}
                <p className="mt-1 text-2xs text-subtle">{formatRelative(item.created_at)}</p>
              </Link>
            ))
          )}
        </div>
      </div>
    </>
  )
}

function CommandPalette({ onClose }: { onClose: () => void }) {
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(0)
  const navigate = useNavigate()

  const { data, isFetching } = useQuery({
    queryKey: ['search', query],
    queryFn: () => api.get('/search', { q: query, limit: 6 }),
    enabled: query.trim().length >= 2,
  })

  const pages = useMemo(
    () =>
      NAVIGATION.flatMap((item) =>
        item.children
          ? item.children.map((child) => ({
              type: 'page',
              id: child.to,
              label: child.label,
              subtitle: item.label,
              url: child.to,
            }))
          : [{ type: 'page', id: item.to!, label: item.label, subtitle: 'Navigate', url: item.to! }],
      ).filter((page) =>
        query.trim() ? page.label.toLowerCase().includes(query.toLowerCase()) : true,
      ),
    [query],
  )

  const results = useMemo(
    () => [...pages.slice(0, 5), ...(data?.results ?? [])],
    [pages, data],
  )

  useEffect(() => setSelected(0), [query])

  const go = (item: any) => {
    navigate(item.url)
    onClose()
  }

  return (
    <div className="fixed inset-0 z-[60] flex items-start justify-center pt-[10vh]">
      <div className="absolute inset-0 bg-slate-900/50 backdrop-blur-[2px]" onClick={onClose} aria-hidden />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Search"
        className="relative w-full max-w-xl overflow-hidden rounded-xl border border-border bg-surface shadow-pop animate-fade-in"
      >
        <div className="flex items-center gap-2.5 border-b border-border px-3.5">
          <Search className="h-4 w-4 shrink-0 text-subtle" aria-hidden />
          <input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') onClose()
              if (event.key === 'ArrowDown') {
                event.preventDefault()
                setSelected((index) => Math.min(index + 1, results.length - 1))
              }
              if (event.key === 'ArrowUp') {
                event.preventDefault()
                setSelected((index) => Math.max(index - 1, 0))
              }
              if (event.key === 'Enter' && results[selected]) go(results[selected])
            }}
            placeholder="Search invoices, bills, customers, accounts, or jump to a page…"
            className="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-subtle"
          />
          <kbd className="shrink-0 rounded border border-border px-1.5 py-0.5 text-2xs text-muted">
            Esc
          </kbd>
        </div>

        <div className="max-h-80 overflow-y-auto p-1.5">
          {isFetching && query.length >= 2 && (
            <p className="px-3 py-4 text-center text-xs text-muted">Searching…</p>
          )}
          {!isFetching && results.length === 0 && (
            <p className="px-3 py-6 text-center text-xs text-muted">
              {query.length < 2 ? 'Type at least two characters.' : 'No matches found.'}
            </p>
          )}
          {results.map((item: any, index: number) => (
            <button
              key={`${item.type}-${item.id}`}
              onClick={() => go(item)}
              onMouseEnter={() => setSelected(index)}
              className={cn(
                'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors',
                index === selected ? 'bg-brand-soft' : 'hover:bg-raised',
              )}
            >
              <span className="rounded bg-raised px-1.5 py-0.5 text-2xs font-medium uppercase text-muted">
                {item.type}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm text-strong">{item.label}</span>
                {item.subtitle && (
                  <span className="block truncate text-2xs text-muted">{item.subtitle}</span>
                )}
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  )
}
