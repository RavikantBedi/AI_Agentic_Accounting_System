/** Shared data hooks and small utilities used across pages. */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { fiscalYearStartISO, monthStartISO, todayISO } from '@/lib/money'

/** Paginated list state plus a debounced search box. */
export function useListQuery<T = any>(
  key: string,
  path: string,
  params: Record<string, any> = {},
  options: { limit?: number; enabled?: boolean } = {},
) {
  const limit = options.limit ?? 50
  const [offset, setOffset] = useState(0)
  const [search, setSearch] = useState('')
  const debouncedSearch = useDebounced(search, 300)

  // Any filter change resets to the first page.
  const filterSignature = JSON.stringify(params)
  useEffect(() => setOffset(0), [filterSignature, debouncedSearch])

  const query = useQuery({
    queryKey: [key, params, debouncedSearch, offset, limit],
    queryFn: () =>
      api.get(path, {
        ...params,
        search: debouncedSearch || undefined,
        limit,
        offset,
      }),
    enabled: options.enabled ?? true,
    placeholderData: (previous) => previous,
  })

  return {
    ...query,
    items: (query.data?.items ?? []) as T[],
    total: query.data?.total ?? 0,
    limit,
    offset,
    setOffset,
    search,
    setSearch,
  }
}

export function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, delay])
  return debounced
}

/** Date range with sensible presets, shared by every report screen. */
export type RangePreset = 'this-month' | 'last-month' | 'quarter' | 'fy' | 'custom'

export function useDateRange(initial: RangePreset = 'fy') {
  const [preset, setPreset] = useState<RangePreset>(initial)
  const [custom, setCustom] = useState({ start: monthStartISO(), end: todayISO() })

  const range = useMemo(() => {
    const today = new Date()
    switch (preset) {
      case 'this-month':
        return { start_date: monthStartISO(), end_date: todayISO() }
      case 'last-month': {
        const end = new Date(today.getFullYear(), today.getMonth(), 0)
        const start = new Date(end.getFullYear(), end.getMonth(), 1)
        return {
          start_date: start.toISOString().slice(0, 10),
          end_date: end.toISOString().slice(0, 10),
        }
      }
      case 'quarter': {
        const start = new Date(today)
        start.setMonth(start.getMonth() - 3)
        return { start_date: start.toISOString().slice(0, 10), end_date: todayISO() }
      }
      case 'fy':
        return { start_date: fiscalYearStartISO(), end_date: todayISO() }
      case 'custom':
        return { start_date: custom.start, end_date: custom.end }
    }
  }, [preset, custom])

  return { preset, setPreset, custom, setCustom, range }
}

export const RANGE_OPTIONS: { id: RangePreset; label: string }[] = [
  { id: 'this-month', label: 'This month' },
  { id: 'last-month', label: 'Last month' },
  { id: 'quarter', label: 'Last 3 months' },
  { id: 'fy', label: 'Financial year' },
  { id: 'custom', label: 'Custom' },
]

export function useCompany() {
  return useQuery({ queryKey: ['me'], queryFn: () => api.get('/auth/me'), staleTime: 300_000 })
}

export function usePermission(permission: string) {
  const { data } = useCompany()
  return Boolean(data?.permissions?.includes(permission))
}

/** Reference data that rarely changes — cached aggressively. */
export function useAccounts(enabled = true) {
  return useQuery({
    queryKey: ['accounts-flat'],
    queryFn: async () => {
      const tree = await api.get('/accounting/accounts', { include_balances: false })
      const flat: any[] = []
      const walk = (nodes: any[], depth = 0) => {
        for (const node of nodes) {
          flat.push({ ...node, depth })
          if (node.children?.length) walk(node.children, depth + 1)
        }
      }
      walk(tree)
      return flat
    },
    staleTime: 300_000,
    enabled,
  })
}

export function usePostableAccounts() {
  const query = useAccounts()
  return { ...query, data: (query.data ?? []).filter((account: any) => !account.is_group) }
}

export function useCustomers() {
  return useQuery({
    queryKey: ['customers-all'],
    queryFn: () => api.get('/sales/customers', { limit: 200 }),
    staleTime: 120_000,
  })
}

export function useVendors() {
  return useQuery({
    queryKey: ['vendors-all'],
    queryFn: () => api.get('/purchases/vendors', { limit: 200 }),
    staleTime: 120_000,
  })
}

export function useItems() {
  return useQuery({
    queryKey: ['items-all'],
    queryFn: () => api.get('/inventory/items', { limit: 200 }),
    staleTime: 120_000,
  })
}

export function useBankAccounts() {
  return useQuery({
    queryKey: ['bank-accounts'],
    queryFn: () => api.get('/banking/accounts'),
    staleTime: 60_000,
  })
}

export function useWarehouses() {
  return useQuery({
    queryKey: ['warehouses'],
    queryFn: () => api.get('/inventory/warehouses'),
    staleTime: 300_000,
  })
}

/** Invalidate everything money-related after a posting. */
export function useRefreshBooks() {
  const queryClient = useQueryClient()
  return useCallback(() => {
    for (const key of [
      'dashboard',
      'briefing',
      'trial-balance',
      'invoices',
      'bills',
      'receipts',
      'payments',
      'journals',
      'accounts-flat',
      'bank-summary',
      'exception-summary',
      'gst-summary',
      'reports',
      'close-status',
    ]) {
      queryClient.invalidateQueries({ queryKey: [key] })
    }
  }, [queryClient])
}

export function currentPeriod(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}

export function periodOptions(count = 12): { value: string; label: string }[] {
  const options: { value: string; label: string }[] = []
  const now = new Date()
  for (let index = 0; index < count; index += 1) {
    const date = new Date(now.getFullYear(), now.getMonth() - index, 1)
    options.push({
      value: `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`,
      label: date.toLocaleDateString('en-IN', { month: 'long', year: 'numeric' }),
    })
  }
  return options
}
