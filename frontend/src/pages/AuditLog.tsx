/** Audit log — a tamper-evident timeline of who changed what, and why. */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Bot, ShieldAlert, User, Cog } from 'lucide-react'
import { api } from '@/lib/api'
import { formatDateTime } from '@/lib/money'
import { useListQuery } from '@/lib/hooks'
import {
  Alert,
  Amount,
  Badge,
  Button,
  Card,
  EmptyState,
  Modal,
  PageHeader,
  Pagination,
  Select,
  Spinner,
  Toolbar,
  cn,
} from '@/components/ui'

const ACTIONS = [
  '',
  'CREATE',
  'UPDATE',
  'POST',
  'REVERSE',
  'APPROVE',
  'REJECT',
  'DELETE',
  'LOGIN',
  'LOGIN_FAILED',
  'CLOSE_PERIOD',
  'REOPEN_PERIOD',
  'AI_RUN',
  'POLICY_CHANGE',
  'PERMISSION_DENIED',
  'IMPORT',
]

const ACTOR_ICON: Record<string, any> = {
  USER: User,
  AI_AGENT: Bot,
  SYSTEM: Cog,
}

export default function AuditLog() {
  const [action, setAction] = useState('')
  const [actorType, setActorType] = useState('')
  const [selected, setSelected] = useState<any>(null)

  const list = useListQuery<any>('audit', '/audit', {
    action: action || undefined,
    actor_type: actorType || undefined,
  })

  const verification = useQuery({
    queryKey: ['audit-verify'],
    queryFn: () => api.get('/audit/verify'),
  })

  return (
    <div>
      <PageHeader
        title="Audit log"
        description="Append-only, hash-chained, and never edited. Every event links to the one before it."
      >
        <Toolbar>
          <div className="min-w-0 flex-1 sm:max-w-xs">
            <input
              value={list.search}
              onChange={(event) => list.setSearch(event.target.value)}
              placeholder="Search summaries, records and actors"
              className="input"
              aria-label="Search the audit log"
            />
          </div>
          <Select
            value={action}
            onChange={(event) => setAction(event.target.value)}
            className="w-44"
            aria-label="Filter by action"
          >
            {ACTIONS.map((option) => (
              <option key={option} value={option}>
                {option ? option.replace(/_/g, ' ') : 'All actions'}
              </option>
            ))}
          </Select>
          <Select
            value={actorType}
            onChange={(event) => setActorType(event.target.value)}
            className="w-36"
            aria-label="Filter by actor"
          >
            <option value="">All actors</option>
            <option value="USER">People</option>
            <option value="AI_AGENT">AI agents</option>
            <option value="SYSTEM">System</option>
          </Select>
        </Toolbar>
      </PageHeader>

      {verification.data && (
        <Alert
          tone={verification.data.valid ? 'positive' : 'negative'}
          className="mb-3"
          title={
            verification.data.valid
              ? 'Audit chain intact'
              : 'Audit chain integrity check FAILED'
          }
        >
          {verification.data.message}
          {verification.data.valid && (
            <p className="mt-1">
              Each event stores a hash of itself plus the previous event's hash. Altering or
              deleting history would break the chain, and this check would say so.
            </p>
          )}
        </Alert>
      )}

      <Card bodyClassName="p-0">
        {list.isLoading ? (
          <Spinner label="Loading the audit trail" />
        ) : !list.items.length ? (
          <EmptyState
            icon={ShieldAlert}
            title="No audit events"
            description="Events appear here as soon as anything is created, posted or approved."
          />
        ) : (
          <>
            <ol className="divide-y divide-border">
              {list.items.map((event: any) => {
                const Icon = ACTOR_ICON[event.actor_type] ?? Cog
                return (
                  <li key={event.id}>
                    <button
                      onClick={() => setSelected(event)}
                      className="flex w-full items-start gap-3 px-4 py-3 text-left transition-colors hover:bg-raised"
                    >
                      <span
                        className={cn(
                          'mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg',
                          event.actor_type === 'AI_AGENT'
                            ? 'bg-brand-soft text-brand'
                            : event.action === 'PERMISSION_DENIED' || event.action === 'LOGIN_FAILED'
                              ? 'bg-negative-soft text-negative'
                              : 'bg-raised text-muted',
                        )}
                      >
                        <Icon className="h-3.5 w-3.5" aria-hidden />
                      </span>

                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge tone="neutral">{event.action.replace(/_/g, ' ')}</Badge>
                          <span className="text-2xs text-subtle">
                            #{event.sequence} · {event.entity_type}
                          </span>
                          {event.agent_kind && (
                            <Badge tone="brand">{event.agent_kind.replace(/_/g, ' ')}</Badge>
                          )}
                        </div>
                        <p className="mt-1 text-sm text-strong">{event.summary}</p>
                        {event.reason && (
                          <p className="mt-0.5 text-2xs italic text-muted">"{event.reason}"</p>
                        )}
                        <p className="mt-1 text-2xs text-subtle">
                          {formatDateTime(event.occurred_at)} ·{' '}
                          {event.actor_label || event.actor_type.toLowerCase()}
                          {event.ip_address && ` · ${event.ip_address}`}
                        </p>
                      </div>

                      {event.amount && (
                        <Amount
                          value={event.amount}
                          compact
                          className="shrink-0 text-sm font-medium"
                        />
                      )}
                    </button>
                  </li>
                )
              })}
            </ol>
            <Pagination
              total={list.total}
              limit={list.limit}
              offset={list.offset}
              onChange={list.setOffset}
            />
          </>
        )}
      </Card>

      {selected && <EventDialog event={selected} onClose={() => setSelected(null)} />}
    </div>
  )
}

function EventDialog({ event, onClose }: { event: any; onClose: () => void }) {
  const timeline = useQuery({
    queryKey: ['audit-timeline', event.entity_type, event.entity_id],
    queryFn: () => api.get(`/audit/timeline/${event.entity_type}/${event.entity_id}`),
    enabled: Boolean(event.entity_id),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title={event.summary}
      description={`${event.action.replace(/_/g, ' ')} · ${formatDateTime(event.occurred_at)}`}
      size="lg"
      footer={<Button onClick={onClose}>Close</Button>}
    >
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-2 text-2xs">
          <KeyValue label="Sequence" value={`#${event.sequence}`} />
          <KeyValue label="Actor" value={event.actor_label || event.actor_type} />
          <KeyValue label="Record" value={`${event.entity_type} ${event.entity_label}`} />
          <KeyValue label="IP address" value={event.ip_address || '—'} />
          <KeyValue label="Request id" value={event.request_id || '—'} mono />
          <KeyValue label="Hash" value={event.hash?.slice(0, 20) + '…'} mono />
        </div>

        {event.reason && (
          <div>
            <p className="label">Reason given</p>
            <p className="rounded-lg bg-raised p-2.5 text-sm italic text-muted">
              "{event.reason}"
            </p>
          </div>
        )}

        {event.changed_fields?.length > 0 && (
          <div>
            <p className="label">Fields changed</p>
            <div className="flex flex-wrap gap-1.5">
              {event.changed_fields.map((field: string) => (
                <Badge key={field} tone="warning">
                  {field.replace(/_/g, ' ')}
                </Badge>
              ))}
            </div>
          </div>
        )}

        {(event.before || event.after) && (
          <div className="grid gap-2 sm:grid-cols-2">
            {event.before && (
              <div>
                <p className="label">Before</p>
                <pre className="max-h-48 overflow-auto rounded-lg bg-raised p-2.5 text-2xs">
                  {JSON.stringify(event.before, null, 2)}
                </pre>
              </div>
            )}
            {event.after && (
              <div>
                <p className="label">After</p>
                <pre className="max-h-48 overflow-auto rounded-lg bg-raised p-2.5 text-2xs">
                  {JSON.stringify(event.after, null, 2)}
                </pre>
              </div>
            )}
          </div>
        )}

        {timeline.data && timeline.data.length > 1 && (
          <div>
            <p className="label">Full history of this record</p>
            <ol className="space-y-2 border-l border-border pl-3">
              {timeline.data.map((item: any) => (
                <li key={item.id} className="relative">
                  <span
                    className={cn(
                      'absolute -left-[17px] top-1.5 h-2 w-2 rounded-full',
                      item.id === event.id ? 'bg-brand' : 'bg-border',
                    )}
                    aria-hidden
                  />
                  <p className="text-xs text-strong">{item.summary}</p>
                  <p className="text-2xs text-subtle">
                    {formatDateTime(item.occurred_at)} ·{' '}
                    {item.actor_label || item.actor_type.toLowerCase()}
                  </p>
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>
    </Modal>
  )
}

function KeyValue({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="rounded-lg bg-raised p-2">
      <p className="uppercase tracking-wider text-muted">{label}</p>
      <p className={cn('mt-0.5 truncate text-strong', mono && 'font-mono')}>{value}</p>
    </div>
  )
}
