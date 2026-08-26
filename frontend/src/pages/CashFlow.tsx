/** Cash forecast — with its assumptions stated, never presented as certainty. */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { Info, TrendingDown, TrendingUp, Wallet } from 'lucide-react'
import { api } from '@/lib/api'
import { formatDate, formatMoney, toNumber, toPaise } from '@/lib/money'
import {
  Alert,
  Badge,
  Card,
  EmptyState,
  ErrorState,
  PageHeader,
  Spinner,
  StatCard,
  Tabs,
  cn,
} from '@/components/ui'

const HORIZONS = [
  { id: '7', label: '7 days' },
  { id: '30', label: '30 days' },
  { id: '60', label: '60 days' },
  { id: '90', label: '90 days' },
]

export default function CashFlow() {
  const [horizon, setHorizon] = useState('30')

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['cash-forecast', horizon],
    queryFn: () => api.get('/reports/cash-forecast', { days: Number(horizon) }),
  })

  if (isError) return <ErrorState error={error} retry={() => refetch()} />

  const chartData = [
    { label: 'Today', cash: toNumber(data?.opening_cash ?? 0), low: null, high: null },
    ...(data?.weeks ?? []).map((week: any) => ({
      label: week.label,
      cash: toNumber(week.closing_cash),
      inflow: toNumber(week.inflow),
      outflow: toNumber(week.outflow),
    })),
  ]

  return (
    <div>
      <PageHeader
        title="Cash flow forecast"
        description="Built from committed receivables, payables and recurring items — not from a trend line."
      />

      <Tabs
        tabs={HORIZONS.map((option) => ({ id: option.id, label: option.label }))}
        active={horizon}
        onChange={setHorizon}
        className="mb-3"
      />

      {isLoading ? (
        <Spinner label="Building the forecast" />
      ) : !data ? (
        <EmptyState title="No forecast available" />
      ) : (
        <>
          {data.warnings?.map((warning: string) => (
            <Alert key={warning} tone="negative" className="mb-3" title="Cash warning">
              {warning}
            </Alert>
          ))}

          <div className="mb-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
            <StatCard
              label="Cash today"
              value={formatMoney(data.opening_cash, { compact: true })}
              sub="Cash and bank balances"
              icon={Wallet}
              tone="brand"
            />
            <StatCard
              label="Expected in"
              value={formatMoney(data.expected_inflow, { compact: true })}
              sub={`${data.basis.open_invoices} open invoices`}
              icon={TrendingUp}
              tone="positive"
            />
            <StatCard
              label="Expected out"
              value={formatMoney(data.expected_outflow, { compact: true })}
              sub={`${data.basis.open_bills} open bills · ${data.basis.recurring_items} recurring`}
              icon={TrendingDown}
              tone="warning"
            />
            <StatCard
              label={`Projected in ${horizon} days`}
              value={formatMoney(data.closing_cash, { compact: true })}
              sub={`Range ${formatMoney(data.confidence_low, { compact: true })} – ${formatMoney(data.confidence_high, { compact: true })}`}
              icon={Wallet}
              tone={toPaise(data.closing_cash) < 0 ? 'negative' : 'positive'}
            />
          </div>

          <Card
            title="Projected cash position"
            description={`${formatDate(data.start_date)} to ${formatDate(data.end_date)}`}
            className="mb-3"
            bodyClassName="p-2 pt-4"
          >
            <ResponsiveContainer width="100%" height={280}>
              <AreaChart data={chartData} margin={{ top: 4, right: 16, left: 4, bottom: 0 }}>
                <defs>
                  <linearGradient id="cashFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#2563eb" stopOpacity={0.3} />
                    <stop offset="100%" stopColor="#2563eb" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border))" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }}
                  axisLine={false}
                  tickLine={false}
                  width={56}
                  tickFormatter={(value) => formatMoney(value, { compact: true, symbol: false })}
                />
                <Tooltip
                  content={({ active, payload, label }: any) => {
                    if (!active || !payload?.length) return null
                    return (
                      <div className="rounded-lg border border-border bg-surface p-2.5 shadow-pop">
                        <p className="mb-1 text-2xs font-semibold text-strong">{label}</p>
                        {payload.map((entry: any) => (
                          <p key={entry.dataKey} className="text-2xs text-muted">
                            {entry.dataKey}:{' '}
                            <span className="font-semibold tabular text-strong">
                              {formatMoney(entry.value)}
                            </span>
                          </p>
                        ))}
                      </div>
                    )
                  }}
                />
                <ReferenceLine y={0} stroke="rgb(var(--negative))" strokeDasharray="4 4" />
                <Area
                  type="monotone"
                  dataKey="cash"
                  name="Projected cash"
                  stroke="#2563eb"
                  strokeWidth={2}
                  fill="url(#cashFill)"
                />
              </AreaChart>
            </ResponsiveContainer>
          </Card>

          <div className="grid gap-3 lg:grid-cols-3">
            <Card
              title="Week by week"
              description="Committed items in each window"
              className="lg:col-span-2"
              bodyClassName="p-0"
            >
              {data.weeks.length === 0 ? (
                <div className="p-4">
                  <EmptyState
                    title="No commitments in this horizon"
                    description="There are no dated receivables or payables in the selected window."
                  />
                </div>
              ) : (
                <div className="divide-y divide-border">
                  {data.weeks.map((week: any) => (
                    <div key={week.label} className="p-3.5">
                      <div className="flex flex-wrap items-baseline justify-between gap-2">
                        <span className="text-sm font-medium text-strong">{week.label}</span>
                        <div className="flex items-center gap-3 text-xs tabular">
                          <span className="text-positive">
                            +{formatMoney(week.inflow, { symbol: false })}
                          </span>
                          <span className="text-negative">
                            −{formatMoney(week.outflow, { symbol: false })}
                          </span>
                          <span
                            className={cn(
                              'font-semibold',
                              toPaise(week.closing_cash) < 0 ? 'text-negative' : 'text-strong',
                            )}
                          >
                            {formatMoney(week.closing_cash)}
                          </span>
                        </div>
                      </div>
                      {week.items.length > 0 && (
                        <ul className="mt-2 space-y-1">
                          {week.items.slice(0, 4).map((item: any, index: number) => (
                            <li
                              key={index}
                              className="flex items-center justify-between gap-2 text-2xs"
                            >
                              <span className="flex min-w-0 items-center gap-1.5">
                                <Badge tone={item.type === 'invoice' ? 'positive' : 'warning'}>
                                  {item.type}
                                </Badge>
                                <span className="truncate text-muted">{item.label}</span>
                              </span>
                              <span className="shrink-0 tabular text-muted">
                                {formatMoney(item.expected)}
                                {Number(item.probability) < 1 && (
                                  <span className="ml-1 text-subtle">
                                    ({Math.round(Number(item.probability) * 100)}%)
                                  </span>
                                )}
                              </span>
                            </li>
                          ))}
                          {week.item_count > 4 && (
                            <li className="text-2xs text-subtle">
                              +{week.item_count - 4} more
                            </li>
                          )}
                        </ul>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </Card>

            <Card title="Assumptions" description="What this forecast takes as given">
              <ul className="space-y-2">
                {data.assumptions.map((assumption: string, index: number) => (
                  <li key={index} className="flex items-start gap-2 text-2xs text-muted">
                    <Info className="mt-0.5 h-3 w-3 shrink-0 text-subtle" aria-hidden />
                    {assumption}
                  </li>
                ))}
              </ul>
              <Alert tone="info" className="mt-3">
                {data.confidence_note}
              </Alert>
              <div className="mt-3 space-y-1.5 border-t border-border pt-3 text-2xs">
                <Row label="Gross receivable" value={data.gross_receivable} />
                <Row label="Gross payable" value={data.gross_payable} />
                <Row
                  label="Uncertainty band"
                  value={`±${Number(data.uncertainty_percent).toFixed(0)}%`}
                  raw
                />
              </div>
            </Card>
          </div>
        </>
      )}
    </div>
  )
}

function Row({ label, value, raw }: { label: string; value: any; raw?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-muted">{label}</span>
      <span className="tabular font-medium text-strong">
        {raw ? value : formatMoney(value)}
      </span>
    </div>
  )
}
