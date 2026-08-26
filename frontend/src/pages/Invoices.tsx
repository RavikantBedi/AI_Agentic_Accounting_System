/** Sales invoices: list, detail and creation with a live preview. */
import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, FileText, Plus, Search, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { formatDate, formatMoney, sum, toPaise, todayISO } from '@/lib/money'
import {
  useCustomers,
  useItems,
  useListQuery,
  useRefreshBooks,
  useWarehouses,
} from '@/lib/hooks'
import {
  Alert,
  Amount,
  Badge,
  Button,
  Card,
  DataTable,
  EmptyState,
  Field,
  Input,
  Modal,
  PageHeader,
  Pagination,
  Select,
  StatusBadge,
  Textarea,
  Toolbar,
  useToast,
  type Column,
} from '@/components/ui'

const STATUSES = ['', 'DRAFT', 'SENT', 'PARTIALLY_PAID', 'PAID', 'OVERDUE', 'CANCELLED']

export default function Invoices() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [status, setStatus] = useState('')
  const [creating, setCreating] = useState(false)

  const list = useListQuery<any>('invoices', '/sales/invoices', { status: status || undefined })

  if (id) return <InvoiceDetail id={id} onClose={() => navigate('/sales/invoices')} />

  const columns: Column<any>[] = [
    {
      key: 'number',
      header: 'Invoice',
      render: (row) => <span className="font-medium text-strong">{row.number}</span>,
    },
    { key: 'customer', header: 'Customer', render: (row) => row.customer_name },
    {
      key: 'date',
      header: 'Date',
      secondary: true,
      render: (row) => <span className="tabular text-muted">{formatDate(row.doc_date)}</span>,
    },
    {
      key: 'due',
      header: 'Due',
      secondary: true,
      render: (row) => (
        <span className={`tabular ${row.days_overdue > 0 ? 'text-negative' : 'text-muted'}`}>
          {formatDate(row.due_date)}
          {row.days_overdue > 0 && (
            <span className="ml-1 text-2xs">({row.days_overdue}d)</span>
          )}
        </span>
      ),
    },
    {
      key: 'taxable',
      header: 'Taxable',
      align: 'right',
      secondary: true,
      render: (row) => <Amount value={row.taxable_value} />,
    },
    {
      key: 'tax',
      header: 'GST',
      align: 'right',
      secondary: true,
      render: (row) => <Amount value={row.tax_total} className="text-muted" />,
    },
    {
      key: 'total',
      header: 'Total',
      align: 'right',
      render: (row) => <Amount value={row.total} className="font-semibold" />,
    },
    {
      key: 'balance',
      header: 'Due',
      align: 'right',
      render: (row) => (
        <Amount
          value={row.balance_due}
          className={toPaise(row.balance_due) > 0 ? 'font-medium text-negative' : 'text-muted'}
        />
      ),
    },
    { key: 'status', header: 'Status', render: (row) => <StatusBadge status={row.status} /> },
  ]

  return (
    <div>
      <PageHeader
        title="Invoices"
        description="Every invoice posts to the ledger the moment it is created."
        actions={
          <>
            <Button
              icon={Download}
              size="sm"
              onClick={() => api.download('/reports/export/ar-aging.csv', 'ar-aging.csv')}
            >
              Export aging
            </Button>
            <Button variant="primary" size="sm" icon={Plus} onClick={() => setCreating(true)}>
              New invoice
            </Button>
          </>
        }
      >
        <Toolbar>
          <div className="relative min-w-0 flex-1 sm:max-w-xs">
            <Search
              className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-subtle"
              aria-hidden
            />
            <Input
              value={list.search}
              onChange={(event) => list.setSearch(event.target.value)}
              placeholder="Search by number or reference"
              className="pl-8"
              aria-label="Search invoices"
            />
          </div>
          <Select
            value={status}
            onChange={(event) => setStatus(event.target.value)}
            className="w-40"
            aria-label="Filter by status"
          >
            {STATUSES.map((option) => (
              <option key={option} value={option}>
                {option ? option.replace(/_/g, ' ') : 'All statuses'}
              </option>
            ))}
          </Select>
        </Toolbar>
      </PageHeader>

      <Card bodyClassName="p-0">
        <DataTable
          columns={columns}
          rows={list.items}
          loading={list.isLoading}
          rowKey={(row) => row.id}
          onRowClick={(row) => navigate(`/sales/invoices/${row.id}`)}
          empty={
            <EmptyState
              icon={FileText}
              title="No invoices yet"
              description="Create your first invoice, or load the demo company to explore."
              action={
                <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
                  New invoice
                </Button>
              }
            />
          }
        />
        <Pagination
          total={list.total}
          limit={list.limit}
          offset={list.offset}
          onChange={list.setOffset}
        />
      </Card>

      {creating && <InvoiceForm onClose={() => setCreating(false)} />}
    </div>
  )
}

/* ------------------------------------------------------------- creation */

interface LineDraft {
  item_id: string
  description: string
  hsn_sac: string
  quantity: string
  rate: string
  discount_percent: string
  gst_rate: string
}

const blankLine = (): LineDraft => ({
  item_id: '',
  description: '',
  hsn_sac: '',
  quantity: '1',
  rate: '',
  discount_percent: '0',
  gst_rate: '18',
})

function InvoiceForm({ onClose }: { onClose: () => void }) {
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()

  const customers = useCustomers()
  const items = useItems()
  const warehouses = useWarehouses()

  const [customerId, setCustomerId] = useState('')
  const [docDate, setDocDate] = useState(todayISO())
  const [dueDate, setDueDate] = useState('')
  const [reference, setReference] = useState('')
  const [notes, setNotes] = useState('')
  const [lines, setLines] = useState<LineDraft[]>([blankLine()])
  const [error, setError] = useState('')

  const { data: nextNumber } = useQuery({
    queryKey: ['next-invoice-number'],
    queryFn: () => api.get('/sales/invoices/next-number'),
  })

  const customer = (customers.data?.items ?? []).find((c: any) => c.id === customerId)

  // Client-side preview only. The server recomputes every figure on submit —
  // this is for immediate feedback, never the source of truth.
  const preview = useMemo(() => {
    let taxable = '0.00'
    let tax = '0.00'
    for (const line of lines) {
      const quantity = Number(line.quantity || 0)
      const rate = toPaise(line.rate)
      const gross = Math.round(quantity * rate)
      const discount = Math.round((gross * Number(line.discount_percent || 0)) / 100)
      const net = gross - discount
      const lineTax = Math.round((net * Number(line.gst_rate || 0)) / 100)
      taxable = sum(taxable, (net / 100).toFixed(2))
      tax = sum(tax, (lineTax / 100).toFixed(2))
    }
    const total = sum(taxable, tax)
    const rounded = Math.round(toPaise(total) / 100) * 100
    return {
      taxable,
      tax,
      total: (rounded / 100).toFixed(2),
      roundOff: ((rounded - toPaise(total)) / 100).toFixed(2),
      interstate: customer && customer.state_code && customer.state_code !== '27',
    }
  }, [lines, customer])

  const create = useMutation({
    mutationFn: () =>
      api.post('/sales/invoices', {
        customer_id: customerId,
        doc_date: docDate,
        due_date: dueDate || undefined,
        reference,
        notes,
        post: true,
        lines: lines.map((line) => ({
          item_id: line.item_id || undefined,
          description: line.description,
          hsn_sac: line.hsn_sac,
          quantity: line.quantity,
          rate: line.rate || '0',
          discount_percent: line.discount_percent || '0',
          gst_rate: line.gst_rate || undefined,
          warehouse_id: line.item_id ? warehouses.data?.[0]?.id : undefined,
        })),
      }),
    onSuccess: (invoice) => {
      push({
        tone: 'positive',
        title: `Invoice ${invoice.number} posted`,
        description: `${formatMoney(invoice.total)} — journal entry created and the ledger updated.`,
      })
      queryClient.invalidateQueries({ queryKey: ['invoices'] })
      refresh()
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not create the invoice.'),
  })

  const updateLine = (index: number, patch: Partial<LineDraft>) =>
    setLines((current) =>
      current.map((line, position) => (position === index ? { ...line, ...patch } : line)),
    )

  const pickItem = (index: number, itemId: string) => {
    const item = (items.data?.items ?? []).find((candidate: any) => candidate.id === itemId)
    updateLine(index, {
      item_id: itemId,
      description: item?.name ?? '',
      hsn_sac: item?.hsn_sac ?? '',
      rate: item ? String(Number(item.selling_price)) : '',
      gst_rate: item ? String(Number(item.gst_rate)) : '18',
    })
  }

  const valid =
    customerId && lines.some((line) => line.description && Number(line.quantity) > 0 && line.rate)

  return (
    <Modal
      open
      onClose={onClose}
      title="New invoice"
      description={nextNumber ? `Will be numbered ${nextNumber.number}` : undefined}
      size="xl"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!valid}
            onClick={() => {
              setError('')
              create.mutate()
            }}
          >
            Create and post
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {error && <Alert tone="negative">{error}</Alert>}

        <div className="grid gap-3 sm:grid-cols-4">
          <Field label="Customer" required className="sm:col-span-2">
            <Select
              value={customerId}
              onChange={(event) => setCustomerId(event.target.value)}
              required
            >
              <option value="">Select a customer…</option>
              {(customers.data?.items ?? []).map((option: any) => (
                <option key={option.id} value={option.id}>
                  {option.name} {option.gstin ? `· ${option.gstin}` : ''}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Invoice date" required>
            <Input
              type="date"
              value={docDate}
              onChange={(event) => setDocDate(event.target.value)}
            />
          </Field>
          <Field
            label="Due date"
            hint={customer ? `Terms: Net ${customer.payment_terms_days}` : undefined}
          >
            <Input
              type="date"
              value={dueDate}
              onChange={(event) => setDueDate(event.target.value)}
            />
          </Field>
        </div>

        {preview.interstate && (
          <Alert tone="info">
            {customer.name} is registered outside your state, so this invoice will be charged{' '}
            <strong>IGST</strong> rather than CGST + SGST.
          </Alert>
        )}

        {/* Lines */}
        <div className="rounded-lg border border-border">
          <div className="scroll-x">
            <table className="w-full min-w-[720px]">
              <thead>
                <tr className="table-head">
                  <th className="px-2 py-2 text-left">Item / description</th>
                  <th className="w-20 px-2 py-2 text-right">Qty</th>
                  <th className="w-28 px-2 py-2 text-right">Rate</th>
                  <th className="w-20 px-2 py-2 text-right">Disc %</th>
                  <th className="w-20 px-2 py-2 text-right">GST %</th>
                  <th className="w-28 px-2 py-2 text-right">Amount</th>
                  <th className="w-8" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {lines.map((line, index) => {
                  const amount =
                    Number(line.quantity || 0) * (toPaise(line.rate) / 100) *
                    (1 - Number(line.discount_percent || 0) / 100)
                  return (
                    <tr key={index}>
                      <td className="p-1.5">
                        <Select
                          value={line.item_id}
                          onChange={(event) => pickItem(index, event.target.value)}
                          className="mb-1 h-8 text-xs"
                          aria-label={`Line ${index + 1} item`}
                        >
                          <option value="">Custom line…</option>
                          {(items.data?.items ?? []).map((item: any) => (
                            <option key={item.id} value={item.id}>
                              {item.sku} — {item.name}
                            </option>
                          ))}
                        </Select>
                        <Input
                          value={line.description}
                          onChange={(event) =>
                            updateLine(index, { description: event.target.value })
                          }
                          placeholder="Description"
                          className="h-8 text-xs"
                          aria-label={`Line ${index + 1} description`}
                        />
                      </td>
                      <td className="p-1.5">
                        <Input
                          type="number"
                          min="0"
                          step="any"
                          value={line.quantity}
                          onChange={(event) =>
                            updateLine(index, { quantity: event.target.value })
                          }
                          className="h-8 text-right text-xs tabular"
                          aria-label={`Line ${index + 1} quantity`}
                        />
                      </td>
                      <td className="p-1.5">
                        <Input
                          type="number"
                          min="0"
                          step="0.01"
                          value={line.rate}
                          onChange={(event) => updateLine(index, { rate: event.target.value })}
                          className="h-8 text-right text-xs tabular"
                          aria-label={`Line ${index + 1} rate`}
                        />
                      </td>
                      <td className="p-1.5">
                        <Input
                          type="number"
                          min="0"
                          max="100"
                          value={line.discount_percent}
                          onChange={(event) =>
                            updateLine(index, { discount_percent: event.target.value })
                          }
                          className="h-8 text-right text-xs tabular"
                          aria-label={`Line ${index + 1} discount`}
                        />
                      </td>
                      <td className="p-1.5">
                        <Input
                          type="number"
                          min="0"
                          max="28"
                          value={line.gst_rate}
                          onChange={(event) =>
                            updateLine(index, { gst_rate: event.target.value })
                          }
                          className="h-8 text-right text-xs tabular"
                          aria-label={`Line ${index + 1} GST rate`}
                        />
                      </td>
                      <td className="p-1.5 text-right text-xs tabular text-strong">
                        {formatMoney(amount.toFixed(2))}
                      </td>
                      <td className="p-1.5">
                        {lines.length > 1 && (
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
                  )
                })}
              </tbody>
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

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-3">
            <Field label="Reference">
              <Input
                value={reference}
                onChange={(event) => setReference(event.target.value)}
                placeholder="Customer PO number"
              />
            </Field>
            <Field label="Notes">
              <Textarea
                value={notes}
                onChange={(event) => setNotes(event.target.value)}
                placeholder="Visible on the invoice"
              />
            </Field>
          </div>

          <div className="rounded-lg bg-raised p-3">
            <p className="mb-2 text-2xs font-semibold uppercase tracking-wider text-muted">
              Preview
            </p>
            <dl className="space-y-1.5 text-sm">
              <Row label="Taxable value" value={preview.taxable} />
              <Row
                label={preview.interstate ? 'IGST' : 'CGST + SGST'}
                value={preview.tax}
                muted
              />
              {preview.roundOff !== '0.00' && (
                <Row label="Round off" value={preview.roundOff} muted />
              )}
              <div className="border-t border-border pt-1.5">
                <Row label="Total" value={preview.total} bold />
              </div>
            </dl>
            <p className="mt-2 text-2xs text-subtle">
              The server recalculates every figure using dated tax rules; this preview is
              indicative.
            </p>
          </div>
        </div>
      </div>
    </Modal>
  )
}

function Row({
  label,
  value,
  bold,
  muted,
}: {
  label: string
  value: string
  bold?: boolean
  muted?: boolean
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className={muted ? 'text-xs text-muted' : 'text-sm text-muted'}>{label}</dt>
      <dd
        className={`tabular ${bold ? 'text-base font-semibold text-strong' : muted ? 'text-xs text-muted' : 'text-sm text-strong'}`}
      >
        {formatMoney(value)}
      </dd>
    </div>
  )
}

/* --------------------------------------------------------------- detail */

function InvoiceDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const { push } = useToast()
  const refresh = useRefreshBooks()

  const { data: invoice, isLoading } = useQuery({
    queryKey: ['invoice', id],
    queryFn: () => api.get(`/sales/invoices/${id}`),
  })
  const { data: journal } = useQuery({
    queryKey: ['journal', invoice?.journal_entry_id],
    queryFn: () => api.get(`/accounting/journals/${invoice.journal_entry_id}`),
    enabled: Boolean(invoice?.journal_entry_id),
  })

  const cancel = useMutation({
    mutationFn: (reason: string) =>
      api.post(`/sales/invoices/${id}/cancel`, undefined, { reason }),
    onSuccess: () => {
      push({
        tone: 'positive',
        title: 'Invoice cancelled',
        description: 'The journal entry was reversed — nothing was deleted.',
      })
      refresh()
      onClose()
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Could not cancel', description: error?.message }),
  })

  if (isLoading || !invoice) {
    return (
      <Modal open onClose={onClose} title="Invoice" size="lg">
        <div className="space-y-2">
          {Array.from({ length: 6 }).map((_, index) => (
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
      title={`Invoice ${invoice.number}`}
      description={`${invoice.customer_name} · ${formatDate(invoice.doc_date)}`}
      size="lg"
      footer={
        <>
          <StatusBadge status={invoice.status} />
          <span className="flex-1" />
          {invoice.status !== 'CANCELLED' && toPaise(invoice.amount_paid) === 0 && (
            <Button
              variant="danger"
              size="sm"
              loading={cancel.isPending}
              onClick={() => {
                const reason = window.prompt(
                  'Why is this invoice being cancelled? The reason is recorded in the audit trail.',
                )
                if (reason) cancel.mutate(reason)
              }}
            >
              Cancel invoice
            </Button>
          )}
          <Button onClick={onClose}>Close</Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Metric label="Total" value={formatMoney(invoice.total)} />
          <Metric label="Paid" value={formatMoney(invoice.amount_paid)} />
          <Metric
            label="Balance"
            value={formatMoney(invoice.balance_due)}
            tone={toPaise(invoice.balance_due) > 0 ? 'negative' : 'positive'}
          />
          <Metric label="Due date" value={formatDate(invoice.due_date)} />
        </div>

        <div className="rounded-lg border border-border">
          <table className="w-full">
            <thead>
              <tr className="table-head">
                <th className="px-3 py-2 text-left">Description</th>
                <th className="px-3 py-2 text-right">Qty</th>
                <th className="px-3 py-2 text-right">Rate</th>
                <th className="px-3 py-2 text-right">Taxable</th>
                <th className="px-3 py-2 text-right">GST</th>
                <th className="px-3 py-2 text-right">Total</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {invoice.lines.map((line: any) => (
                <tr key={line.id}>
                  <td className="px-3 py-2 text-sm">
                    {line.description}
                    {line.hsn_sac && (
                      <span className="ml-1.5 text-2xs text-subtle">HSN {line.hsn_sac}</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right text-sm tabular">
                    {Number(line.quantity)} {line.uom}
                  </td>
                  <td className="px-3 py-2 text-right text-sm tabular">
                    {formatMoney(line.rate)}
                  </td>
                  <td className="px-3 py-2 text-right text-sm tabular">
                    {formatMoney(line.taxable_value)}
                  </td>
                  <td className="px-3 py-2 text-right text-sm tabular text-muted">
                    {formatMoney(sum(line.cgst, line.sgst, line.igst))}
                    <span className="ml-1 text-2xs">({Number(line.gst_rate)}%)</span>
                  </td>
                  <td className="px-3 py-2 text-right text-sm font-medium tabular">
                    {formatMoney(line.line_total)}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot className="border-t-2 border-border bg-raised">
              <tr>
                <td colSpan={3} className="px-3 py-2 text-right text-xs text-muted">
                  Taxable value
                </td>
                <td className="px-3 py-2 text-right text-sm tabular">
                  {formatMoney(invoice.taxable_value)}
                </td>
                <td className="px-3 py-2 text-right text-sm tabular">
                  {formatMoney(invoice.tax_total)}
                </td>
                <td className="px-3 py-2 text-right text-sm font-semibold tabular">
                  {formatMoney(invoice.total)}
                </td>
              </tr>
            </tfoot>
          </table>
        </div>

        <div className="flex flex-wrap gap-2 text-2xs">
          {toPaise(invoice.cgst) > 0 && (
            <Badge tone="neutral">CGST {formatMoney(invoice.cgst)}</Badge>
          )}
          {toPaise(invoice.sgst) > 0 && (
            <Badge tone="neutral">SGST {formatMoney(invoice.sgst)}</Badge>
          )}
          {toPaise(invoice.igst) > 0 && (
            <Badge tone="neutral">IGST {formatMoney(invoice.igst)}</Badge>
          )}
          {toPaise(invoice.round_off) !== 0 && (
            <Badge tone="neutral">Round off {formatMoney(invoice.round_off)}</Badge>
          )}
          <Badge tone="neutral">{invoice.supply_type?.replace(/_/g, ' ')}</Badge>
        </div>

        {journal && (
          <div>
            <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-muted">
              Journal entry {journal.entry_number}
            </p>
            <div className="rounded-lg border border-border">
              <table className="w-full">
                <thead>
                  <tr className="table-head">
                    <th className="px-3 py-1.5 text-left">Account</th>
                    <th className="px-3 py-1.5 text-right">Debit</th>
                    <th className="px-3 py-1.5 text-right">Credit</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {journal.lines.map((line: any) => (
                    <tr key={line.id}>
                      <td className="px-3 py-1.5 text-xs">
                        <span className="font-mono text-2xs text-subtle">
                          {line.account_code}
                        </span>{' '}
                        {line.account_name}
                      </td>
                      <td className="px-3 py-1.5 text-right text-xs tabular">
                        {toPaise(line.debit) > 0 ? formatMoney(line.debit) : ''}
                      </td>
                      <td className="px-3 py-1.5 text-right text-xs tabular">
                        {toPaise(line.credit) > 0 ? formatMoney(line.credit) : ''}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot className="border-t border-border bg-raised">
                  <tr>
                    <td className="px-3 py-1.5 text-2xs font-semibold text-muted">
                      Balanced
                    </td>
                    <td className="px-3 py-1.5 text-right text-xs font-semibold tabular">
                      {formatMoney(journal.total_debit)}
                    </td>
                    <td className="px-3 py-1.5 text-right text-xs font-semibold tabular">
                      {formatMoney(journal.total_credit)}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </div>
        )}
      </div>
    </Modal>
  )
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: 'positive' | 'negative'
}) {
  return (
    <div className="rounded-lg bg-raised p-2.5">
      <p className="text-2xs uppercase tracking-wider text-muted">{label}</p>
      <p
        className={`mt-0.5 text-sm font-semibold tabular ${
          tone === 'negative' ? 'text-negative' : tone === 'positive' ? 'text-positive' : 'text-strong'
        }`}
      >
        {value}
      </p>
    </div>
  )
}
