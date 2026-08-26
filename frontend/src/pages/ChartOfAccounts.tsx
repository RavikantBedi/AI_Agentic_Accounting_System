/** Chart of accounts — a hierarchy with rolled-up balances. */
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ChevronRight, Lock, Search } from 'lucide-react'
import { api } from '@/lib/api'
import { formatMoney, toPaise } from '@/lib/money'
import {
  Amount,
  Badge,
  Card,
  EmptyState,
  ErrorState,
  Input,
  PageHeader,
  Select,
  Spinner,
  Toolbar,
  cn,
} from '@/components/ui'

const TYPES = ['', 'ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE']

const TYPE_TONE: Record<string, any> = {
  ASSET: 'brand',
  LIABILITY: 'warning',
  EQUITY: 'info',
  REVENUE: 'positive',
  EXPENSE: 'negative',
}

export default function ChartOfAccounts() {
  const navigate = useNavigate()
  const [type, setType] = useState('')
  const [search, setSearch] = useState('')
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['coa', type],
    queryFn: () => api.get('/accounting/accounts', { account_type: type || undefined }),
  })

  const matches = (node: any): boolean => {
    if (!search.trim()) return true
    const needle = search.toLowerCase()
    if (node.name.toLowerCase().includes(needle) || node.code.includes(needle)) return true
    return (node.children ?? []).some(matches)
  }

  const totals = useMemo(() => {
    const byType: Record<string, number> = {}
    for (const root of data ?? []) {
      byType[root.type] = (byType[root.type] ?? 0) + toPaise(root.balance)
    }
    return byType
  }, [data])

  if (isError) return <ErrorState error={error} retry={() => refetch()} />

  return (
    <div>
      <PageHeader
        title="Chart of accounts"
        description="Group accounts roll up their children. Only leaf accounts can be posted to."
      >
        <Toolbar>
          <div className="relative min-w-0 flex-1 sm:max-w-xs">
            <Search
              className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-subtle"
              aria-hidden
            />
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search by code or name"
              className="pl-8"
              aria-label="Search accounts"
            />
          </div>
          <Select
            value={type}
            onChange={(event) => setType(event.target.value)}
            className="w-40"
            aria-label="Filter by type"
          >
            {TYPES.map((option) => (
              <option key={option} value={option}>
                {option || 'All types'}
              </option>
            ))}
          </Select>
        </Toolbar>
      </PageHeader>

      <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-5">
        {['ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE'].map((accountType) => (
          <div key={accountType} className="card p-2.5">
            <p className="text-2xs uppercase tracking-wider text-muted">{accountType}</p>
            <p className="mt-0.5 text-sm font-semibold tabular text-strong">
              {formatMoney(((totals[accountType] ?? 0) / 100).toFixed(2), { compact: true })}
            </p>
          </div>
        ))}
      </div>

      <Card bodyClassName="p-0">
        {isLoading ? (
          <Spinner label="Loading the chart of accounts" />
        ) : !data?.length ? (
          <EmptyState title="No accounts" description="Seed the default chart of accounts to begin." />
        ) : (
          <div className="scroll-x">
            <table className="w-full min-w-[640px]">
              <thead>
                <tr className="table-head">
                  <th className="px-3 py-2 text-left">Account</th>
                  <th className="hidden px-3 py-2 text-left md:table-cell">Type</th>
                  <th className="hidden px-3 py-2 text-left lg:table-cell">Subtype</th>
                  <th className="px-3 py-2 text-right">Debit</th>
                  <th className="px-3 py-2 text-right">Credit</th>
                  <th className="px-3 py-2 text-right">Balance</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {(data ?? [])
                  .filter(matches)
                  .map((root: any) => (
                    <AccountRows
                      key={root.id}
                      node={root}
                      depth={0}
                      collapsed={collapsed}
                      toggle={(id) =>
                        setCollapsed((current) => ({ ...current, [id]: !current[id] }))
                      }
                      matches={matches}
                      onOpen={(account) => navigate(`/accounting/ledger/${account.id}`)}
                      searching={Boolean(search.trim())}
                    />
                  ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}

function AccountRows({
  node,
  depth,
  collapsed,
  toggle,
  matches,
  onOpen,
  searching,
}: {
  node: any
  depth: number
  collapsed: Record<string, boolean>
  toggle: (id: string) => void
  matches: (node: any) => boolean
  onOpen: (node: any) => void
  searching: boolean
}) {
  const hasChildren = (node.children ?? []).length > 0
  // While searching, keep everything expanded so matches are visible.
  const isOpen = searching || !collapsed[node.id]

  return (
    <>
      <tr
        className={cn(
          'transition-colors hover:bg-raised',
          !node.is_group && 'cursor-pointer',
          depth === 0 && 'bg-raised/50 font-semibold',
        )}
        onClick={() => (node.is_group ? toggle(node.id) : onOpen(node))}
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            node.is_group ? toggle(node.id) : onOpen(node)
          }
        }}
      >
        <td className="px-3 py-2">
          <div className="flex items-center gap-1.5" style={{ paddingLeft: depth * 16 }}>
            {hasChildren ? (
              <ChevronRight
                className={cn('h-3.5 w-3.5 shrink-0 text-subtle transition-transform', isOpen && 'rotate-90')}
                aria-hidden
              />
            ) : (
              <span className="w-3.5 shrink-0" />
            )}
            <span className="font-mono text-2xs text-subtle">{node.code}</span>
            <span className={cn('truncate text-sm', node.is_group ? 'font-medium' : '')}>
              {node.name}
            </span>
            {node.is_system && (
              <Lock className="h-3 w-3 shrink-0 text-subtle" aria-label="System account" />
            )}
          </div>
        </td>
        <td className="hidden px-3 py-2 md:table-cell">
          {depth === 0 && <Badge tone={TYPE_TONE[node.type]}>{node.type}</Badge>}
        </td>
        <td className="hidden px-3 py-2 text-2xs text-muted lg:table-cell">
          {node.subtype?.replace(/_/g, ' ').toLowerCase()}
        </td>
        <td className="px-3 py-2 text-right text-sm tabular text-muted">
          {toPaise(node.debit) ? formatMoney(node.debit) : ''}
        </td>
        <td className="px-3 py-2 text-right text-sm tabular text-muted">
          {toPaise(node.credit) ? formatMoney(node.credit) : ''}
        </td>
        <td className="px-3 py-2 text-right">
          <Amount value={node.balance} className="text-sm font-medium" />
        </td>
      </tr>

      {isOpen &&
        (node.children ?? [])
          .filter(matches)
          .map((child: any) => (
            <AccountRows
              key={child.id}
              node={child}
              depth={depth + 1}
              collapsed={collapsed}
              toggle={toggle}
              matches={matches}
              onOpen={onOpen}
              searching={searching}
            />
          ))}
    </>
  )
}
