/** Customer receipts, allocated against open invoices. */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Wallet } from 'lucide-react'
import { api } from '@/lib/api'
import { formatDate, formatMoney, toPaise, todayISO } from '@/lib/money'
import { useBankAccounts, useCustomers, useListQuery, useRefreshBooks } from '@/lib/hooks'
import { ListPage } from '@/components/ListPage'
import {
  Alert,
  Amount,
  Button,
  Field,
  Input,
  Modal,
  Select,
  StatusBadge,
  useToast,
  type Column,
} from '@/components/ui'

export default function Receipts() {
  const [creating, setCreating] = useState(false)
  const list = useListQuery<any>('receipts', '/sales/receipts', {})

  const columns: Column<any>[] = [
    {
      key: 'number',
      header: 'Receipt',
      render: (row) => <span className="font-medium text-strong">{row.number}</span>,
    },
    { key: 'customer', header: 'Customer', render: (row) => row.customer_name },
    {
      key: 'date',
      header: 'Date',
      render: (row) => <span className="tabular text-muted">{formatDate(row.receipt_date)}</span>,
    },
    { key: 'method', header: 'Method', secondary: true, render: (row) => row.method?.replace(/_/g, ' ') },
    {
      key: 'reference',
      header: 'Reference',
      secondary: true,
      render: (row) => <span className="font-mono text-2xs text-muted">{row.reference || '—'}</span>,
    },
    {
      key: 'unallocated',
      header: 'Unallocated',
      align: 'right',
      secondary: true,
      render: (row) =>
        toPaise(row.unallocated_amount) > 0 ? (
          <Amount value={row.unallocated_amount} className="text-warning" />
        ) : (
          <span className="text-subtle">—</span>
        ),
    },
    {
      key: 'amount',
      header: 'Amount',
      align: 'right',
      render: (row) => <Amount value={row.amount} className="font-semibold" />,
    },
    { key: 'status', header: 'Status', render: (row) => <StatusBadge status={row.status} /> },
  ]

  return (
    <ListPage
      title="Receipts"
      description="Money received from customers, allocated to invoices oldest-first unless you choose otherwise."
      actions={
        <Button variant="primary" size="sm" icon={Plus} onClick={() => setCreating(true)}>
          Record receipt
        </Button>
      }
      columns={columns}
      list={list}
      rowKey={(row) => row.id}
      searchPlaceholder="Search receipts"
      emptyIcon={Wallet}
      emptyTitle="No receipts recorded"
      emptyDescription="Record a customer payment to settle open invoices."
      emptyAction={
        <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
          Record receipt
        </Button>
      }
    >
      {creating && <ReceiptForm onClose={() => setCreating(false)} />}
    </ListPage>
  )
}

function ReceiptForm({ onClose }: { onClose: () => void }) {
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()
  const customers = useCustomers()
  const banks = useBankAccounts()

  const [customerId, setCustomerId] = useState('')
  const [amount, setAmount] = useState('')
  const [receiptDate, setReceiptDate] = useState(todayISO())
  const [method, setMethod] = useState('NEFT')
  const [reference, setReference] = useState('')
  const [bankAccountId, setBankAccountId] = useState('')
  const [tds, setTds] = useState('0')
  const [selected, setSelected] = useState<Record<string, boolean>>({})
  const [error, setError] = useState('')

  const { data: openInvoices } = useQuery({
    queryKey: ['open-invoices', customerId],
    queryFn: () =>
      api.get('/sales/invoices', { customer_id: customerId, limit: 50 }),
    enabled: Boolean(customerId),
  })

  const outstanding = useMemo(
    () => (openInvoices?.items ?? []).filter((invoice: any) => toPaise(invoice.balance_due) > 0),
    [openInvoices],
  )

  const selectedTotal = useMemo(
    () =>
      outstanding
        .filter((invoice: any) => selected[invoice.id])
        .reduce((total: number, invoice: any) => total + toPaise(invoice.balance_due), 0),
    [outstanding, selected],
  )

  const create = useMutation({
    mutationFn: () => {
      const allocations = outstanding
        .filter((invoice: any) => selected[invoice.id])
        .map((invoice: any) => ({ invoice_id: invoice.id, amount: invoice.balance_due }))
      return api.post('/sales/receipts', {
        customer_id: customerId,
        receipt_date: receiptDate,
        amount,
        method,
        reference,
        bank_account_id: bankAccountId || undefined,
        tds_deducted: tds || '0',
        allocations: allocations.length ? allocations : undefined,
        auto_allocate: allocations.length === 0,
      })
    },
    onSuccess: (receipt) => {
      push({
        tone: 'positive',
        title: `Receipt ${receipt.number} recorded`,
        description: `${formatMoney(receipt.amount)} received and allocated.`,
      })
      queryClient.invalidateQueries({ queryKey: ['receipts'] })
      refresh()
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not record the receipt.'),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title="Record receipt"
      size="lg"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!customerId || !amount || Number(amount) <= 0}
            onClick={() => {
              setError('')
              create.mutate()
            }}
          >
            Record receipt
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Customer" required>
            <Select
              value={customerId}
              onChange={(event) => {
                setCustomerId(event.target.value)
                setSelected({})
              }}
            >
              <option value="">Select a customer…</option>
              {(customers.data?.items ?? []).map((customer: any) => (
                <option key={customer.id} value={customer.id}>
                  {customer.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Amount received" required>
            <Input
              type="number"
              step="0.01"
              min="0"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
              className="tabular"
            />
          </Field>
          <Field label="Date" required>
            <Input
              type="date"
              value={receiptDate}
              onChange={(event) => setReceiptDate(event.target.value)}
            />
          </Field>
          <Field label="Method">
            <Select value={method} onChange={(event) => setMethod(event.target.value)}>
              {['NEFT', 'RTGS', 'IMPS', 'UPI', 'CHEQUE', 'CASH', 'CARD', 'BANK_TRANSFER'].map(
                (option) => (
                  <option key={option} value={option}>
                    {option.replace(/_/g, ' ')}
                  </option>
                ),
              )}
            </Select>
          </Field>
          <Field label="Deposit into">
            <Select
              value={bankAccountId}
              onChange={(event) => setBankAccountId(event.target.value)}
            >
              <option value="">Default bank account</option>
              {(banks.data ?? []).map((bank: any) => (
                <option key={bank.id} value={bank.id}>
                  {bank.name} · {bank.masked_number}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Reference" hint="UTR or cheque number — used for reconciliation">
            <Input
              value={reference}
              onChange={(event) => setReference(event.target.value)}
              placeholder="UTR123456789012"
            />
          </Field>
          <Field label="TDS deducted by customer" className="sm:col-span-2">
            <Input
              type="number"
              step="0.01"
              min="0"
              value={tds}
              onChange={(event) => setTds(event.target.value)}
              className="tabular"
            />
          </Field>
        </div>

        {customerId && (
          <div>
            <p className="label">
              Allocate to invoices
              <span className="ml-1 font-normal text-subtle">
                (leave all unticked to allocate oldest-first automatically)
              </span>
            </p>
            {outstanding.length === 0 ? (
              <p className="rounded-lg bg-raised p-3 text-xs text-muted">
                This customer has no outstanding invoices.
              </p>
            ) : (
              <div className="max-h-52 overflow-y-auto rounded-lg border border-border">
                {outstanding.map((invoice: any) => (
                  <label
                    key={invoice.id}
                    className="flex cursor-pointer items-center gap-2.5 border-b border-border px-3 py-2 last:border-0 hover:bg-raised"
                  >
                    <input
                      type="checkbox"
                      checked={Boolean(selected[invoice.id])}
                      onChange={(event) =>
                        setSelected((current) => ({
                          ...current,
                          [invoice.id]: event.target.checked,
                        }))
                      }
                      className="h-4 w-4 rounded border-border"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs font-medium text-strong">
                        {invoice.number}
                      </span>
                      <span className="block text-2xs text-muted">
                        Due {formatDate(invoice.due_date)}
                        {invoice.days_overdue > 0 && (
                          <span className="ml-1 text-negative">
                            · {invoice.days_overdue}d overdue
                          </span>
                        )}
                      </span>
                    </span>
                    <Amount value={invoice.balance_due} className="shrink-0 text-xs font-medium" />
                  </label>
                ))}
              </div>
            )}
            {selectedTotal > 0 && (
              <p className="mt-1.5 text-2xs text-muted">
                Selected total:{' '}
                <strong className="tabular text-strong">
                  {formatMoney((selectedTotal / 100).toFixed(2))}
                </strong>
                {amount && toPaise(amount) !== selectedTotal && (
                  <span className="ml-1 text-warning">
                    — differs from the receipt amount, so the balance will remain unallocated.
                  </span>
                )}
              </p>
            )}
          </div>
        )}
      </div>
    </Modal>
  )
}
