/** Reconciliation — bank on the left, books on the right, evidence in between. */
import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Play, Scale, X } from 'lucide-react'
import { api } from '@/lib/api'
import { formatDate, formatMoney, toPaise } from '@/lib/money'
import { useBankAccounts, useRefreshBooks } from '@/lib/hooks'
import {
  Alert,
  Badge,
  Button,
  Card,
  ConfidenceBadge,
  EmptyState,
  ErrorState,
  Field,
  PageHeader,
  Select,
  Spinner,
  StatusBadge,
  Toolbar,
  cn,
  useToast,
} from '@/components/ui'

export default function Reconciliation() {
  const [params, setParams] = useSearchParams()
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()

  const banks = useBankAccounts()
  const [accountId, setAccountId] = useState(params.get('account') ?? '')
  const [status, setStatus] = useState('')

  useEffect(() => {
    if (!accountId && banks.data?.length) setAccountId(banks.data[0].id)
  }, [banks.data, accountId])

  const view = useQuery({
    queryKey: ['reconciliation', accountId, status],
    queryFn: () =>
      api.get(`/banking/reconciliation/${accountId}`, {
        status: status || undefined,
        limit: 50,
      }),
    enabled: Boolean(accountId),
  })

  const run = useMutation({
    mutationFn: () => api.post('/banking/reconcile', { bank_account_id: accountId }),
    onSuccess: (result) => {
      const session = result.sessions[0]
      push({
        tone: 'positive',
        title: 'Reconciliation complete',
        description: `${session.matched} matched automatically, ${session.for_review + session.ambiguous} need review, ${session.unmatched} unmatched.`,
      })
      queryClient.invalidateQueries({ queryKey: ['reconciliation'] })
      refresh()
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Reconciliation failed', description: error?.message }),
  })

  const confirm = useMutation({
    mutationFn: (matchId: string) => api.post(`/banking/matches/${matchId}/confirm`),
    onSuccess: () => {
      push({ tone: 'positive', title: 'Match confirmed' })
      queryClient.invalidateQueries({ queryKey: ['reconciliation'] })
      refresh()
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Could not confirm', description: error?.message }),
  })

  const reject = useMutation({
    mutationFn: ({ matchId, reason }: { matchId: string; reason: string }) =>
      api.post(`/banking/matches/${matchId}/reject`, { reason }),
    onSuccess: () => {
      push({ tone: 'info', title: 'Match rejected', description: 'The feedback improves future matching.' })
      queryClient.invalidateQueries({ queryKey: ['reconciliation'] })
    },
  })

  if (view.isError) return <ErrorState error={view.error} retry={() => view.refetch()} />

  const account = view.data?.bank_account
  const hasDifference = account?.difference != null && toPaise(account.difference) !== 0

  return (
    <div>
      <PageHeader
        title="Bank reconciliation"
        description="Eight matching strategies run in order. Anything the engine cannot separate confidently is left for you."
        actions={
          <Button
            variant="primary"
            size="sm"
            icon={Play}
            loading={run.isPending}
            disabled={!accountId}
            onClick={() => run.mutate()}
          >
            Run reconciliation
          </Button>
        }
      >
        <Toolbar>
          <Field label="Bank account" className="min-w-0 flex-1 sm:max-w-sm">
            <Select
              value={accountId}
              onChange={(event) => {
                setAccountId(event.target.value)
                setParams({ account: event.target.value })
              }}
            >
              {(banks.data ?? []).map((bank: any) => (
                <option key={bank.id} value={bank.id}>
                  {bank.name} · {bank.masked_number}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Show" className="w-44">
            <Select value={status} onChange={(event) => setStatus(event.target.value)}>
              <option value="">Needs attention</option>
              <option value="UNMATCHED">Unmatched</option>
              <option value="PARTIAL">For review</option>
              <option value="AMBIGUOUS">Ambiguous</option>
              <option value="MATCHED">Matched</option>
            </Select>
          </Field>
        </Toolbar>
      </PageHeader>

      {account && (
        <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="Balance per books" value={formatMoney(account.book_balance)} />
          <Stat
            label="Balance per statement"
            value={
              account.statement_balance != null
                ? formatMoney(account.statement_balance)
                : 'Not imported'
            }
          />
          <Stat
            label="Difference"
            value={account.difference != null ? formatMoney(account.difference) : '—'}
            tone={hasDifference ? 'negative' : 'positive'}
          />
          <Stat label="Items to review" value={String(view.data?.total ?? 0)} />
        </div>
      )}

      {hasDifference && (
        <Alert tone="warning" className="mb-3" title="The books do not agree with the statement">
          A difference of {formatMoney(account.difference)} remains. It is usually explained by
          transactions the bank has recorded that are not yet in the books (or the reverse).
        </Alert>
      )}

      {view.isLoading ? (
        <Spinner label="Loading reconciliation" />
      ) : !view.data?.items?.length ? (
        <Card>
          <EmptyState
            icon={Scale}
            title="Nothing to reconcile"
            description="Every bank transaction in this view is matched."
          />
        </Card>
      ) : (
        <div className="space-y-2.5">
          {view.data.items.map((item: any) => (
            <ReconciliationRow
              key={item.transaction.id}
              item={item}
              onConfirm={(matchId) => confirm.mutate(matchId)}
              onReject={(matchId) => {
                const reason = window.prompt('Why is this match wrong?')
                if (reason) reject.mutate({ matchId, reason })
              }}
              busy={confirm.isPending || reject.isPending}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="card p-2.5">
      <p className="text-2xs uppercase tracking-wider text-muted">{label}</p>
      <p
        className={cn(
          'mt-0.5 text-sm font-semibold tabular',
          tone === 'negative' ? 'text-negative' : tone === 'positive' ? 'text-positive' : 'text-strong',
        )}
      >
        {value}
      </p>
    </div>
  )
}

function ReconciliationRow({
  item,
  onConfirm,
  onReject,
  busy,
}: {
  item: any
  onConfirm: (matchId: string) => void
  onReject: (matchId: string) => void
  busy: boolean
}) {
  const { transaction, proposals } = item
  const best = proposals[0]

  return (
    <Card bodyClassName="p-0">
      <div className="grid gap-0 lg:grid-cols-2">
        {/* Bank side */}
        <div className="border-b border-border p-3.5 lg:border-b-0 lg:border-r">
          <div className="mb-2 flex items-center gap-2">
            <Badge tone="neutral">Bank</Badge>
            <StatusBadge status={transaction.status} />
          </div>
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="line-clamp-2 text-sm text-strong">{transaction.description}</p>
              <p className="mt-0.5 text-2xs text-muted">
                {formatDate(transaction.date)}
                {transaction.reference && ` · ${transaction.reference}`}
              </p>
            </div>
            <span
              className={cn(
                'shrink-0 text-base font-semibold tabular',
                transaction.direction === 'CREDIT' ? 'text-positive' : 'text-strong',
              )}
            >
              {transaction.direction === 'CREDIT' ? '+' : '−'}
              {formatMoney(transaction.amount, { symbol: false })}
            </span>
          </div>
        </div>

        {/* Books side */}
        <div className="p-3.5">
          {!best ? (
            <div className="flex h-full flex-col justify-center">
              <Badge tone="warning" className="mb-1.5 self-start">
                No match found
              </Badge>
              <p className="text-xs text-muted">
                No ledger entry corresponds to this bank line. It may be a bank charge, interest,
                or a transaction that has not been recorded yet.
              </p>
            </div>
          ) : (
            <>
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <Badge tone="brand">Books</Badge>
                <ConfidenceBadge value={best.confidence} />
                <Badge tone="neutral">{best.strategy?.replace(/_/g, ' ').toLowerCase()}</Badge>
                {best.proposed_by === 'AI' && <Badge tone="info">AI proposed</Badge>}
              </div>

              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-strong">
                    {best.evidence?.entry_number ?? 'Journal entry'}
                  </p>
                  <p className="mt-0.5 line-clamp-2 text-2xs text-muted">{best.reason}</p>
                </div>
                <span className="shrink-0 text-sm font-semibold tabular text-strong">
                  {formatMoney(best.amount)}
                </span>
              </div>

              {/* Evidence */}
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                <EvidenceChip
                  label="Amount"
                  ok={best.evidence?.amount?.matched === true}
                  detail={
                    best.evidence?.amount?.difference &&
                    toPaise(best.evidence.amount.difference) !== 0
                      ? `off by ${formatMoney(best.evidence.amount.difference)}`
                      : undefined
                  }
                />
                <EvidenceChip
                  label="Date"
                  ok={best.evidence?.date?.days_apart === 0}
                  detail={
                    best.evidence?.date?.days_apart
                      ? `${best.evidence.date.days_apart}d apart`
                      : undefined
                  }
                />
                {best.evidence?.reference && (
                  <EvidenceChip
                    label="Reference"
                    ok={best.evidence.reference.matched === true}
                  />
                )}
                {best.evidence?.counterparty && (
                  <EvidenceChip
                    label="Counterparty"
                    ok={Number(best.evidence.counterparty.similarity ?? 0) >= 82}
                    detail={`${Math.round(best.evidence.counterparty.similarity ?? 0)}%`}
                  />
                )}
              </div>

              <div className="mt-3 flex gap-2">
                <Button
                  size="sm"
                  variant="primary"
                  icon={Check}
                  disabled={busy}
                  onClick={() => onConfirm(best.id)}
                >
                  Confirm match
                </Button>
                <Button size="sm" icon={X} disabled={busy} onClick={() => onReject(best.id)}>
                  Not a match
                </Button>
                {proposals.length > 1 && (
                  <span className="self-center text-2xs text-muted">
                    +{proposals.length - 1} other candidate
                    {proposals.length > 2 ? 's' : ''}
                  </span>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </Card>
  )
}

function EvidenceChip({
  label,
  ok,
  detail,
}: {
  label: string
  ok: boolean
  detail?: string
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-2xs font-medium',
        ok ? 'bg-positive-soft text-positive' : 'bg-warning-soft text-warning',
      )}
    >
      {ok ? <Check className="h-3 w-3" aria-hidden /> : <X className="h-3 w-3" aria-hidden />}
      {label}
      {detail && <span className="opacity-80">· {detail}</span>}
    </span>
  )
}
