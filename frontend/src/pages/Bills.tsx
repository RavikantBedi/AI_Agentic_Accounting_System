/** Vendor bills, with duplicate detection surfaced before anything is booked. */
import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileText, Plus, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { formatDate, formatMoney, sum, toPaise, todayISO } from '@/lib/money'
import { useDebounced, useItems, useListQuery, useRefreshBooks, useVendors, useWarehouses } from '@/lib/hooks'
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

export default function Bills() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [status, setStatus] = useState('')
  const [creating, setCreating] = useState(false)

  const list = useListQuery<any>('bills', '/purchases/bills', { status: status || undefined })

  if (id) return <BillDetail id={id} onClose={() => navigate('/purchases/bills')} />

  const columns: Column<any>[] = [
    {
      key: 'number',
      header: 'Bill',
      render: (row) => (
        <div>
          <div className="font-medium text-strong">{row.number}</div>
          <div className="text-2xs text-subtle">Ref {row.vendor_invoice_number}</div>
        </div>
      ),
    },
    { key: 'vendor', header: 'Vendor', render: (row) => row.vendor_name },
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
          {row.days_overdue > 0 && <span className="ml-1 text-2xs">({row.days_overdue}d)</span>}
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
      key: 'tds',
      header: 'TDS',
      align: 'right',
      secondary: true,
      render: (row) =>
        toPaise(row.tds_amount) > 0 ? (
          <span className="tabular text-warning">
            {formatMoney(row.tds_amount)}
            <span className="ml-1 text-2xs">{row.tds_section}</span>
          </span>
        ) : (
          <span className="text-subtle">—</span>
        ),
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
    <ListPage
      title="Bills"
      description="A vendor's invoice number can only be booked once — the database enforces it."
      actions={
        <Button variant="primary" size="sm" icon={Plus} onClick={() => setCreating(true)}>
          New bill
        </Button>
      }
      columns={columns}
      list={list}
      rowKey={(row) => row.id}
      onRowClick={(row) => navigate(`/purchases/bills/${row.id}`)}
      searchPlaceholder="Search by number or vendor reference"
      filters={[
        {
          id: 'status',
          label: 'Status',
          value: status,
          onChange: setStatus,
          options: statusOptions([
            'DRAFT',
            'APPROVED',
            'PARTIALLY_PAID',
            'PAID',
            'OVERDUE',
            'CANCELLED',
          ]),
        },
      ]}
      emptyIcon={FileText}
      emptyTitle="No bills yet"
      emptyDescription="Record a vendor bill, or upload an invoice for the document agent to read."
      emptyAction={
        <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
          New bill
        </Button>
      }
    >
      {creating && <BillForm onClose={() => setCreating(false)} />}
    </ListPage>
  )
}

interface LineDraft {
  item_id: string
  description: string
  hsn_sac: string
  quantity: string
  rate: string
  gst_rate: string
}

const blankLine = (): LineDraft => ({
  item_id: '',
  description: '',
  hsn_sac: '9983',
  quantity: '1',
  rate: '',
  gst_rate: '18',
})

function BillForm({ onClose }: { onClose: () => void }) {
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()
  const vendors = useVendors()
  const items = useItems()
  const warehouses = useWarehouses()

  const [vendorId, setVendorId] = useState('')
  const [vendorInvoiceNumber, setVendorInvoiceNumber] = useState('')
  const [docDate, setDocDate] = useState(todayISO())
  const [dueDate, setDueDate] = useState('')
  const [notes, setNotes] = useState('')
  const [lines, setLines] = useState<LineDraft[]>([blankLine()])
  const [allowDuplicate, setAllowDuplicate] = useState(false)
  const [error, setError] = useState('')

  const vendor = (vendors.data?.items ?? []).find((candidate: any) => candidate.id === vendorId)

  const totals = useMemo(() => {
    let taxable = '0.00'
    let tax = '0.00'
    for (const line of lines) {
      const net = Math.round(Number(line.quantity || 0) * toPaise(line.rate))
      taxable = sum(taxable, (net / 100).toFixed(2))
      tax = sum(tax, (Math.round((net * Number(line.gst_rate || 0)) / 100) / 100).toFixed(2))
    }
    return { taxable, tax, total: sum(taxable, tax) }
  }, [lines])

  // Live duplicate check, exactly as the AP agent would run it.
  const debouncedRef = useDebounced(vendorInvoiceNumber, 400)
  const { data: duplicateCheck } = useQuery({
    queryKey: ['bill-duplicate', vendorId, debouncedRef, totals.total, docDate],
    queryFn: () =>
      api.post('/purchases/bills/check-duplicate', undefined, {
        vendor_id: vendorId,
        vendor_invoice_number: debouncedRef,
        amount: Number(totals.total),
        doc_date: docDate,
      }),
    enabled: Boolean(vendorId && debouncedRef && Number(totals.total) > 0),
  })

  const create = useMutation({
    mutationFn: () =>
      api.post('/purchases/bills', {
        vendor_id: vendorId,
        vendor_invoice_number: vendorInvoiceNumber,
        doc_date: docDate,
        due_date: dueDate || undefined,
        notes,
        post: true,
        allow_duplicate: allowDuplicate,
        lines: lines.map((line) => ({
          item_id: line.item_id || undefined,
          description: line.description,
          hsn_sac: line.hsn_sac,
          quantity: line.quantity,
          rate: line.rate || '0',
          gst_rate: line.gst_rate || undefined,
          warehouse_id: line.item_id ? warehouses.data?.[0]?.id : undefined,
        })),
      }),
    onSuccess: (bill) => {
      push({
        tone: 'positive',
        title: `Bill ${bill.number} posted`,
        description:
          toPaise(bill.tds_amount) > 0
            ? `${formatMoney(bill.total)} with ${formatMoney(bill.tds_amount)} TDS deducted under ${bill.tds_section}.`
            : formatMoney(bill.total),
      })
      queryClient.invalidateQueries({ queryKey: ['bills'] })
      refresh()
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not record the bill.'),
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
      hsn_sac: item?.hsn_sac ?? '9983',
      rate: item ? String(Number(item.purchase_price)) : '',
      gst_rate: item ? String(Number(item.gst_rate)) : '18',
    })
  }

  const exactDuplicate = duplicateCheck?.is_duplicate
  const similar = duplicateCheck?.similar?.length > 0
  const valid =
    vendorId &&
    vendorInvoiceNumber.trim() &&
    lines.some((line) => line.description && line.rate)

  return (
    <Modal
      open
      onClose={onClose}
      title="New bill"
      description="Totals and TDS are computed by the server from dated rules."
      size="xl"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!valid || exactDuplicate}
            onClick={() => {
              setError('')
              create.mutate()
            }}
          >
            Record and post
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {error && <Alert tone="negative">{error}</Alert>}

        {exactDuplicate && (
          <Alert tone="negative" title="This bill has already been booked">
            {duplicateCheck.reason} Correct the reference, or cancel the original first.
          </Alert>
        )}

        {!exactDuplicate && similar && (
          <Alert tone="warning" title="Possible duplicate">
            {duplicateCheck.reason}
            <label className="mt-1.5 flex items-center gap-2">
              <input
                type="checkbox"
                checked={allowDuplicate}
                onChange={(event) => setAllowDuplicate(event.target.checked)}
                className="h-3.5 w-3.5 rounded border-border"
              />
              <span className="text-2xs">
                I have checked the documents — these are genuinely separate invoices.
              </span>
            </label>
          </Alert>
        )}

        <div className="grid gap-3 sm:grid-cols-4">
          <Field label="Vendor" required className="sm:col-span-2">
            <Select value={vendorId} onChange={(event) => setVendorId(event.target.value)}>
              <option value="">Select a vendor…</option>
              {(vendors.data?.items ?? []).map((option: any) => (
                <option key={option.id} value={option.id}>
                  {option.name}
                  {option.tds_applicable ? ` · TDS ${option.default_tds_section}` : ''}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Vendor invoice no." required hint="Must be unique per vendor">
            <Input
              value={vendorInvoiceNumber}
              onChange={(event) => setVendorInvoiceNumber(event.target.value)}
              placeholder="INV-2024-001"
            />
          </Field>
          <Field label="Bill date" required>
            <Input
              type="date"
              value={docDate}
              onChange={(event) => setDocDate(event.target.value)}
            />
          </Field>
        </div>

        {vendor?.tds_applicable && (
          <Alert tone="info">
            {vendor.name} is marked TDS-applicable under section{' '}
            <strong>{vendor.default_tds_section}</strong>. TDS is computed on the taxable value
            and applied only if the configured threshold is crossed.
          </Alert>
        )}

        <div className="rounded-lg border border-border">
          <div className="scroll-x">
            <table className="w-full min-w-[640px]">
              <thead>
                <tr className="table-head">
                  <th className="px-2 py-2 text-left">Item / description</th>
                  <th className="w-24 px-2 py-2 text-left">HSN/SAC</th>
                  <th className="w-20 px-2 py-2 text-right">Qty</th>
                  <th className="w-28 px-2 py-2 text-right">Rate</th>
                  <th className="w-20 px-2 py-2 text-right">GST %</th>
                  <th className="w-28 px-2 py-2 text-right">Amount</th>
                  <th className="w-8" />
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {lines.map((line, index) => (
                  <tr key={index}>
                    <td className="p-1.5">
                      <Select
                        value={line.item_id}
                        onChange={(event) => pickItem(index, event.target.value)}
                        className="mb-1 h-8 text-xs"
                        aria-label={`Line ${index + 1} item`}
                      >
                        <option value="">Expense / custom line…</option>
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
                        value={line.hsn_sac}
                        onChange={(event) => updateLine(index, { hsn_sac: event.target.value })}
                        className="h-8 text-xs"
                        aria-label={`Line ${index + 1} HSN`}
                      />
                    </td>
                    <td className="p-1.5">
                      <Input
                        type="number"
                        min="0"
                        step="any"
                        value={line.quantity}
                        onChange={(event) => updateLine(index, { quantity: event.target.value })}
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
                        max="28"
                        value={line.gst_rate}
                        onChange={(event) => updateLine(index, { gst_rate: event.target.value })}
                        className="h-8 text-right text-xs tabular"
                        aria-label={`Line ${index + 1} GST rate`}
                      />
                    </td>
                    <td className="p-1.5 text-right text-xs tabular text-strong">
                      {formatMoney(
                        ((Number(line.quantity || 0) * toPaise(line.rate)) / 100).toFixed(2),
                      )}
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
                ))}
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
            <Field label="Due date">
              <Input
                type="date"
                value={dueDate}
                onChange={(event) => setDueDate(event.target.value)}
              />
            </Field>
            <Field label="Notes">
              <Textarea value={notes} onChange={(event) => setNotes(event.target.value)} />
            </Field>
          </div>
          <div className="rounded-lg bg-raised p-3">
            <p className="mb-2 text-2xs font-semibold uppercase tracking-wider text-muted">
              Preview
            </p>
            <dl className="space-y-1.5 text-sm">
              <div className="flex justify-between">
                <dt className="text-muted">Taxable value</dt>
                <dd className="tabular">{formatMoney(totals.taxable)}</dd>
              </div>
              <div className="flex justify-between text-xs text-muted">
                <dt>Input GST</dt>
                <dd className="tabular">{formatMoney(totals.tax)}</dd>
              </div>
              <div className="flex justify-between border-t border-border pt-1.5">
                <dt className="text-sm text-muted">Total</dt>
                <dd className="text-base font-semibold tabular">{formatMoney(totals.total)}</dd>
              </div>
            </dl>
            <p className="mt-2 text-2xs text-subtle">
              TDS, if applicable, is computed by the server and reduces the amount payable.
            </p>
          </div>
        </div>
      </div>
    </Modal>
  )
}

function BillDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const { data: bill, isLoading } = useQuery({
    queryKey: ['bill', id],
    queryFn: () => api.get(`/purchases/bills/${id}`),
  })
  const { data: journal } = useQuery({
    queryKey: ['journal', bill?.journal_entry_id],
    queryFn: () => api.get(`/accounting/journals/${bill.journal_entry_id}`),
    enabled: Boolean(bill?.journal_entry_id),
  })

  if (isLoading || !bill) {
    return (
      <Modal open onClose={onClose} title="Bill" size="lg">
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
      title={`Bill ${bill.number}`}
      description={`${bill.vendor_name} · vendor ref ${bill.vendor_invoice_number}`}
      size="lg"
      footer={
        <>
          <StatusBadge status={bill.status} />
          <span className="flex-1" />
          <Button onClick={onClose}>Close</Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <Metric label="Total" value={formatMoney(bill.total)} />
          <Metric label="TDS deducted" value={formatMoney(bill.tds_amount)} />
          <Metric label="Paid" value={formatMoney(bill.amount_paid)} />
          <Metric
            label="Balance"
            value={formatMoney(bill.balance_due)}
            tone={toPaise(bill.balance_due) > 0 ? 'negative' : 'positive'}
          />
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
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {bill.lines.map((line: any) => (
                <tr key={line.id}>
                  <td className="px-3 py-2 text-sm">{line.description}</td>
                  <td className="px-3 py-2 text-right text-sm tabular">
                    {Number(line.quantity)}
                  </td>
                  <td className="px-3 py-2 text-right text-sm tabular">
                    {formatMoney(line.rate)}
                  </td>
                  <td className="px-3 py-2 text-right text-sm tabular">
                    {formatMoney(line.taxable_value)}
                  </td>
                  <td className="px-3 py-2 text-right text-sm tabular text-muted">
                    {formatMoney(sum(line.cgst, line.sgst, line.igst))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap gap-2">
          <Badge tone={bill.itc_eligible ? 'positive' : 'warning'}>
            {bill.itc_eligible ? 'ITC eligible' : 'ITC not eligible'}
          </Badge>
          {bill.tds_section && <Badge tone="warning">TDS {bill.tds_section}</Badge>}
          {bill.document_id && <Badge tone="info">From uploaded document</Badge>}
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
          tone === 'negative'
            ? 'text-negative'
            : tone === 'positive'
              ? 'text-positive'
              : 'text-strong'
        }`}
      >
        {value}
      </p>
    </div>
  )
}
