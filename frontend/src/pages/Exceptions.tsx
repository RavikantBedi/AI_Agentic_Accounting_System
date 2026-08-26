/** Exception centre — "needs your attention", with evidence and a recommended action. */
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, RefreshCw, ShieldAlert } from 'lucide-react'
import { api } from '@/lib/api'
import { formatMoney, formatRelative } from '@/lib/money'
import { useRefreshBooks } from '@/lib/hooks'
import {
  Alert,
  Amount,
  Badge,
  Button,
  Card,
  EmptyState,
  Modal,
  PageHeader,
  SeverityDot,
  Spinner,
  Tabs,
  Toolbar,
  cn,
  useToast,
} from '@/components/ui'

export default function Exceptions() {
  const [params, setParams] = useSearchParams()
  const { push } = useToast()
  const queryClient = useQueryClient()
  const refresh = useRefreshBooks()

  const [tab, setTab] = useState('exceptions')
  const [category, setCategory] = useState(params.get('category') ?? '')
  const [resolving, setResolving] = useState<any>(null)

  const summary = useQuery({
    queryKey: ['exception-summary'],
    queryFn: () => api.get('/exceptions/summary'),
  })
  const exceptions = useQuery({
    queryKey: ['exceptions', category],
    queryFn: () => api.get('/exceptions', { category: category || undefined, limit: 100 }),
  })
  const anomalies = useQuery({
    queryKey: ['anomalies'],
    queryFn: () => api.get('/anomalies', { limit: 100 }),
    enabled: tab === 'anomalies',
  })

  const scan = useMutation({
    mutationFn: () => api.post('/anomalies/scan', undefined, { lookback_days: 180 }),
    onSuccess: (result) => {
      push({
        tone: result.created > 0 ? 'warning' : 'positive',
        title: `${result.created} new anomal${result.created === 1 ? 'y' : 'ies'} detected`,
        description: `${result.findings} findings across ${result.scanned_from} to ${result.scanned_to}.`,
      })
      queryClient.invalidateQueries({ queryKey: ['anomalies'] })
      queryClient.invalidateQueries({ queryKey: ['exception-summary'] })
      refresh()
    },
  })

  const resolve = useMutation({
    mutationFn: ({ id, note, dismiss }: any) =>
      api.post(`/exceptions/${id}/resolve`, { note, dismiss }),
    onSuccess: (_result, variables) => {
      push({
        tone: 'positive',
        title: variables.dismiss ? 'Dismissed' : 'Marked resolved',
      })
      queryClient.invalidateQueries({ queryKey: ['exceptions'] })
      queryClient.invalidateQueries({ queryKey: ['exception-summary'] })
      setResolving(null)
    },
  })

  const reviewAnomaly = useMutation({
    mutationFn: ({ id, truePositive }: any) =>
      api.post(`/anomalies/${id}/review`, undefined, { is_true_positive: truePositive }),
    onSuccess: () => {
      push({ tone: 'positive', title: 'Anomaly reviewed' })
      queryClient.invalidateQueries({ queryKey: ['anomalies'] })
      queryClient.invalidateQueries({ queryKey: ['exception-summary'] })
    },
  })

  const categories = summary.data?.by_category ?? []

  return (
    <div>
      <PageHeader
        title="Needs your attention"
        description="Everything the system cannot resolve on its own, with the evidence behind it."
        actions={
          <Button size="sm" icon={RefreshCw} loading={scan.isPending} onClick={() => scan.mutate()}>
            Scan for anomalies
          </Button>
        }
      >
        {categories.length > 0 && (
          <Toolbar>
            <button
              onClick={() => {
                setCategory('')
                setParams({})
              }}
              className={cn(
                'rounded-lg border px-2.5 py-1 text-xs font-medium transition-colors',
                !category
                  ? 'border-brand bg-brand-soft text-brand'
                  : 'border-border text-muted hover:text-strong',
              )}
            >
              All ({summary.data?.total_open ?? 0})
            </button>
            {categories.map((entry: any) => (
              <button
                key={entry.category}
                onClick={() => {
                  setCategory(entry.category)
                  setParams({ category: entry.category })
                }}
                className={cn(
                  'rounded-lg border px-2.5 py-1 text-xs font-medium transition-colors',
                  category === entry.category
                    ? 'border-brand bg-brand-soft text-brand'
                    : 'border-border text-muted hover:text-strong',
                )}
              >
                {entry.category} ({entry.count})
              </button>
            ))}
          </Toolbar>
        )}
      </PageHeader>

      {summary.data && summary.data.total_open > 0 && (
        <Alert tone="warning" className="mb-3">
          {summary.data.total_open} open item(s)
          {Number(summary.data.total_amount) > 0 &&
            ` involving ${formatMoney(summary.data.total_amount)}`}
          .
        </Alert>
      )}

      <Tabs
        tabs={[
          { id: 'exceptions', label: 'Exceptions', count: exceptions.data?.total },
          { id: 'anomalies', label: 'Anomalies', count: anomalies.data?.total },
        ]}
        active={tab}
        onChange={setTab}
        className="mb-3"
      />

      {tab === 'exceptions' ? (
        exceptions.isLoading ? (
          <Spinner label="Loading exceptions" />
        ) : !exceptions.data?.items?.length ? (
          <Card>
            <EmptyState
              icon={CheckCircle2}
              title="Nothing needs your attention"
              description="Every exception has been resolved or dismissed."
            />
          </Card>
        ) : (
          <div className="space-y-2.5">
            {exceptions.data.items.map((item: any) => (
              <Card key={item.id} bodyClassName="p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <SeverityDot severity={item.severity} />
                      <span className="text-sm font-medium text-strong">{item.title}</span>
                      <Badge tone="neutral">{item.category}</Badge>
                      {item.detected_by === 'AI' && <Badge tone="info">AI detected</Badge>}
                    </div>
                    <p className="mt-1 text-xs text-muted">{item.reason}</p>
                    {item.recommended_action && (
                      <p className="mt-1.5 rounded-lg bg-brand-soft/40 px-2.5 py-1.5 text-2xs text-strong">
                        <strong>Recommended:</strong> {item.recommended_action}
                      </p>
                    )}
                    {item.evidence?.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {item.evidence.slice(0, 5).map((evidence: any, index: number) => (
                          <span
                            key={index}
                            className="rounded-md bg-raised px-1.5 py-0.5 text-2xs text-muted"
                          >
                            {evidence.label ?? evidence.type}
                            {evidence.amount && ` · ${formatMoney(evidence.amount)}`}
                          </span>
                        ))}
                      </div>
                    )}
                    <p className="mt-1.5 text-2xs text-subtle">
                      {formatRelative(item.created_at)}
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-2">
                    {item.amount && (
                      <Amount value={item.amount} className="text-sm font-semibold" />
                    )}
                    <div className="flex gap-1.5">
                      {item.action_url && (
                        <Link to={item.action_url}>
                          <Button size="sm">Open</Button>
                        </Link>
                      )}
                      <Button size="sm" variant="primary" onClick={() => setResolving(item)}>
                        Resolve
                      </Button>
                    </div>
                  </div>
                </div>
              </Card>
            ))}
          </div>
        )
      ) : anomalies.isLoading ? (
        <Spinner label="Loading anomalies" />
      ) : !anomalies.data?.items?.length ? (
        <Card>
          <EmptyState
            icon={ShieldAlert}
            title="No open anomalies"
            description="Run a scan to check recent activity against twelve detectors."
            action={
              <Button size="sm" loading={scan.isPending} onClick={() => scan.mutate()}>
                Scan now
              </Button>
            }
          />
        </Card>
      ) : (
        <div className="space-y-2.5">
          {anomalies.data.items.map((anomaly: any) => (
            <Card key={anomaly.id} bodyClassName="p-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <SeverityDot severity={anomaly.severity} />
                    <span className="text-sm font-medium text-strong">{anomaly.title}</span>
                    <Badge tone="neutral">{anomaly.kind.replace(/_/g, ' ')}</Badge>
                    <Badge tone="info">{anomaly.detection_method.toLowerCase()}</Badge>
                  </div>
                  <p className="mt-1 text-xs text-muted">{anomaly.reason}</p>
                  {anomaly.recommendation && (
                    <p className="mt-1.5 rounded-lg bg-brand-soft/40 px-2.5 py-1.5 text-2xs text-strong">
                      <strong>Recommended:</strong> {anomaly.recommendation}
                    </p>
                  )}

                  {anomaly.baseline_value && (
                    <p className="mt-1.5 text-2xs text-subtle tabular">
                      Baseline {formatMoney(anomaly.baseline_value)} · observed{' '}
                      {formatMoney(anomaly.observed_value)}
                      {anomaly.deviation_score &&
                        ` · ${Number(anomaly.deviation_score).toFixed(1)} deviation`}
                    </p>
                  )}

                  {anomaly.evidence?.length > 0 && (
                    <details className="mt-2">
                      <summary className="cursor-pointer text-2xs font-medium text-brand">
                        View {anomaly.evidence.length} piece(s) of evidence
                      </summary>
                      <ul className="mt-1.5 space-y-1">
                        {anomaly.evidence.map((evidence: any, index: number) => (
                          <li
                            key={index}
                            className="flex items-center gap-2 rounded-lg bg-raised px-2.5 py-1.5 text-2xs"
                          >
                            <Badge tone="neutral">{evidence.type ?? 'record'}</Badge>
                            <span className="min-w-0 flex-1 truncate text-muted">
                              {evidence.label}
                            </span>
                            {evidence.date && (
                              <span className="shrink-0 text-subtle">{evidence.date}</span>
                            )}
                            {evidence.amount && (
                              <span className="shrink-0 tabular text-strong">
                                {formatMoney(evidence.amount)}
                              </span>
                            )}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>

                <div className="flex shrink-0 flex-col items-end gap-2">
                  {anomaly.amount && (
                    <Amount value={anomaly.amount} className="text-sm font-semibold" />
                  )}
                  <div className="flex gap-1.5">
                    <Button
                      size="sm"
                      onClick={() =>
                        reviewAnomaly.mutate({ id: anomaly.id, truePositive: false })
                      }
                    >
                      Not an issue
                    </Button>
                    <Button
                      size="sm"
                      variant="primary"
                      onClick={() =>
                        reviewAnomaly.mutate({ id: anomaly.id, truePositive: true })
                      }
                    >
                      Confirm and resolve
                    </Button>
                  </div>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      {resolving && (
        <ResolveDialog
          item={resolving}
          onClose={() => setResolving(null)}
          onResolve={(note, dismiss) =>
            resolve.mutate({ id: resolving.id, note, dismiss })
          }
          busy={resolve.isPending}
        />
      )}
    </div>
  )
}

function ResolveDialog({
  item,
  onClose,
  onResolve,
  busy,
}: {
  item: any
  onClose: () => void
  onResolve: (note: string, dismiss: boolean) => void
  busy: boolean
}) {
  const [note, setNote] = useState('')

  return (
    <Modal
      open
      onClose={onClose}
      title="Resolve exception"
      description={item.title}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button loading={busy} onClick={() => onResolve(note, true)}>
            Dismiss
          </Button>
          <Button variant="primary" loading={busy} onClick={() => onResolve(note, false)}>
            Mark resolved
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <p className="rounded-lg bg-raised p-3 text-xs text-muted">{item.reason}</p>
        <div>
          <p className="label">What did you do about it?</p>
          <textarea
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Recorded in the audit trail alongside your name."
            className="input h-auto min-h-[80px] resize-y py-2"
          />
        </div>
        <p className="text-2xs text-subtle">
          <strong>Dismiss</strong> if this was a false positive. <strong>Mark resolved</strong> if
          you acted on it.
        </p>
      </div>
    </Modal>
  )
}
