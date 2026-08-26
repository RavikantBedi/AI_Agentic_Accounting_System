/** Financial statements with AI explanation and drill-down. */
import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Download, Sparkles } from 'lucide-react'
import { api } from '@/lib/api'
import { formatMoney, formatPercent, percentChange, toPaise } from '@/lib/money'
import { RANGE_OPTIONS, useDateRange } from '@/lib/hooks'
import {
  Alert,
  Badge,
  Button,
  Card,
  Delta,
  EmptyState,
  ErrorState,
  Field,
  Input,
  PageHeader,
  Select,
  Spinner,
  Tabs,
  Toolbar,
  cn,
} from '@/components/ui'

const TABS = [
  { id: 'pl', label: 'Profit & Loss' },
  { id: 'bs', label: 'Balance Sheet' },
  { id: 'cf', label: 'Cash Flow' },
  { id: 'ratios', label: 'Ratios' },
  { id: 'ar', label: 'Receivables' },
  { id: 'ap', label: 'Payables' },
]

export default function Reports() {
  const [tab, setTab] = useState('pl')
  const { preset, setPreset, custom, setCustom, range } = useDateRange('fy')
  const [explanation, setExplanation] = useState<any>(null)

  const explain = useMutation({
    mutationFn: () =>
      api.post('/ai/agents/REPORTING/run', {
        params: { start_date: range.start_date, end_date: range.end_date },
      }),
    onSuccess: setExplanation,
  })

  return (
    <div>
      <PageHeader
        title="Reports"
        description="Every figure is aggregated from posted journal lines. There is no separate reporting store to drift."
        actions={
          <>
            <Button
              size="sm"
              icon={Sparkles}
              loading={explain.isPending}
              onClick={() => explain.mutate()}
            >
              Explain with AI
            </Button>
            <Button
              size="sm"
              icon={Download}
              onClick={() =>
                api.download(
                  `/reports/export/${
                    { pl: 'profit-and-loss', bs: 'balance-sheet', cf: 'cash-flow', ar: 'ar-aging', ap: 'ap-aging' }[
                      tab
                    ] ?? 'trial-balance'
                  }.csv`,
                  `${tab}-${range.end_date}.csv`,
                  range,
                )
              }
            >
              Export CSV
            </Button>
          </>
        }
      >
        <Toolbar>
          <Field label="Period" className="w-44">
            <Select value={preset} onChange={(event) => setPreset(event.target.value as any)}>
              {RANGE_OPTIONS.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </Select>
          </Field>
          {preset === 'custom' && (
            <>
              <Field label="From" className="w-40">
                <Input
                  type="date"
                  value={custom.start}
                  onChange={(event) =>
                    setCustom({ ...custom, start: event.target.value })
                  }
                />
              </Field>
              <Field label="To" className="w-40">
                <Input
                  type="date"
                  value={custom.end}
                  onChange={(event) => setCustom({ ...custom, end: event.target.value })}
                />
              </Field>
            </>
          )}
        </Toolbar>
      </PageHeader>

      {explanation && (
        <Card
          className="mb-3 border-brand/20"
          title="AI explanation"
          actions={
            <button
              onClick={() => setExplanation(null)}
              className="text-2xs text-muted hover:text-strong"
            >
              Dismiss
            </button>
          }
        >
          <p className="text-sm text-strong">{explanation.summary}</p>
          {explanation.findings?.length > 0 && (
            <ul className="mt-2.5 space-y-1.5">
              {explanation.findings.map((finding: any, index: number) => (
                <li key={index} className="flex items-start gap-2 text-xs">
                  <span
                    className={cn(
                      'mt-1 h-1.5 w-1.5 shrink-0 rounded-full',
                      finding.direction === 'increase' ? 'bg-negative' : 'bg-positive',
                    )}
                    aria-hidden
                  />
                  <span className="text-muted">{finding.detail}</span>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-2.5 text-2xs text-subtle">
            Every driver above is an account-level movement computed from the ledger, not a
            guess. {explanation.used_llm ? `Phrased via ${explanation.provider}.` : 'Computed deterministically.'}
          </p>
        </Card>
      )}

      <Tabs tabs={TABS} active={tab} onChange={setTab} className="mb-3" />

      {tab === 'pl' && <ProfitAndLoss range={range} />}
      {tab === 'bs' && <BalanceSheet asOf={range.end_date} />}
      {tab === 'cf' && <CashFlowStatement range={range} />}
      {tab === 'ratios' && <Ratios range={range} />}
      {tab === 'ar' && <Aging kind="ar" />}
      {tab === 'ap' && <Aging kind="ap" />}
    </div>
  )
}

function ProfitAndLoss({ range }: { range: any }) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['reports', 'pl', range],
    queryFn: () => api.get('/reports/profit-and-loss', { ...range, compare: true }),
  })

  if (isError) return <ErrorState error={error} retry={() => refetch()} />
  if (isLoading) return <Spinner label="Computing profit and loss" />

  const comparative = data.comparative

  return (
    <Card bodyClassName="p-0">
      <div className="scroll-x">
        <table className="w-full min-w-[520px]">
          <thead>
            <tr className="table-head">
              <th className="px-4 py-2 text-left">Account</th>
              <th className="px-4 py-2 text-right">Amount</th>
              {comparative && <th className="hidden px-4 py-2 text-right md:table-cell">Prior period</th>}
              {comparative && <th className="hidden px-4 py-2 text-right md:table-cell">Change</th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            <Section title="Revenue" block={data.revenue} comparative={comparative?.revenue} />
            <Section title="Cost of goods sold" block={data.cogs} comparative={comparative?.cogs} />
            <Total
              label="Gross profit"
              value={data.gross_profit}
              prior={comparative?.gross_profit}
              note={formatPercent(data.gross_margin_percent) + ' margin'}
            />
            <Section
              title="Operating expenses"
              block={data.operating_expenses}
              comparative={comparative?.operating_expenses}
            />
            <Total
              label="Operating profit"
              value={data.operating_profit}
              prior={comparative?.operating_profit}
              note={formatPercent(data.operating_margin_percent) + ' margin'}
            />
            {toPaise(data.other_income.total) !== 0 && (
              <Section title="Other income" block={data.other_income} comparative={comparative?.other_income} />
            )}
            {toPaise(data.other_expenses.total) !== 0 && (
              <Section
                title="Other expenses"
                block={data.other_expenses}
                comparative={comparative?.other_expenses}
              />
            )}
            <Total
              label="Net profit"
              value={data.net_profit}
              prior={comparative?.net_profit}
              note={formatPercent(data.net_margin_percent) + ' margin'}
              emphasis
            />
          </tbody>
        </table>
      </div>
    </Card>
  )
}

function Section({ title, block, comparative }: any) {
  return (
    <>
      <tr className="bg-raised/60">
        <td className="px-4 py-2 text-xs font-semibold uppercase tracking-wider text-muted">
          {title}
        </td>
        <td className="px-4 py-2 text-right text-sm font-semibold tabular text-strong">
          {formatMoney(block.total)}
        </td>
        {comparative && (
          <td className="hidden px-4 py-2 text-right text-sm tabular text-muted md:table-cell">
            {formatMoney(comparative.total)}
          </td>
        )}
        {comparative && (
          <td className="hidden px-4 py-2 text-right md:table-cell">
            <Delta percent={percentChange(block.total, comparative.total)} />
          </td>
        )}
      </tr>
      {block.lines.map((line: any) => {
        const prior = comparative?.lines?.find((row: any) => row.account_id === line.account_id)
        return (
          <tr key={line.account_id} className="hover:bg-raised">
            <td className="py-1.5 pl-8 pr-4 text-sm text-muted">
              <span className="font-mono text-2xs text-subtle">{line.code}</span> {line.name}
            </td>
            <td className="px-4 py-1.5 text-right text-sm tabular">{formatMoney(line.amount)}</td>
            {comparative && (
              <td className="hidden px-4 py-1.5 text-right text-sm tabular text-muted md:table-cell">
                {prior ? formatMoney(prior.amount) : '—'}
              </td>
            )}
            {comparative && (
              <td className="hidden px-4 py-1.5 text-right md:table-cell">
                {prior ? <Delta percent={percentChange(line.amount, prior.amount)} /> : null}
              </td>
            )}
          </tr>
        )
      })}
    </>
  )
}

function Total({ label, value, prior, note, emphasis }: any) {
  return (
    <tr className={cn('border-y-2 border-border', emphasis ? 'bg-brand-soft/40' : 'bg-raised')}>
      <td className="px-4 py-2.5">
        <span className={cn('font-semibold', emphasis ? 'text-base text-strong' : 'text-sm')}>
          {label}
        </span>
        {note && <span className="ml-2 text-2xs text-muted">{note}</span>}
      </td>
      <td
        className={cn(
          'px-4 py-2.5 text-right tabular font-bold',
          emphasis ? 'text-base' : 'text-sm',
          toPaise(value) < 0 ? 'text-negative' : 'text-strong',
        )}
      >
        {formatMoney(value)}
      </td>
      {prior !== undefined && (
        <td className="hidden px-4 py-2.5 text-right text-sm tabular text-muted md:table-cell">
          {formatMoney(prior)}
        </td>
      )}
      {prior !== undefined && (
        <td className="hidden px-4 py-2.5 text-right md:table-cell">
          <Delta percent={percentChange(value, prior)} />
        </td>
      )}
    </tr>
  )
}

function BalanceSheet({ asOf }: { asOf: string }) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['reports', 'bs', asOf],
    queryFn: () => api.get('/reports/balance-sheet', { as_of: asOf }),
  })

  if (isError) return <ErrorState error={error} retry={() => refetch()} />
  if (isLoading) return <Spinner label="Computing the balance sheet" />

  return (
    <div className="space-y-3">
      {data.balanced ? (
        <Alert tone="positive" title="The balance sheet balances">
          Assets of {formatMoney(data.assets.total)} equal liabilities plus equity exactly.
        </Alert>
      ) : (
        <Alert tone="negative" title="The balance sheet does not balance">
          Difference of {formatMoney(data.difference)}. This indicates a serious problem —
          investigate before relying on any report.
        </Alert>
      )}

      <div className="grid gap-3 lg:grid-cols-2">
        <Card title="Assets" bodyClassName="p-0">
          <BsSection title="Current assets" group={data.assets.current} />
          <BsSection title="Fixed assets" group={data.assets.fixed} />
          <BsTotal label="Total assets" value={data.assets.total} />
        </Card>

        <div className="space-y-3">
          <Card title="Liabilities" bodyClassName="p-0">
            <BsSection title="Current liabilities" group={data.liabilities.current} />
            <BsSection title="Long-term liabilities" group={data.liabilities.long_term} />
            <BsTotal label="Total liabilities" value={data.liabilities.total} />
          </Card>
          <Card title="Equity" bodyClassName="p-0">
            <BsSection title="" group={data.equity} />
            <BsTotal label="Total equity" value={data.equity.total} />
          </Card>
          <Card bodyClassName="p-3">
            <div className="flex items-baseline justify-between">
              <span className="text-sm font-semibold text-strong">
                Total liabilities and equity
              </span>
              <span className="text-base font-bold tabular text-strong">
                {formatMoney(data.total_liabilities_and_equity)}
              </span>
            </div>
          </Card>
        </div>
      </div>
    </div>
  )
}

function BsSection({ title, group }: { title: string; group: any }) {
  if (!group.lines?.length) return null
  return (
    <div className="border-b border-border last:border-0">
      {title && (
        <p className="bg-raised/60 px-4 py-1.5 text-2xs font-semibold uppercase tracking-wider text-muted">
          {title}
        </p>
      )}
      {group.lines.map((line: any) => (
        <div
          key={line.account_id ?? line.name}
          className="flex items-baseline justify-between gap-3 px-4 py-1.5 hover:bg-raised"
        >
          <span className="min-w-0 truncate text-sm text-muted">
            {line.code !== '—' && (
              <span className="font-mono text-2xs text-subtle">{line.code}</span>
            )}{' '}
            {line.name}
            {line.is_computed && (
              <Badge tone="info" className="ml-1.5">
                computed
              </Badge>
            )}
          </span>
          <span className="shrink-0 text-sm tabular">{formatMoney(line.amount)}</span>
        </div>
      ))}
      {title && (
        <div className="flex items-baseline justify-between gap-3 border-t border-border px-4 py-1.5">
          <span className="text-xs font-medium text-muted">{title} total</span>
          <span className="text-sm font-semibold tabular text-strong">
            {formatMoney(group.total)}
          </span>
        </div>
      )}
    </div>
  )
}

function BsTotal({ label, value }: { label: string; value: any }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-t-2 border-border bg-raised px-4 py-2.5">
      <span className="text-sm font-semibold text-strong">{label}</span>
      <span className="text-base font-bold tabular text-strong">{formatMoney(value)}</span>
    </div>
  )
}

function CashFlowStatement({ range }: { range: any }) {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['reports', 'cf', range],
    queryFn: () => api.get('/reports/cash-flow', range),
  })

  if (isError) return <ErrorState error={error} retry={() => refetch()} />
  if (isLoading) return <Spinner label="Computing cash flow" />

  return (
    <div className="space-y-3">
      {!data.reconciles && (
        <Alert tone="warning" title="Some cash movement is unclassified">
          {formatMoney(data.unclassified)} of movement could not be assigned to a category.
        </Alert>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <MiniStat label="Opening cash" value={data.opening_cash} />
        <MiniStat label="Net change" value={data.net_change} signed />
        <MiniStat label="Closing cash" value={data.closing_cash} strong />
        <MiniStat label="Operating" value={data.operating.net} signed />
      </div>

      <Card bodyClassName="p-0">
        {['operating', 'investing', 'financing'].map((category) => (
          <div key={category} className="border-b border-border last:border-0">
            <div className="flex items-baseline justify-between bg-raised/60 px-4 py-2">
              <span className="text-xs font-semibold uppercase tracking-wider text-muted">
                {category} activities
              </span>
              <span
                className={cn(
                  'text-sm font-semibold tabular',
                  toPaise(data[category].net) < 0 ? 'text-negative' : 'text-positive',
                )}
              >
                {formatMoney(data[category].net)}
              </span>
            </div>
            {data[category].lines.length === 0 ? (
              <p className="px-4 py-2 text-xs text-subtle">No activity in this category.</p>
            ) : (
              data[category].lines.map((line: any) => (
                <div
                  key={line.label}
                  className="flex items-baseline justify-between gap-3 px-4 py-1.5 hover:bg-raised"
                >
                  <span className="min-w-0 truncate text-sm text-muted">{line.label}</span>
                  <span
                    className={cn(
                      'shrink-0 text-sm tabular',
                      toPaise(line.amount) < 0 ? 'text-negative' : 'text-positive',
                    )}
                  >
                    {formatMoney(line.amount)}
                  </span>
                </div>
              ))
            )}
          </div>
        ))}
      </Card>
    </div>
  )
}

function MiniStat({
  label,
  value,
  strong,
  signed,
}: {
  label: string
  value: any
  strong?: boolean
  signed?: boolean
}) {
  return (
    <div className="card p-2.5">
      <p className="text-2xs uppercase tracking-wider text-muted">{label}</p>
      <p
        className={cn(
          'mt-0.5 tabular font-semibold',
          strong ? 'text-base' : 'text-sm',
          signed && toPaise(value) < 0 ? 'text-negative' : signed ? 'text-positive' : 'text-strong',
        )}
      >
        {formatMoney(value)}
      </p>
    </div>
  )
}

function Ratios({ range }: { range: any }) {
  const { data, isLoading } = useQuery({
    queryKey: ['reports', 'ratios', range],
    queryFn: () =>
      api.get('/reports/ratios', { as_of: range.end_date, start_date: range.start_date }),
  })

  if (isLoading) return <Spinner label="Computing ratios" />
  if (!data) return <EmptyState title="No ratio data" />

  const groups = [
    ['Liquidity', data.liquidity],
    ['Leverage', data.leverage],
    ['Profitability', data.profitability],
    ['Efficiency', data.efficiency],
  ] as [string, any][]

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      {groups.map(([title, metrics]) => (
        <Card key={title} title={title} bodyClassName="p-0">
          {Object.entries(metrics).map(([key, metric]: [string, any]) => (
            <div key={key} className="border-b border-border px-4 py-2.5 last:border-0">
              <div className="flex items-baseline justify-between gap-3">
                <span className="text-sm text-strong">
                  {key.replace(/_/g, ' ').replace(/percent/, '%')}
                </span>
                <span className="text-base font-semibold tabular text-strong">
                  {metric.value === null
                    ? '—'
                    : key.includes('percent')
                      ? `${Number(metric.value).toFixed(1)}%`
                      : Number(metric.value).toFixed(2)}
                </span>
              </div>
              <p className="mt-0.5 text-2xs text-muted">{metric.interpretation}</p>
              {metric.inputs && (
                <p className="mt-1 text-2xs text-subtle">
                  {Object.entries(metric.inputs)
                    .map(([name, value]) => `${name.replace(/_/g, ' ')}: ${formatMoney(value as any)}`)
                    .join(' · ')}
                </p>
              )}
            </div>
          ))}
        </Card>
      ))}
    </div>
  )
}

function Aging({ kind }: { kind: 'ar' | 'ap' }) {
  const { data, isLoading } = useQuery({
    queryKey: ['reports', `${kind}-aging`],
    queryFn: () => api.get(`/reports/${kind}-aging`),
  })

  if (isLoading) return <Spinner label="Computing aging" />
  if (!data?.rows?.length) {
    return (
      <Card>
        <EmptyState
          title={kind === 'ar' ? 'Nothing outstanding from customers' : 'Nothing owed to vendors'}
        />
      </Card>
    )
  }

  const buckets = ['current', '1-30', '31-60', '61-90', '90+']

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
        {buckets.map((bucket) => (
          <div key={bucket} className="card p-2.5">
            <p className="text-2xs uppercase tracking-wider text-muted">
              {bucket === 'current' ? 'Not yet due' : `${bucket} days`}
            </p>
            <p
              className={cn(
                'mt-0.5 text-sm font-semibold tabular',
                bucket === '90+' && toPaise(data.bucket_totals[bucket]) > 0
                  ? 'text-negative'
                  : 'text-strong',
              )}
            >
              {formatMoney(data.bucket_totals[bucket], { compact: true })}
            </p>
          </div>
        ))}
      </div>

      <Card bodyClassName="p-0">
        <div className="scroll-x">
          <table className="w-full min-w-[720px]">
            <thead>
              <tr className="table-head">
                <th className="px-3 py-2 text-left">{kind === 'ar' ? 'Customer' : 'Vendor'}</th>
                {buckets.map((bucket) => (
                  <th key={bucket} className="px-3 py-2 text-right">
                    {bucket === 'current' ? 'Current' : bucket}
                  </th>
                ))}
                <th className="px-3 py-2 text-right">Total</th>
                <th className="px-3 py-2 text-right">Oldest</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {data.rows.map((row: any) => (
                <tr key={row.customer_id ?? row.vendor_id} className="hover:bg-raised">
                  <td className="px-3 py-2 text-sm font-medium text-strong">{row.name}</td>
                  {buckets.map((bucket) => (
                    <td
                      key={bucket}
                      className={cn(
                        'px-3 py-2 text-right text-sm tabular',
                        bucket === '90+' && toPaise(row.buckets[bucket]) > 0
                          ? 'font-medium text-negative'
                          : 'text-muted',
                      )}
                    >
                      {toPaise(row.buckets[bucket]) ? formatMoney(row.buckets[bucket]) : ''}
                    </td>
                  ))}
                  <td className="px-3 py-2 text-right text-sm font-semibold tabular">
                    {formatMoney(row.total)}
                  </td>
                  <td className="px-3 py-2 text-right text-sm tabular text-muted">
                    {row.oldest_days > 0 ? `${row.oldest_days}d` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot className="border-t-2 border-border bg-raised">
              <tr>
                <td className="px-3 py-2 text-xs font-semibold text-muted">Total</td>
                {buckets.map((bucket) => (
                  <td key={bucket} className="px-3 py-2 text-right text-sm font-semibold tabular">
                    {formatMoney(data.bucket_totals[bucket])}
                  </td>
                ))}
                <td className="px-3 py-2 text-right text-sm font-bold tabular">
                  {formatMoney(data.total_outstanding)}
                </td>
                <td />
              </tr>
            </tfoot>
          </table>
        </div>
      </Card>
    </div>
  )
}
