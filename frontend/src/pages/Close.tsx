/** Month-end close — every item backed by a real, re-runnable check. */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  CheckCircle2,
  ClipboardCheck,
  Circle,
  Lock,
  Play,
  RefreshCw,
} from 'lucide-react'
import { api } from '@/lib/api'
import { formatDateTime, formatMoney, toPaise } from '@/lib/money'
import { currentPeriod, periodOptions, usePermission, useRefreshBooks } from '@/lib/hooks'
import {
  Alert,
  Amount,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  PageHeader,
  Progress,
  Select,
  Spinner,
  StatusBadge,
  Toolbar,
  cn,
  useToast,
} from '@/components/ui'

export default function Close() {
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()
  const canClose = usePermission('close_period')
  const [period, setPeriod] = useState(currentPeriod())

  const status = useQuery({
    queryKey: ['close-status', period],
    queryFn: () => api.get('/close/status', { period }),
  })

  const runChecklist = useMutation({
    mutationFn: () => api.post('/close/run', undefined, { period }),
    onSuccess: (result) => {
      push({
        tone: result.blocked_tasks > 0 ? 'warning' : 'positive',
        title: `Checklist re-run for ${result.period_label}`,
        description: `${result.completed_tasks} of ${result.total_tasks} checks passing${
          result.blocked_tasks ? `, ${result.blocked_tasks} blocking` : ''
        }.`,
      })
      queryClient.invalidateQueries({ queryKey: ['close-status'] })
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Could not run the checklist', description: error?.message }),
  })

  const runAgent = useMutation({
    mutationFn: () => api.post('/ai/agents/CLOSE/run', { params: { period } }),
    onSuccess: (result) => {
      push({ tone: 'info', title: 'Close agent finished', description: result.summary })
      queryClient.invalidateQueries({ queryKey: ['close-status'] })
    },
  })

  const approve = useMutation({
    mutationFn: (notes: string) =>
      api.post('/close/approve', undefined, { period, notes }),
    onSuccess: () => {
      push({
        tone: 'positive',
        title: 'Period closed',
        description: 'No further entries can be posted into it without a formal re-open.',
      })
      queryClient.invalidateQueries({ queryKey: ['close-status'] })
      refresh()
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Could not close the period', description: error?.message }),
  })

  const data = status.data
  const closed = data?.period_status === 'CLOSED'

  return (
    <div>
      <PageHeader
        title="Month-end close"
        description="Sixteen checks run against live data. Blocking items cannot be ticked by hand — they pass only when the underlying check passes."
        actions={
          <>
            <Button
              size="sm"
              icon={RefreshCw}
              loading={runChecklist.isPending}
              onClick={() => runChecklist.mutate()}
            >
              Re-run checks
            </Button>
            <Button
              size="sm"
              icon={Play}
              loading={runAgent.isPending}
              onClick={() => runAgent.mutate()}
            >
              Run close agent
            </Button>
            {canClose && data?.can_close && !closed && (
              <Button
                variant="primary"
                size="sm"
                icon={Lock}
                loading={approve.isPending}
                onClick={() => {
                  const notes = window.prompt('Any closing notes for the audit trail?') ?? ''
                  approve.mutate(notes)
                }}
              >
                Close period
              </Button>
            )}
          </>
        }
      >
        <Toolbar>
          <Field label="Period" className="w-52">
            <Select value={period} onChange={(event) => setPeriod(event.target.value)}>
              {periodOptions(18).map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </Field>
        </Toolbar>
      </PageHeader>

      {status.isLoading ? (
        <Spinner label="Loading the close checklist" />
      ) : !data?.exists ? (
        <Card>
          <EmptyState
            icon={ClipboardCheck}
            title="The checklist has not been run for this period"
            description={data?.message ?? 'Run the checks to see what is outstanding.'}
            action={
              <Button
                variant="primary"
                size="sm"
                loading={runChecklist.isPending}
                onClick={() => runChecklist.mutate()}
              >
                Run checklist
              </Button>
            }
          />
        </Card>
      ) : (
        <>
          <Card className="mb-3" bodyClassName="p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <h2 className="text-base font-semibold text-strong">{data.period_label}</h2>
                  <StatusBadge status={data.period_status} />
                </div>
                <p className="mt-0.5 text-xs text-muted">
                  {data.completed_tasks} of {data.total_tasks} checks passing
                  {data.blocked_tasks > 0 && ` · ${data.blocked_tasks} blocking`}
                  {data.last_run && ` · last run ${formatDateTime(data.last_run)}`}
                </p>
              </div>
              <div className="text-right">
                <p className="text-2xl font-bold tabular text-strong">
                  {Number(data.progress_percent).toFixed(0)}%
                </p>
              </div>
            </div>
            <Progress
              value={Number(data.progress_percent)}
              tone={data.blocked_tasks > 0 ? 'warning' : 'positive'}
              className="mt-3"
            />
          </Card>

          {closed && (
            <Alert tone="positive" className="mb-3" title="This period is closed">
              Entries can no longer be posted into it. Corrections must be made as adjustments in
              an open period, or the period must be formally re-opened with a recorded reason.
            </Alert>
          )}

          {data.ai_summary && (
            <Alert tone="info" className="mb-3" title="Close agent summary">
              {data.ai_summary}
            </Alert>
          )}

          {data.blocking_issues?.length > 0 && (
            <Alert
              tone="negative"
              className="mb-3"
              title={`${data.blocking_issues.length} issue(s) block this close`}
            >
              <ul className="mt-1 space-y-0.5">
                {data.blocking_issues.map((issue: any) => (
                  <li key={issue.key}>
                    <strong>{issue.name}:</strong> {issue.detail}
                  </li>
                ))}
              </ul>
            </Alert>
          )}

          <Card title="Checklist" bodyClassName="p-0">
            <ul className="divide-y divide-border">
              {data.tasks.map((task: any) => (
                <TaskRow key={task.id} task={task} />
              ))}
            </ul>
          </Card>
        </>
      )}
    </div>
  )
}

function TaskRow({ task }: { task: any }) {
  const [expanded, setExpanded] = useState(false)
  const evidence = task.evidence ?? []

  const Icon =
    task.status === 'COMPLETED'
      ? CheckCircle2
      : task.status === 'BLOCKED'
        ? AlertTriangle
        : Circle

  return (
    <li>
      <button
        onClick={() => evidence.length > 0 && setExpanded((value) => !value)}
        className={cn(
          'flex w-full items-start gap-3 px-4 py-3 text-left transition-colors',
          evidence.length > 0 && 'hover:bg-raised',
        )}
        aria-expanded={evidence.length > 0 ? expanded : undefined}
      >
        <Icon
          className={cn(
            'mt-0.5 h-4 w-4 shrink-0',
            task.status === 'COMPLETED'
              ? 'text-positive'
              : task.status === 'BLOCKED'
                ? 'text-negative'
                : 'text-subtle',
          )}
          aria-hidden
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                'text-sm',
                task.status === 'COMPLETED' ? 'text-muted' : 'font-medium text-strong',
              )}
            >
              {task.name}
            </span>
            <Badge tone="neutral">{task.category}</Badge>
            {task.is_blocking && task.status !== 'COMPLETED' && (
              <Badge tone="negative">Blocking</Badge>
            )}
          </div>
          <p className="mt-0.5 text-2xs text-muted">{task.description}</p>
          {toPaise(task.amount_at_risk) > 0 && (
            <p className="mt-1 text-2xs">
              <span className="text-muted">Amount involved: </span>
              <Amount value={task.amount_at_risk} className="font-medium text-warning" />
            </p>
          )}
        </div>
        <div className="shrink-0 text-right">
          {task.issue_count > 0 && (
            <Badge tone={task.status === 'BLOCKED' ? 'negative' : 'warning'}>
              {task.issue_count} item{task.issue_count === 1 ? '' : 's'}
            </Badge>
          )}
        </div>
      </button>

      {expanded && evidence.length > 0 && (
        <div className="border-t border-border bg-raised/40 px-4 py-2.5">
          <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-muted">
            Evidence
          </p>
          <ul className="space-y-1">
            {evidence.slice(0, 10).map((item: any, index: number) => (
              <li key={index} className="flex items-center gap-2 text-2xs">
                <Badge tone="neutral">{item.type ?? item.label}</Badge>
                <span className="min-w-0 flex-1 truncate text-muted">
                  {item.label ?? item.detail}
                </span>
                {item.amount && (
                  <span className="shrink-0 tabular text-strong">
                    {formatMoney(item.amount)}
                  </span>
                )}
                {item.passed !== undefined && (
                  <Badge tone={item.passed ? 'positive' : 'negative'}>
                    {item.passed ? 'pass' : 'fail'}
                  </Badge>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </li>
  )
}
