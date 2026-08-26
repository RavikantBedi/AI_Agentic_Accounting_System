/** Journal entries — including the reversal flow that replaces editing. */
import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BookOpen, Plus, RotateCcw, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { formatDate, formatDateTime, formatMoney, toPaise, todayISO } from '@/lib/money'
import { useListQuery, usePermission, usePostableAccounts, useRefreshBooks } from '@/lib/hooks'
import { ListPage, statusOptions } from '@/components/ListPage'
import {
  Alert,
  Amount,
  Badge,
  Button,
  Field,
  Input,
  Modal,
  Select,
  StatusBadge,
  Textarea,
  useToast,
  type Column,
} from '@/components/ui'

const VOUCHER_TYPES = [
  'JOURNAL',
  'SALES',
  'PURCHASE',
  'PAYMENT',
  'RECEIPT',
  'CONTRA',
  'CREDIT_NOTE',
  'DEBIT_NOTE',
  'STOCK_JOURNAL',
  'OPENING',
]

export default function Journals() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [status, setStatus] = useState('')
  const [voucherType, setVoucherType] = useState('')
  const [creating, setCreating] = useState(false)

  const list = useListQuery<any>('journals', '/accounting/journals', {
    status: status || undefined,
    voucher_type: voucherType || undefined,
  })

  if (id) return <JournalDetail id={id} onClose={() => navigate('/accounting/journals')} />

  const columns: Column<any>[] = [
    {
      key: 'number',
      header: 'Entry',
      render: (row) => (
        <div className="flex items-center gap-1.5">
          <span className="font-medium text-strong">{row.entry_number}</span>
          {row.is_reversal && <Badge tone="info">Reversal</Badge>}
        </div>
      ),
    },
    {
      key: 'date',
      header: 'Date',
      render: (row) => <span className="tabular text-muted">{formatDate(row.entry_date)}</span>,
    },
    {
      key: 'type',
      header: 'Type',
      secondary: true,
      render: (row) => <Badge tone="neutral">{row.voucher_type?.replace(/_/g, ' ')}</Badge>,
    },
    {
      key: 'narration',
      header: 'Narration',
      render: (row) => (
        <span className="line-clamp-1 text-muted">{row.narration || row.reference || '—'}</span>
      ),
    },
    {
      key: 'source',
      header: 'Source',
      secondary: true,
      render: (row) => (
        <span className="text-2xs uppercase tracking-wide text-subtle">
          {row.source?.replace(/_/g, ' ')}
        </span>
      ),
    },
    {
      key: 'amount',
      header: 'Amount',
      align: 'right',
      render: (row) => <Amount value={row.total_debit} className="font-semibold" />,
    },
    { key: 'status', header: 'Status', render: (row) => <StatusBadge status={row.status} /> },
  ]

  return (
    <ListPage
      title="Journal entries"
      description="Posted entries are immutable. Corrections are made by reversal, and both entries stay visible."
      actions={
        <Button variant="primary" size="sm" icon={Plus} onClick={() => setCreating(true)}>
          New entry
        </Button>
      }
      columns={columns}
      list={list}
      rowKey={(row) => row.id}
      onRowClick={(row) => navigate(`/accounting/journals/${row.id}`)}
      searchPlaceholder="Search by number, narration or reference"
      filters={[
        {
          id: 'type',
          label: 'Voucher type',
          value: voucherType,
          onChange: setVoucherType,
          options: [
            { value: '', label: 'All voucher types' },
            ...VOUCHER_TYPES.map((type) => ({ value: type, label: type.replace(/_/g, ' ') })),
          ],
        },
        {
          id: 'status',
          label: 'Status',
          value: status,
          onChange: setStatus,
          options: statusOptions(['DRAFT', 'POSTED', 'REVERSED', 'VOID']),
        },
      ]}
      emptyIcon={BookOpen}
      emptyTitle="No journal entries"
      emptyDescription="Entries appear here as invoices, bills and payments are posted."
    >
      {creating && <JournalForm onClose={() => setCreating(false)} />}
    </ListPage>
  )
}

interface LineDraft {
  account_id: string
  debit: string
  credit: string
  description: string
}

const blankLine = (): LineDraft => ({ account_id: '', debit: '', credit: '', description: '' })

function JournalForm({ onClose }: { onClose: () => void }) {
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()
  const accounts = usePostableAccounts()

  const [entryDate, setEntryDate] = useState(todayISO())
  const [narration, setNarration] = useState('')
  const [reference, setReference] = useState('')
  const [voucherType, setVoucherType] = useState('JOURNAL')
  const [lines, setLines] = useState<LineDraft[]>([blankLine(), blankLine()])
  const [error, setError] = useState('')

  const totals = useMemo(() => {
    const debit = lines.reduce((total, line) => total + toPaise(line.debit), 0)
    const credit = lines.reduce((total, line) => total + toPaise(line.credit), 0)
    return { debit, credit, difference: debit - credit }
  }, [lines])

  const balanced = totals.difference === 0 && totals.debit > 0

  const create = useMutation({
    mutationFn: () =>
      api.post('/accounting/journals', {
        entry_date: entryDate,
        voucher_type: voucherType,
        narration,
        reference,
        post: true,
        lines: lines
          .filter((line) => line.account_id && (line.debit || line.credit))
          .map((line) => ({
            account_id: line.account_id,
            debit: line.debit || '0',
            credit: line.credit || '0',
            description: line.description,
          })),
      }),
    onSuccess: (entry) => {
      push({
        tone: 'positive',
        title: `Entry ${entry.entry_number} posted`,
        description: `${formatMoney(entry.total_debit)} — debits equal credits.`,
      })
      queryClient.invalidateQueries({ queryKey: ['journals'] })
      refresh()
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not post the entry.'),
  })

  const updateLine = (index: number, patch: Partial<LineDraft>) =>
    setLines((current) =>
      current.map((line, position) => (position === index ? { ...line, ...patch } : line)),
    )

  return (
    <Modal
      open
      onClose={onClose}
      title="New journal entry"
      description="The engine rejects anything that does not balance — this preview shows the difference live."
      size="xl"
      footer={
        <>
          <div
            className={`mr-auto text-xs tabular ${balanced ? 'text-positive' : 'text-negative'}`}
          >
            {balanced
              ? `Balanced at ${formatMoney((totals.debit / 100).toFixed(2))}`
              : `Out of balance by ${formatMoney((Math.abs(totals.difference) / 100).toFixed(2))}`}
          </div>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!balanced}
            onClick={() => {
              setError('')
              create.mutate()
            }}
          >
            Post entry
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}

        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Date" required>
            <Input
              type="date"
              value={entryDate}
              onChange={(event) => setEntryDate(event.target.value)}
            />
          </Field>
          <Field label="Voucher type">
            <Select value={voucherType} onChange={(event) => setVoucherType(event.target.value)}>
              {VOUCHER_TYPES.map((type) => (
                <option key={type} value={type}>
                  {type.replace(/_/g, ' ')}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Reference">
            <Input value={reference} onChange={(event) => setReference(event.target.value)} />
          </Field>
        </div>

        <Field label="Narration" required>
          <Textarea
            value={narration}
            onChange={(event) => setNarration(event.target.value)}
            placeholder="What is this entry for?"
            className="min-h-[56px]"
          />
        </Field>

        <div className="rounded-lg border border-border">
          <div className="scroll-x">
            <table className="w-full min-w-[600px]">
              <thead>
                <tr className="table-head">
                  <th className="px-2 py-2 text-left">Account</th>
                  <th className="px-2 py-2 text-left">Description</th>
                  <th className="w-32 px-2 py-2 text-right">Debit</th>
                  <th className="w-32 px-2 py-2 text-right">Credit</th>
                  <th className="w-8" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {lines.map((line, index) => (
                  <tr key={index}>
                    <td className="p-1.5">
                      <Select
                        value={line.account_id}
                        onChange={(event) =>
                          updateLine(index, { account_id: event.target.value })
                        }
                        className="h-8 text-xs"
                        aria-label={`Line ${index + 1} account`}
                      >
                        <option value="">Select an account…</option>
                        {(accounts.data ?? []).map((account: any) => (
                          <option key={account.id} value={account.id}>
                            {account.code} — {account.name}
                          </option>
                        ))}
                      </Select>
                    </td>
                    <td className="p-1.5">
                      <Input
                        value={line.description}
                        onChange={(event) =>
                          updateLine(index, { description: event.target.value })
                        }
                        className="h-8 text-xs"
                        aria-label={`Line ${index + 1} description`}
                      />
                    </td>
                    <td className="p-1.5">
                      <Input
                        type="number"
                        step="0.01"
                        min="0"
                        value={line.debit}
                        onChange={(event) =>
                          updateLine(index, { debit: event.target.value, credit: '' })
                        }
                        className="h-8 text-right text-xs tabular"
                        aria-label={`Line ${index + 1} debit`}
                      />
                    </td>
                    <td className="p-1.5">
                      <Input
                        type="number"
                        step="0.01"
                        min="0"
                        value={line.credit}
                        onChange={(event) =>
                          updateLine(index, { credit: event.target.value, debit: '' })
                        }
                        className="h-8 text-right text-xs tabular"
                        aria-label={`Line ${index + 1} credit`}
                      />
                    </td>
                    <td className="p-1.5">
                      {lines.length > 2 && (
                        <button
                          onClick={() =>
                            setLines((current) =>
                              current.filter((_, position) => position !== index),
                            )
                          }
                          className="btn-ghost h-7 w-7 p-0"
                          aria-label={`Remove line ${index + 1}`}
                        >
                          <Trash2 className="h-3.5 w-3.5" aria-hidden />
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot className="border-t-2 border-border bg-raised">
                <tr>
                  <td colSpan={2} className="px-2 py-2 text-2xs font-semibold text-muted">
                    Totals
                  </td>
                  <td className="px-2 py-2 text-right text-sm font-semibold tabular">
                    {formatMoney((totals.debit / 100).toFixed(2))}
                  </td>
                  <td className="px-2 py-2 text-right text-sm font-semibold tabular">
                    {formatMoney((totals.credit / 100).toFixed(2))}
                  </td>
                  <td />
                </tr>
              </tfoot>
            </table>
          </div>
          <div className="border-t border-border p-1.5">
            <Button
              size="sm"
              icon={Plus}
              onClick={() => setLines((current) => [...current, blankLine()])}
            >
              Add line
            </Button>
          </div>
        </div>
      </div>
    </Modal>
  )
}

function JournalDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const canPost = usePermission('post_journal')

  const { data: entry, isLoading } = useQuery({
    queryKey: ['journal', id],
    queryFn: () => api.get(`/accounting/journals/${id}`),
  })
  const { data: timeline } = useQuery({
    queryKey: ['audit-timeline', 'JournalEntry', id],
    queryFn: () => api.get(`/audit/timeline/JournalEntry/${id}`),
  })

  const reverse = useMutation({
    mutationFn: (reason: string) =>
      api.post(`/accounting/journals/${id}/reverse`, { reason }),
    onSuccess: (reversal) => {
      push({
        tone: 'positive',
        title: `Reversed as ${reversal.entry_number}`,
        description: 'The original entry is retained and both remain in the ledger.',
      })
      refresh()
      onClose()
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Could not reverse', description: error?.message }),
  })

  if (isLoading || !entry) {
    return (
      <Modal open onClose={onClose} title="Journal entry" size="lg">
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, index) => (
            <div key={index} className="skeleton h-8" />
          ))}
        </div>
      </Modal>
    )
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={entry.entry_number}
      description={`${entry.voucher_type.replace(/_/g, ' ')} · ${formatDate(entry.entry_date)}`}
      size="lg"
      footer={
        <>
          <StatusBadge status={entry.status} />
          <span className="flex-1" />
          {canPost && entry.status === 'POSTED' && !entry.reversed_by_entry_id && (
            <Button
              variant="danger"
              size="sm"
              icon={RotateCcw}
              loading={reverse.isPending}
              onClick={() => {
                const reason = window.prompt(
                  'Why is this entry being reversed? The reason becomes part of the audit trail.',
                )
                if (reason) reverse.mutate(reason)
              }}
            >
              Reverse entry
            </Button>
          )}
          <Button onClick={onClose}>Close</Button>
        </>
      }
    >
      <div className="space-y-4">
        {entry.narration && <p className="text-sm text-muted">{entry.narration}</p>}

        {entry.reversed_by_entry_id && (
          <Alert tone="info" title="This entry has been reversed">
            A mirror entry cancels it out. Both remain in the ledger — nothing was deleted.
          </Alert>
        )}
        {entry.is_reversal && (
          <Alert tone="info" title="This is a reversal entry">
            It reverses an earlier entry as a correction.
          </Alert>
        )}

        <div className="rounded-lg border border-border">
          <table className="w-full">
            <thead>
              <tr className="table-head">
                <th className="px-3 py-2 text-left">Account</th>
                <th className="px-3 py-2 text-left">Description</th>
                <th className="px-3 py-2 text-right">Debit</th>
                <th className="px-3 py-2 text-right">Credit</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {entry.lines.map((line: any) => (
                <tr key={line.id}>
                  <td className="px-3 py-2 text-sm">
                    <span className="font-mono text-2xs text-subtle">{line.account_code}</span>{' '}
                    {line.account_name}
                  </td>
                  <td className="px-3 py-2 text-xs text-muted">{line.description}</td>
                  <td className="px-3 py-2 text-right text-sm tabular">
                    {toPaise(line.debit) > 0 ? formatMoney(line.debit) : ''}
                  </td>
                  <td className="px-3 py-2 text-right text-sm tabular">
                    {toPaise(line.credit) > 0 ? formatMoney(line.credit) : ''}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot className="border-t-2 border-border bg-raised">
              <tr>
                <td colSpan={2} className="px-3 py-2 text-2xs font-semibold text-muted">
                  Totals — equal by construction
                </td>
                <td className="px-3 py-2 text-right text-sm font-semibold tabular">
                  {formatMoney(entry.total_debit)}
                </td>
                <td className="px-3 py-2 text-right text-sm font-semibold tabular">
                  {formatMoney(entry.total_credit)}
                </td>
              </tr>
            </tfoot>
          </table>
        </div>

        {timeline && timeline.length > 0 && (
          <div>
            <p className="mb-2 text-2xs font-semibold uppercase tracking-wider text-muted">
              Audit trail
            </p>
            <ol className="space-y-2 border-l border-border pl-3">
              {timeline.map((event: any) => (
                <li key={event.id} className="relative">
                  <span
                    className="absolute -left-[17px] top-1.5 h-2 w-2 rounded-full bg-brand"
                    aria-hidden
                  />
                  <p className="text-xs text-strong">{event.summary}</p>
                  <p className="text-2xs text-subtle">
                    {formatDateTime(event.occurred_at)} · {event.actor_label || event.actor_type}
                  </p>
                  {event.reason && (
                    <p className="mt-0.5 text-2xs italic text-muted">"{event.reason}"</p>
                  )}
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>
    </Modal>
  )
}
