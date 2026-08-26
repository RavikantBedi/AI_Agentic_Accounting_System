/** Vendor payments. Releasing money is a human-only action, by design. */
import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Banknote, Plus, ShieldCheck } from 'lucide-react'
import { api } from '@/lib/api'
import { formatDate, formatMoney, toPaise, todayISO } from '@/lib/money'
import {
  useBankAccounts,
  useListQuery,
  usePermission,
  useRefreshBooks,
  useVendors,
} from '@/lib/hooks'
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

export default function Payments() {
  const [creating, setCreating] = useState(false)
  const canRelease = usePermission('release_payment')
  const list = useListQuery<any>('payments', '/purchases/payments', {})

  const columns: Column<any>[] = [
    {
      key: 'number',
      header: 'Payment',
      render: (row) => <span className="font-medium text-strong">{row.number}</span>,
    },
    { key: 'vendor', header: 'Vendor', render: (row) => row.vendor_name },
    {
      key: 'date',
      header: 'Date',
      render: (row) => <span className="tabular text-muted">{formatDate(row.payment_date)}</span>,
    },
    {
      key: 'method',
      header: 'Method',
      secondary: true,
      render: (row) => row.method?.replace(/_/g, ' '),
    },
    {
      key: 'reference',
      header: 'Reference',
      secondary: true,
      render: (row) => (
        <span className="font-mono text-2xs text-muted">{row.reference || '—'}</span>
      ),
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
      title="Payments"
      description="AI agents can prepare a payment run, but only a person with release authority can pay."
      actions={
        canRelease ? (
          <Button variant="primary" size="sm" icon={Plus} onClick={() => setCreating(true)}>
            Release payment
          </Button>
        ) : undefined
      }
      columns={columns}
      list={list}
      rowKey={(row) => row.id}
      searchPlaceholder="Search payments"
      emptyIcon={Banknote}
      emptyTitle="No payments recorded"
      emptyDescription="Release a payment to settle outstanding vendor bills."
      summary={
        !canRelease ? (
          <Alert tone="info" className="mb-3" title="You cannot release payments">
            Your role does not include the <code>release_payment</code> permission. You can see
            the payment history, but only a finance manager or owner can authorise a payment.
          </Alert>
        ) : undefined
      }
    >
      {creating && <PaymentForm onClose={() => setCreating(false)} />}
    </ListPage>
  )
}

function PaymentForm({ onClose }: { onClose: () => void }) {
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()
  const vendors = useVendors()
  const banks = useBankAccounts()

  const [vendorId, setVendorId] = useState('')
  const [amount, setAmount] = useState('')
  const [paymentDate, setPaymentDate] = useState(todayISO())
  const [method, setMethod] = useState('NEFT')
  const [reference, setReference] = useState('')
  const [bankAccountId, setBankAccountId] = useState('')
  const [selected, setSelected] = useState<Record<string, boolean>>({})
  const [error, setError] = useState('')

  const { data: openBills } = useQuery({
    queryKey: ['open-bills', vendorId],
    queryFn: () => api.get('/purchases/bills', { vendor_id: vendorId, limit: 50 }),
    enabled: Boolean(vendorId),
  })

  const outstanding = useMemo(
    () => (openBills?.items ?? []).filter((bill: any) => toPaise(bill.balance_due) > 0),
    [openBills],
  )

  const selectedTotal = useMemo(
    () =>
      outstanding
        .filter((bill: any) => selected[bill.id])
        .reduce((total: number, bill: any) => total + toPaise(bill.balance_due), 0),
    [outstanding, selected],
  )

  const create = useMutation({
    mutationFn: () => {
      const allocations = outstanding
        .filter((bill: any) => selected[bill.id])
        .map((bill: any) => ({ bill_id: bill.id, amount: bill.balance_due }))
      return api.post('/purchases/payments', {
        vendor_id: vendorId,
        payment_date: paymentDate,
        amount,
        method,
        reference,
        bank_account_id: bankAccountId || undefined,
        allocations: allocations.length ? allocations : undefined,
        auto_allocate: allocations.length === 0,
      })
    },
    onSuccess: (payment) => {
      push({
        tone: 'positive',
        title: `Payment ${payment.number} released`,
        description: `${formatMoney(payment.amount)} to ${payment.vendor_name}.`,
      })
      queryClient.invalidateQueries({ queryKey: ['payments'] })
      refresh()
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not release the payment.'),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title="Release payment"
      description="This action moves money and is recorded against your name in the audit trail."
      size="lg"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            icon={ShieldCheck}
            loading={create.isPending}
            disabled={!vendorId || !amount || Number(amount) <= 0}
            onClick={() => {
              setError('')
              create.mutate()
            }}
          >
            Authorise and release
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Vendor" required>
            <Select
              value={vendorId}
              onChange={(event) => {
                setVendorId(event.target.value)
                setSelected({})
              }}
            >
              <option value="">Select a vendor…</option>
              {(vendors.data?.items ?? []).map((vendor: any) => (
                <option key={vendor.id} value={vendor.id}>
                  {vendor.name}
                  {vendor.is_msme ? ' · MSME' : ''}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Amount" required>
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
              value={paymentDate}
              onChange={(event) => setPaymentDate(event.target.value)}
            />
          </Field>
          <Field label="Method">
            <Select value={method} onChange={(event) => setMethod(event.target.value)}>
              {['NEFT', 'RTGS', 'IMPS', 'UPI', 'CHEQUE', 'CASH', 'BANK_TRANSFER'].map((option) => (
                <option key={option} value={option}>
                  {option.replace(/_/g, ' ')}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Pay from">
            <Select
              value={bankAccountId}
              onChange={(event) => setBankAccountId(event.target.value)}
            >
              <option value="">Default bank account</option>
              {(banks.data ?? []).map((bank: any) => (
                <option key={bank.id} value={bank.id}>
                  {bank.name} · {formatMoney(bank.book_balance, { compact: true })}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Reference">
            <Input
              value={reference}
              onChange={(event) => setReference(event.target.value)}
              placeholder="UTR or cheque number"
            />
          </Field>
        </div>

        {vendorId && (
          <div>
            <p className="label">Settle these bills</p>
            {outstanding.length === 0 ? (
              <p className="rounded-lg bg-raised p-3 text-xs text-muted">
                This vendor has no outstanding bills.
              </p>
            ) : (
              <div className="max-h-52 overflow-y-auto rounded-lg border border-border">
                {outstanding.map((bill: any) => (
                  <label
                    key={bill.id}
                    className="flex cursor-pointer items-center gap-2.5 border-b border-border px-3 py-2 last:border-0 hover:bg-raised"
                  >
                    <input
                      type="checkbox"
                      checked={Boolean(selected[bill.id])}
                      onChange={(event) =>
                        setSelected((current) => ({
                          ...current,
                          [bill.id]: event.target.checked,
                        }))
                      }
                      className="h-4 w-4 rounded border-border"
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs font-medium text-strong">
                        {bill.number}{' '}
                        <span className="font-normal text-subtle">
                          ({bill.vendor_invoice_number})
                        </span>
                      </span>
                      <span className="block text-2xs text-muted">
                        Due {formatDate(bill.due_date)}
                        {bill.days_overdue > 0 && (
                          <span className="ml-1 text-negative">
                            · {bill.days_overdue}d overdue
                          </span>
                        )}
                      </span>
                    </span>
                    <Amount value={bill.balance_due} className="shrink-0 text-xs font-medium" />
                  </label>
                ))}
              </div>
            )}
            {selectedTotal > 0 && (
              <button
                onClick={() => setAmount((selectedTotal / 100).toFixed(2))}
                className="mt-1.5 text-2xs font-medium text-brand hover:underline"
              >
                Set amount to selected total (
                {formatMoney((selectedTotal / 100).toFixed(2))})
              </button>
            )}
          </div>
        )}
      </div>
    </Modal>
  )
}
