/** AI agents: what each does, how it has actually performed, and the review queue. */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, CheckCircle2, Play, ThumbsDown, ThumbsUp } from 'lucide-react'
import { api } from '@/lib/api'
import { formatRelative } from '@/lib/money'
import { usePermission, useRefreshBooks } from '@/lib/hooks'
import {
  Alert,
  Amount,
  Badge,
  Button,
  Card,
  ConfidenceBadge,
  EmptyState,
  Modal,
  PageHeader,
  SeverityDot,
  Spinner,
  StatusBadge,
  Tabs,
  cn,
  useToast,
} from '@/components/ui'

export default function Agents() {
  const [tab, setTab] = useState('agents')

  const agents = useQuery({ queryKey: ['agents'], queryFn: () => api.get('/ai/agents') })
  const proposals = useQuery({
    queryKey: ['proposals'],
    queryFn: () => api.get('/ai/proposals', { limit: 50 }),
  })
  const metrics = useQuery({ queryKey: ['ai-metrics'], queryFn: () => api.get('/ai/metrics') })
  const runs = useQuery({
    queryKey: ['agent-runs'],
    queryFn: () => api.get('/ai/runs', { limit: 30 }),
    enabled: tab === 'runs',
  })

  return (
    <div>
      <PageHeader
        title="AI agents"
        description="Seventeen specialists. Each proposes; deterministic rules validate; you approve."
      />

      {metrics.data && (
        <div className="mb-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
          <MetricCard
            label="Proposals pending"
            value={String(metrics.data.proposals_by_status?.PENDING ?? 0)}
            sub="Awaiting your decision"
          />
          <MetricCard
            label="Applied"
            value={String(metrics.data.proposals_by_status?.APPLIED ?? 0)}
            sub="Approved and posted"
          />
          <MetricCard
            label="Measured accuracy"
            value={
              metrics.data.measured_accuracy?.sufficient_data
                ? `${(Number(metrics.data.measured_accuracy.accuracy) * 100).toFixed(0)}%`
                : '—'
            }
            sub={metrics.data.measured_accuracy?.message}
          />
          <MetricCard
            label="Items processed"
            value={String(metrics.data.total_items_processed ?? 0)}
            sub={`Across ${metrics.data.total_runs ?? 0} runs`}
          />
        </div>
      )}

      <Alert tone="info" className="mb-3" title="Confidence is not accuracy">
        Each proposal carries the model's own confidence. Separately, measured accuracy is the
        share of decided proposals a human accepted unchanged — computed from real outcomes, and
        simply absent until there are enough of them to mean anything.
      </Alert>

      <Tabs
        tabs={[
          { id: 'agents', label: 'Agents', count: agents.data?.agents?.length },
          { id: 'queue', label: 'Review queue', count: proposals.data?.total },
          { id: 'runs', label: 'Run history' },
        ]}
        active={tab}
        onChange={setTab}
        className="mb-3"
      />

      {tab === 'agents' && <AgentGrid agents={agents.data?.agents ?? []} loading={agents.isLoading} />}
      {tab === 'queue' && (
        <ReviewQueue proposals={proposals.data?.items ?? []} loading={proposals.isLoading} />
      )}
      {tab === 'runs' && <RunHistory runs={runs.data?.items ?? []} loading={runs.isLoading} />}
    </div>
  )
}

function MetricCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="card p-3">
      <p className="text-2xs uppercase tracking-wider text-muted">{label}</p>
      <p className="mt-0.5 text-xl font-semibold tabular text-strong">{value}</p>
      {sub && <p className="mt-0.5 line-clamp-2 text-2xs text-subtle">{sub}</p>}
    </div>
  )
}

function AgentGrid({ agents, loading }: { agents: any[]; loading: boolean }) {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const refresh = useRefreshBooks()
  const [running, setRunning] = useState<string | null>(null)

  const run = useMutation({
    mutationFn: (kind: string) => api.post(`/ai/agents/${kind}/run`, { params: {} }),
    onMutate: (kind) => setRunning(kind),
    onSuccess: (result) => {
      push({
        tone: result.status === 'FAILED' ? 'negative' : 'positive',
        title: result.agent_kind.replace(/_/g, ' '),
        description: result.summary,
      })
      queryClient.invalidateQueries({ queryKey: ['agents'] })
      queryClient.invalidateQueries({ queryKey: ['proposals'] })
      refresh()
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Agent failed', description: error?.message }),
    onSettled: () => setRunning(null),
  })

  if (loading) return <Spinner label="Loading agents" />

  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
      {agents.map((agent) => (
        <Card key={agent.kind} bodyClassName="flex flex-col gap-2.5 p-4">
          <div className="flex items-start justify-between gap-2">
            <div className="flex min-w-0 items-start gap-2.5">
              <span
                className={cn(
                  'mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg',
                  agent.last_status === 'NEEDS_REVIEW'
                    ? 'bg-warning-soft text-warning'
                    : agent.last_status === 'FAILED'
                      ? 'bg-negative-soft text-negative'
                      : agent.last_status === 'COMPLETED'
                        ? 'bg-positive-soft text-positive'
                        : 'bg-raised text-subtle',
                )}
              >
                <Bot className="h-3.5 w-3.5" aria-hidden />
              </span>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-strong">{agent.name}</p>
                <p className="text-2xs text-muted">{agent.produces}</p>
              </div>
            </div>
            <StatusBadge status={agent.last_status} />
          </div>

          <p className="line-clamp-2 text-2xs text-muted">{agent.description}</p>

          {agent.last_summary && (
            <p className="line-clamp-3 rounded-lg bg-raised p-2 text-2xs text-muted">
              {agent.last_summary}
            </p>
          )}

          <div className="grid grid-cols-3 gap-2 border-t border-border pt-2.5 text-center">
            <Stat value={agent.items_processed_30d} label="processed" />
            <Stat value={agent.proposals_30d} label="proposals" />
            <Stat
              value={agent.needs_review_30d}
              label="need review"
              tone={agent.needs_review_30d > 0 ? 'warning' : undefined}
            />
          </div>

          <div className="flex items-center justify-between gap-2">
            <span className="text-2xs text-subtle">
              {agent.last_run_at ? formatRelative(agent.last_run_at) : 'Never run'}
              {agent.measured_accuracy != null && (
                <span className="ml-1.5 text-positive">
                  · {(Number(agent.measured_accuracy) * 100).toFixed(0)}% accepted
                </span>
              )}
            </span>
            <Button
              size="sm"
              icon={Play}
              loading={running === agent.kind}
              onClick={() => run.mutate(agent.kind)}
            >
              Run
            </Button>
          </div>
        </Card>
      ))}
    </div>
  )
}

function Stat({ value, label, tone }: { value: number; label: string; tone?: string }) {
  return (
    <div>
      <p
        className={cn(
          'text-sm font-semibold tabular',
          tone === 'warning' ? 'text-warning' : 'text-strong',
        )}
      >
        {value}
      </p>
      <p className="text-2xs text-subtle">{label}</p>
    </div>
  )
}

function ReviewQueue({ proposals, loading }: { proposals: any[]; loading: boolean }) {
  const [selected, setSelected] = useState<any>(null)

  if (loading) return <Spinner label="Loading the review queue" />
  if (!proposals.length) {
    return (
      <Card>
        <EmptyState
          icon={CheckCircle2}
          title="Nothing waiting for review"
          description="Run an agent to generate proposals."
        />
      </Card>
    )
  }

  return (
    <>
      <div className="space-y-2.5">
        {proposals.map((proposal) => (
          <Card key={proposal.id} bodyClassName="p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium text-strong">{proposal.title}</span>
                  <Badge tone="neutral">{proposal.agent_kind.replace(/_/g, ' ')}</Badge>
                  <ConfidenceBadge
                    value={proposal.confidence}
                    label={proposal.confidence_label}
                  />
                  {proposal.severity !== 'INFO' && <SeverityDot severity={proposal.severity} />}
                </div>
                <p className="mt-1 text-xs text-muted">{proposal.summary}</p>
                <p className="mt-1.5 rounded-lg bg-raised px-2 py-1 text-2xs text-muted">
                  <strong className="text-strong">Policy:</strong> {proposal.policy_reason}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {Number(proposal.financial_impact) > 0 && (
                  <Amount
                    value={proposal.financial_impact}
                    compact
                    className="text-sm font-semibold"
                  />
                )}
                <Button size="sm" variant="primary" onClick={() => setSelected(proposal)}>
                  Review
                </Button>
              </div>
            </div>
          </Card>
        ))}
      </div>

      {selected && <ProposalDialog proposal={selected} onClose={() => setSelected(null)} />}
    </>
  )
}

function ProposalDialog({ proposal, onClose }: { proposal: any; onClose: () => void }) {
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()
  const canApprove = usePermission('approve_proposal')
  const [reason, setReason] = useState('')

  const decide = useMutation({
    mutationFn: (action: 'approve' | 'reject') =>
      api.post(`/ai/proposals/${proposal.id}/${action}`, { reason }),
    onSuccess: (_result, action) => {
      push({
        tone: action === 'approve' ? 'positive' : 'info',
        title: action === 'approve' ? 'Proposal approved and applied' : 'Proposal rejected',
        description:
          action === 'approve'
            ? 'It went through the same validation as a manual entry.'
            : 'The rejection is recorded and feeds the accuracy measurement.',
      })
      queryClient.invalidateQueries({ queryKey: ['proposals'] })
      queryClient.invalidateQueries({ queryKey: ['ai-metrics'] })
      refresh()
      onClose()
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Could not apply', description: error?.message }),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title={proposal.title}
      description={`${proposal.agent_kind.replace(/_/g, ' ')} · ${proposal.proposal_type.replace(/_/g, ' ')}`}
      size="lg"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          {canApprove ? (
            <>
              <Button
                variant="danger"
                icon={ThumbsDown}
                loading={decide.isPending}
                onClick={() => decide.mutate('reject')}
              >
                Reject
              </Button>
              <Button
                variant="primary"
                icon={ThumbsUp}
                loading={decide.isPending}
                onClick={() => decide.mutate('approve')}
              >
                Approve and apply
              </Button>
            </>
          ) : (
            <Badge tone="warning">
              Requires {proposal.required_role.replace(/_/g, ' ')} approval
            </Badge>
          )}
        </>
      }
    >
      <div className="space-y-3">
        {proposal.validation_errors?.length > 0 && (
          <Alert tone="negative" title="This proposal has validation warnings">
            <ul className="mt-1 space-y-0.5">
              {proposal.validation_errors.map((issue: string, index: number) => (
                <li key={index}>{issue}</li>
              ))}
            </ul>
          </Alert>
        )}

        <div>
          <p className="label">Reasoning</p>
          <p className="rounded-lg bg-raised p-3 text-sm text-muted">{proposal.reasoning}</p>
        </div>

        <Alert tone="info" title="Approval routing">
          {proposal.policy_reason}
        </Alert>

        {proposal.evidence?.length > 0 && (
          <div>
            <p className="label">Evidence ({proposal.evidence.length})</p>
            <div className="max-h-52 space-y-1 overflow-y-auto rounded-lg border border-border p-1.5">
              {proposal.evidence.map((item: any, index: number) => (
                <div
                  key={index}
                  className="flex items-center gap-2 rounded-lg bg-raised px-2.5 py-1.5 text-2xs"
                >
                  <Badge tone="neutral">{item.type?.replace(/_/g, ' ')}</Badge>
                  <span className="min-w-0 flex-1 truncate text-strong">{item.label}</span>
                  {item.amount && <span className="shrink-0 tabular">{item.amount}</span>}
                  {item.detail && (
                    <span className="shrink-0 text-subtle">{item.detail}</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        <details className="rounded-lg border border-border">
          <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-strong">
            View the full proposal payload
          </summary>
          <pre className="max-h-64 overflow-auto border-t border-border bg-raised p-3 text-2xs">
            {JSON.stringify(proposal.payload, null, 2)}
          </pre>
        </details>

        {canApprove && (
          <div>
            <p className="label">Decision note (optional)</p>
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="Why are you approving or rejecting this? Recorded in the audit trail."
              className="input h-auto min-h-[64px] resize-y py-2"
            />
          </div>
        )}
      </div>
    </Modal>
  )
}

function RunHistory({ runs, loading }: { runs: any[]; loading: boolean }) {
  if (loading) return <Spinner label="Loading run history" />
  if (!runs.length) {
    return (
      <Card>
        <EmptyState icon={Bot} title="No agent runs yet" />
      </Card>
    )
  }

  return (
    <Card bodyClassName="p-0">
      <ul className="divide-y divide-border">
        {runs.map((run) => (
          <li key={run.id} className="flex items-start gap-3 px-4 py-3">
            <StatusBadge status={run.status} />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-strong">
                  {run.agent_kind.replace(/_/g, ' ')}
                </span>
                <Badge tone="neutral">{run.trigger.toLowerCase()}</Badge>
                {run.used_llm ? (
                  <Badge tone="brand">{run.provider}</Badge>
                ) : (
                  <Badge tone="neutral">deterministic</Badge>
                )}
              </div>
              <p className="mt-0.5 line-clamp-2 text-2xs text-muted">
                {run.output_summary?.summary || run.error_message || 'No summary recorded.'}
              </p>
              <p className="mt-1 text-2xs text-subtle tabular">
                {run.items_processed} processed · {run.proposals_created} proposals ·{' '}
                {run.duration_ms}ms · {formatRelative(run.finished_at ?? run.created_at)}
              </p>
              {run.fallback_used && run.fallback_reason && (
                <p className="mt-1 text-2xs text-warning">{run.fallback_reason}</p>
              )}
            </div>
          </li>
        ))}
      </ul>
    </Card>
  )
}
