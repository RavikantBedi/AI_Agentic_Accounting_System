/** Overview — the 10-second answer to "how is my business doing?". */
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  AlertTriangle,
  ArrowRight,
  Banknote,
  CheckCircle2,
  ClipboardCheck,
  Package,
  Receipt,
  ShieldAlert,
  Sparkles,
  TrendingDown,
  TrendingUp,
  Wallet,
} from 'lucide-react'
import { api } from '@/lib/api'
import { formatDate, formatMoney, formatPercent, percentChange, toNumber } from '@/lib/money'
import {
  Alert,
  Amount,
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Progress,
  SeverityDot,
  StatCard,
} from '@/components/ui'

const CHART_COLOURS = ['#2563eb', '#0891b2', '#7c3aed', '#059669', '#d97706', '#dc2626', '#4f46e5', '#0d9488']

export default function Dashboard() {
  const navigate = useNavigate()

  const dashboard = useQuery({
    queryKey: ['dashboard'],
    queryFn: () => api.get('/reports/dashboard'),
  })
  const briefing = useQuery({ queryKey: ['briefing'], queryFn: () => api.get('/ai/briefing') })
  const exceptions = useQuery({
    queryKey: ['exceptions', 'top'],
    queryFn: () => api.get('/exceptions', { limit: 5 }),
  })
  const agents = useQuery({ queryKey: ['agents'], queryFn: () => api.get('/ai/agents') })

  if (dashboard.isError) {
    return <ErrorState error={dashboard.error} retry={() => dashboard.refetch()} />
  }

  const kpis = dashboard.data?.kpis
  const trend = dashboard.data?.trend ?? []
  const health = dashboard.data?.health
  const close = dashboard.data?.close
  const loading = dashboard.isLoading

  // Compare the last two complete months for the KPI deltas.
  const thisMonth = trend[trend.length - 1]
  const lastMonth = trend[trend.length - 2]

  const chartData = trend.map((point: any) => ({
    label: point.label.split(' ')[0],
    Revenue: toNumber(point.revenue),
    Expenses: toNumber(point.expenses),
    Profit: toNumber(point.profit),
  }))

  const expenseData = (dashboard.data?.expense_breakdown ?? [])
    .slice(0, 8)
    .map((line: any) => ({ name: line.name, value: toNumber(line.amount), code: line.code }))

  const activeAgents = (agents.data?.agents ?? []).filter(
    (agent: any) => agent.last_status !== 'NEVER_RUN',
  )

  return (
    <div className="space-y-4">
      {/* Every screen needs exactly one h1 so screen readers announce where
          the user is. The dashboard's is visually subtle but structurally real. */}
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-lg font-semibold tracking-tight text-strong">Overview</h1>
        <p className="text-xs text-muted">
          {dashboard.data?.period
            ? `${formatDate(dashboard.data.period.start_date)} — ${formatDate(dashboard.data.period.end_date)}`
            : 'Financial year to date'}
        </p>
      </div>

      {/* AI briefing */}
      <Card
        className="border-brand/20 bg-gradient-to-br from-brand-soft/50 to-transparent"
        bodyClassName="p-4"
      >
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-brand text-brand-fg">
            <Sparkles className="h-4.5 w-4.5" aria-hidden />
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-sm font-semibold text-strong">Good morning</h2>
              <Badge tone="brand">AI briefing</Badge>
            </div>
            <p className="mt-1 text-sm text-muted">
              {briefing.isLoading
                ? 'Reviewing your books…'
                : briefing.data?.summary ?? 'Nothing needs your attention right now.'}
            </p>
            {briefing.data?.highlights?.length > 0 && (
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                {briefing.data.highlights.map((item: any) => (
                  <Link
                    key={item.kind}
                    to={item.action_url}
                    className="rounded-md border border-border bg-surface px-2 py-1 text-2xs font-medium text-muted transition-colors hover:border-brand/40 hover:text-strong"
                  >
                    {item.text}
                  </Link>
                ))}
              </div>
            )}
          </div>
          <Button
            variant="primary"
            size="sm"
            className="shrink-0"
            onClick={() => navigate('/ai')}
          >
            Ask a question
            <ArrowRight className="h-3.5 w-3.5" aria-hidden />
          </Button>
        </div>
      </Card>

      {/* Invariant health — the product's core promise, visible */}
      {health && !health.invariants_passed && (
        <Alert tone="negative" title="Accounting invariants are failing">
          <ul className="mt-1 space-y-0.5">
            {health.checks
              .filter((check: any) => !check.passed)
              .map((check: any) => (
                <li key={check.name}>
                  <strong>{check.name}:</strong> {check.detail}
                </li>
              ))}
          </ul>
        </Alert>
      )}

      {/* Primary KPIs */}
      <section aria-label="Key figures" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="Cash"
          value={formatMoney(kpis?.cash, { compact: true })}
          sub="Cash and bank"
          icon={Wallet}
          tone="brand"
          loading={loading}
          onClick={() => navigate('/banking')}
        />
        <StatCard
          label="Revenue"
          value={formatMoney(kpis?.revenue, { compact: true })}
          delta={thisMonth && lastMonth ? percentChange(thisMonth.revenue, lastMonth.revenue) : undefined}
          sub="YTD · arrow vs last month"
          icon={TrendingUp}
          tone="positive"
          loading={loading}
          onClick={() => navigate('/reports')}
        />
        <StatCard
          label="Expenses"
          value={formatMoney(kpis?.expenses, { compact: true })}
          delta={
            thisMonth && lastMonth ? percentChange(thisMonth.expenses, lastMonth.expenses) : undefined
          }
          sub="YTD · arrow vs last month"
          icon={TrendingDown}
          tone="warning"
          loading={loading}
          onClick={() => navigate('/reports')}
        />
        <StatCard
          label="Net profit"
          value={formatMoney(kpis?.net_profit, { compact: true })}
          delta={thisMonth && lastMonth ? percentChange(thisMonth.profit, lastMonth.profit) : undefined}
          sub={kpis ? `${formatPercent(kpis.net_margin_percent)} margin · vs last month` : undefined}
          icon={TrendingUp}
          tone={kpis && Number(kpis.net_profit) >= 0 ? 'positive' : 'negative'}
          loading={loading}
          onClick={() => navigate('/reports')}
        />
      </section>

      {/* Secondary KPIs */}
      <section aria-label="Working capital" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="Receivable"
          value={formatMoney(kpis?.receivable, { compact: true })}
          sub={
            kpis?.receivable_overdue && Number(kpis.receivable_overdue) > 0
              ? `${formatMoney(kpis.receivable_overdue, { compact: true })} overdue`
              : 'None overdue'
          }
          icon={Receipt}
          tone={Number(kpis?.receivable_overdue ?? 0) > 0 ? 'negative' : 'neutral'}
          loading={loading}
          onClick={() => navigate('/sales/invoices')}
        />
        <StatCard
          label="Payable"
          value={formatMoney(kpis?.payable, { compact: true })}
          sub={
            kpis?.payable_overdue && Number(kpis.payable_overdue) > 0
              ? `${formatMoney(kpis.payable_overdue, { compact: true })} overdue`
              : 'None overdue'
          }
          icon={Banknote}
          tone={Number(kpis?.payable_overdue ?? 0) > 0 ? 'warning' : 'neutral'}
          loading={loading}
          onClick={() => navigate('/purchases/bills')}
        />
        <StatCard
          label="Unreconciled"
          value={kpis?.unreconciled_count ?? 0}
          sub={
            kpis?.unreconciled_amount
              ? `${formatMoney(kpis.unreconciled_amount, { compact: true })} unmatched`
              : undefined
          }
          icon={ShieldAlert}
          tone={Number(kpis?.unreconciled_count ?? 0) > 0 ? 'warning' : 'positive'}
          loading={loading}
          onClick={() => navigate('/banking/reconciliation')}
        />
        <StatCard
          label="Inventory"
          value={formatMoney(kpis?.inventory_value, { compact: true })}
          sub={
            kpis?.low_stock_count
              ? `${kpis.low_stock_count} item(s) low`
              : 'Stock levels healthy'
          }
          icon={Package}
          tone={Number(kpis?.low_stock_count ?? 0) > 0 ? 'warning' : 'neutral'}
          loading={loading}
          onClick={() => navigate('/inventory')}
        />
      </section>

      {/* Charts */}
      <section className="grid gap-3 lg:grid-cols-3">
        <Card
          title="Revenue, expenses and profit"
          description="Last 12 months, from posted ledger entries"
          className="lg:col-span-2"
          bodyClassName="p-2 pt-4"
        >
          {loading ? (
            <div className="skeleton m-2 h-64" />
          ) : chartData.length === 0 ? (
            <EmptyState title="No posted transactions yet" />
          ) : (
            <ResponsiveContainer width="100%" height={264}>
              <AreaChart data={chartData} margin={{ top: 4, right: 12, left: 4, bottom: 0 }}>
                <defs>
                  <linearGradient id="revenueFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#2563eb" stopOpacity={0.28} />
                    <stop offset="100%" stopColor="#2563eb" stopOpacity={0.02} />
                  </linearGradient>
                  <linearGradient id="expenseFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#d97706" stopOpacity={0.22} />
                    <stop offset="100%" stopColor="#d97706" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border))" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }}
                  axisLine={false}
                  tickLine={false}
                  width={54}
                  tickFormatter={(value) => formatMoney(value, { compact: true, symbol: false })}
                />
                <Tooltip content={<ChartTooltip />} />
                <Area
                  type="monotone"
                  dataKey="Revenue"
                  stroke="#2563eb"
                  strokeWidth={2}
                  fill="url(#revenueFill)"
                />
                <Area
                  type="monotone"
                  dataKey="Expenses"
                  stroke="#d97706"
                  strokeWidth={2}
                  fill="url(#expenseFill)"
                />
              </AreaChart>
            </ResponsiveContainer>
          )}
          <div className="flex flex-wrap gap-4 px-4 pb-2 pt-1">
            {[
              ['Revenue', '#2563eb'],
              ['Expenses', '#d97706'],
            ].map(([label, colour]) => (
              <span key={label} className="flex items-center gap-1.5 text-2xs text-muted">
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ background: colour }}
                  aria-hidden
                />
                {label}
              </span>
            ))}
          </div>
        </Card>

        <Card
          title="Where the money goes"
          description="Largest expense accounts"
          bodyClassName="p-2 pt-4"
        >
          {loading ? (
            <div className="skeleton m-2 h-64" />
          ) : expenseData.length === 0 ? (
            <EmptyState title="No expenses recorded yet" />
          ) : (
            <ResponsiveContainer width="100%" height={264}>
              <BarChart
                data={expenseData}
                layout="vertical"
                margin={{ top: 0, right: 16, left: 4, bottom: 0 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="rgb(var(--border))" horizontal={false} />
                <XAxis
                  type="number"
                  tick={{ fontSize: 11, fill: 'rgb(var(--muted))' }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(value) => formatMoney(value, { compact: true, symbol: false })}
                />
                <YAxis
                  type="category"
                  dataKey="name"
                  width={104}
                  tick={{ fontSize: 10, fill: 'rgb(var(--muted))' }}
                  axisLine={false}
                  tickLine={false}
                  tickFormatter={(value: string) =>
                    value.length > 16 ? `${value.slice(0, 15)}…` : value
                  }
                />
                <Tooltip content={<ChartTooltip />} cursor={{ fill: 'rgb(var(--raised))' }} />
                <Bar dataKey="value" radius={[0, 4, 4, 0]} name="Amount">
                  {expenseData.map((_: any, index: number) => (
                    <Cell key={index} fill={CHART_COLOURS[index % CHART_COLOURS.length]} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          )}
        </Card>
      </section>

      {/* AI activity, exceptions, close */}
      <section className="grid gap-3 lg:grid-cols-3">
        <Card
          title="AI activity"
          description="Measured over the last 30 days"
          actions={
            <Link to="/agents" className="text-2xs font-medium text-brand hover:underline">
              All agents
            </Link>
          }
          bodyClassName="divide-y divide-border"
        >
          {agents.isLoading ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 3 }).map((_, index) => (
                <div key={index} className="skeleton h-12" />
              ))}
            </div>
          ) : activeAgents.length === 0 ? (
            <div className="p-4">
              <EmptyState
                title="No agents have run yet"
                description="Run one from the AI Agents page and its measured results will appear here."
                action={
                  <Button size="sm" onClick={() => navigate('/agents')}>
                    Open AI Agents
                  </Button>
                }
              />
            </div>
          ) : (
            activeAgents.slice(0, 5).map((agent: any) => (
              <div key={agent.kind} className="flex items-start gap-2.5 px-4 py-2.5">
                <span
                  className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${
                    agent.last_status === 'COMPLETED'
                      ? 'bg-positive'
                      : agent.last_status === 'NEEDS_REVIEW'
                        ? 'bg-warning'
                        : agent.last_status === 'FAILED'
                          ? 'bg-negative'
                          : 'bg-subtle'
                  }`}
                  aria-hidden
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <p className="truncate text-xs font-medium text-strong">{agent.name}</p>
                    <span className="shrink-0 text-2xs text-subtle">
                      {agent.last_status.replace(/_/g, ' ').toLowerCase()}
                    </span>
                  </div>
                  <p className="mt-0.5 line-clamp-2 text-2xs text-muted">
                    {agent.last_summary || 'No summary recorded.'}
                  </p>
                  <div className="mt-1 flex flex-wrap gap-2 text-2xs text-subtle tabular">
                    <span>{agent.items_processed_30d} processed</span>
                    {agent.needs_review_30d > 0 && (
                      <span className="text-warning">{agent.needs_review_30d} need review</span>
                    )}
                    {agent.measured_accuracy != null && (
                      <span className="text-positive">
                        {(Number(agent.measured_accuracy) * 100).toFixed(0)}% accepted
                      </span>
                    )}
                  </div>
                </div>
              </div>
            ))
          )}
        </Card>

        <Card
          title="Needs your attention"
          description="Open exceptions, most severe first"
          actions={
            <Link to="/exceptions" className="text-2xs font-medium text-brand hover:underline">
              View all
            </Link>
          }
          bodyClassName="divide-y divide-border"
        >
          {exceptions.isLoading ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 3 }).map((_, index) => (
                <div key={index} className="skeleton h-12" />
              ))}
            </div>
          ) : (exceptions.data?.items ?? []).length === 0 ? (
            <div className="p-4">
              <EmptyState
                icon={CheckCircle2}
                title="Nothing needs your attention"
                description="Every exception has been resolved."
              />
            </div>
          ) : (
            exceptions.data.items.map((item: any) => (
              <Link
                key={item.id}
                to={item.action_url || '/exceptions'}
                className="block px-4 py-2.5 transition-colors hover:bg-raised"
              >
                <div className="flex items-start justify-between gap-2">
                  <p className="min-w-0 flex-1 truncate text-xs font-medium text-strong">
                    {item.title}
                  </p>
                  {item.amount && (
                    <Amount
                      value={item.amount}
                      compact
                      className="shrink-0 text-xs font-semibold"
                    />
                  )}
                </div>
                <p className="mt-0.5 line-clamp-2 text-2xs text-muted">{item.reason}</p>
                <div className="mt-1 flex items-center gap-2">
                  <SeverityDot severity={item.severity} />
                  <Badge tone="neutral">{item.category}</Badge>
                </div>
              </Link>
            ))
          )}
        </Card>

        <Card
          title="Month-end close"
          description={close?.period_label ?? 'Current period'}
          actions={
            <Link to="/close" className="text-2xs font-medium text-brand hover:underline">
              Open
            </Link>
          }
        >
          {!close?.exists ? (
            <EmptyState
              icon={ClipboardCheck}
              title="Close not started"
              description={close?.message ?? 'Run the checklist to see what is outstanding.'}
              action={
                <Button size="sm" onClick={() => navigate('/close')}>
                  Run checklist
                </Button>
              }
            />
          ) : (
            <div className="space-y-3">
              <div>
                <div className="mb-1.5 flex items-baseline justify-between">
                  <span className="text-2xs font-medium uppercase tracking-wider text-muted">
                    Progress
                  </span>
                  <span className="text-lg font-semibold tabular text-strong">
                    {Number(close.progress_percent).toFixed(0)}%
                  </span>
                </div>
                <Progress
                  value={Number(close.progress_percent)}
                  tone={close.blocked_tasks > 0 ? 'warning' : 'positive'}
                />
                <p className="mt-1.5 text-2xs text-muted">
                  {close.completed_tasks} of {close.total_tasks} checks passing
                  {close.blocked_tasks > 0 && ` · ${close.blocked_tasks} blocking`}
                </p>
              </div>

              <ul className="space-y-1.5">
                {(close.tasks ?? []).slice(0, 6).map((task: any) => (
                  <li key={task.id} className="flex items-start gap-2 text-2xs">
                    {task.status === 'COMPLETED' ? (
                      <CheckCircle2
                        className="mt-0.5 h-3.5 w-3.5 shrink-0 text-positive"
                        aria-hidden
                      />
                    ) : task.status === 'BLOCKED' ? (
                      <AlertTriangle
                        className="mt-0.5 h-3.5 w-3.5 shrink-0 text-negative"
                        aria-hidden
                      />
                    ) : (
                      <span
                        className="mt-1 h-2.5 w-2.5 shrink-0 rounded-full border border-border"
                        aria-hidden
                      />
                    )}
                    <span
                      className={
                        task.status === 'COMPLETED' ? 'text-muted' : 'font-medium text-strong'
                      }
                    >
                      {task.name}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>
      </section>

      {/* Invariants passing — quiet confirmation the books are sound */}
      {health?.invariants_passed && (
        <div className="flex items-center gap-2 rounded-lg border border-border bg-surface px-3 py-2 text-2xs text-muted">
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-positive" aria-hidden />
          All {health.checks.length} accounting invariants pass: debits equal credits, the trial
          balance balances, and assets equal liabilities plus equity.
        </div>
      )}
    </div>
  )
}

function ChartTooltip({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-lg border border-border bg-surface p-2.5 shadow-pop">
      <p className="mb-1 text-2xs font-semibold text-strong">{label ?? payload[0]?.payload?.name}</p>
      {payload.map((entry: any) => (
        <p key={entry.dataKey} className="flex items-center gap-2 text-2xs text-muted">
          <span
            className="h-2 w-2 rounded-full"
            style={{ background: entry.color || entry.fill }}
            aria-hidden
          />
          <span>{entry.name}</span>
          <span className="ml-auto font-semibold tabular text-strong">
            {formatMoney(entry.value)}
          </span>
        </p>
      ))}
    </div>
  )
}
