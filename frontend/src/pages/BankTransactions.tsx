/** Imported bank transactions, with one-click categorisation that teaches a rule. */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Landmark, Sparkles } from 'lucide-react'
import { api } from '@/lib/api'
import { formatDate, formatMoney } from '@/lib/money'
import {
  useBankAccounts,
  useListQuery,
  usePostableAccounts,
  useRefreshBooks,
} from '@/lib/hooks'
import { ListPage } from '@/components/ListPage'
import {
  Alert,
  Button,
  ConfidenceBadge,
  Field,
  Modal,
  Select,
  StatusBadge,
  useToast,
  type Column,
} from '@/components/ui'

const STATUSES = ['', 'UNMATCHED', 'MATCHED', 'PARTIAL', 'AMBIGUOUS', 'EXCEPTION']

export default function BankTransactions() {
  const [status, setStatus] = useState('')
  const [bankAccountId, setBankAccountId] = useState('')
  const [categorising, setCategorising] = useState<any>(null)

  const banks = useBankAccounts()
  const list = useListQuery<any>('bank-transactions', '/banking/transactions', {
    status: status || undefined,
    bank_account_id: bankAccountId || undefined,
  })

  const columns: Column<any>[] = [
    {
      key: 'date',
      header: 'Date',
      render: (row) => <span className="tabular text-muted">{formatDate(row.txn_date)}</span>,
    },
    {
      key: 'description',
      header: 'Description',
      render: (row) => (
        <div className="min-w-0">
          <p className="line-clamp-1 text-sm text-strong">{row.description}</p>
          {row.reference && (
            <p className="font-mono text-2xs text-subtle">{row.reference}</p>
          )}
        </div>
      ),
    },
    {
      key: 'suggested',
      header: 'Suggested account',
      secondary: true,
      render: (row) =>
        row.suggested_category ? (
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-muted">{row.suggested_category}</span>
            {row.suggested_confidence && (
              <ConfidenceBadge value={row.suggested_confidence} />
            )}
          </div>
        ) : (
          <span className="text-subtle">—</span>
        ),
    },
    {
      key: 'amount',
      header: 'Amount',
      align: 'right',
      render: (row) => (
        <span
          className={`font-medium tabular ${
            row.direction === 'CREDIT' ? 'text-positive' : 'text-strong'
          }`}
        >
          {row.direction === 'CREDIT' ? '+' : '−'}
          {formatMoney(row.amount, { symbol: false })}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      render: (row) => <StatusBadge status={row.recon_status} />,
    },
    {
      key: 'action',
      header: '',
      align: 'right',
      render: (row) =>
        !row.journal_entry_id ? (
          <Button
            size="sm"
            onClick={(event) => {
              event.stopPropagation()
              setCategorising(row)
            }}
          >
            Categorise
          </Button>
        ) : (
          <span className="text-2xs text-subtle">Posted</span>
        ),
    },
  ]

  return (
    <ListPage
      title="Bank transactions"
      description="Statement lines exactly as the bank reported them. They are never edited — they are external evidence."
      columns={columns}
      list={list}
      rowKey={(row) => row.id}
      searchPlaceholder="Search descriptions and references"
      filters={[
        {
          id: 'account',
          label: 'Bank account',
          value: bankAccountId,
          onChange: setBankAccountId,
          options: [
            { value: '', label: 'All accounts' },
            ...(banks.data ?? []).map((bank: any) => ({
              value: bank.id,
              label: `${bank.name} · ${bank.masked_number}`,
            })),
          ],
          width: 'w-56',
        },
        {
          id: 'status',
          label: 'Status',
          value: status,
          onChange: setStatus,
          options: STATUSES.map((option) => ({
            value: option,
            label: option ? option.replace(/_/g, ' ') : 'All statuses',
          })),
        },
      ]}
      emptyIcon={Landmark}
      emptyTitle="No bank transactions"
      emptyDescription="Import a statement from the Bank Accounts page to get started."
    >
      {categorising && (
        <CategoriseDialog
          transaction={categorising}
          onClose={() => setCategorising(null)}
        />
      )}
    </ListPage>
  )
}

function CategoriseDialog({
  transaction,
  onClose,
}: {
  transaction: any
  onClose: () => void
}) {
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()
  const accounts = usePostableAccounts()

  const [accountId, setAccountId] = useState(transaction.suggested_account_id ?? '')
  const [learn, setLearn] = useState(true)
  const [error, setError] = useState('')

  const categorise = useMutation({
    mutationFn: () =>
      api.post(`/banking/transactions/${transaction.id}/categorise`, {
        account_id: accountId,
        learn,
      }),
    onSuccess: () => {
      push({
        tone: 'positive',
        title: 'Transaction posted',
        description: learn
          ? 'A rule was saved, so similar transactions will be categorised automatically next time.'
          : 'Posted to the ledger.',
      })
      queryClient.invalidateQueries({ queryKey: ['bank-transactions'] })
      refresh()
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not post the transaction.'),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title="Categorise transaction"
      description="Posting creates a balanced journal entry against the bank account."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={categorise.isPending}
            disabled={!accountId}
            onClick={() => {
              setError('')
              categorise.mutate()
            }}
          >
            Post to ledger
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}

        <div className="rounded-lg bg-raised p-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-sm text-strong">{transaction.description}</p>
              <p className="mt-0.5 text-2xs text-muted">
                {formatDate(transaction.txn_date)}
                {transaction.reference && ` · ${transaction.reference}`}
              </p>
            </div>
            <span
              className={`shrink-0 text-base font-semibold tabular ${
                transaction.direction === 'CREDIT' ? 'text-positive' : 'text-strong'
              }`}
            >
              {transaction.direction === 'CREDIT' ? '+' : '−'}
              {formatMoney(transaction.amount, { symbol: false })}
            </span>
          </div>
        </div>

        {transaction.suggested_category && (
          <Alert tone="info" title="Suggested categorisation">
            <div className="flex items-center gap-2">
              <Sparkles className="h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>
                {transaction.suggested_category}
                {transaction.categorised_by && (
                  <span className="ml-1 text-2xs opacity-80">
                    (matched by {transaction.categorised_by.toLowerCase()})
                  </span>
                )}
              </span>
            </div>
          </Alert>
        )}

        <Field
          label="Post to account"
          required
          hint={
            transaction.direction === 'CREDIT'
              ? 'Money in: the bank account is debited and this account is credited.'
              : 'Money out: this account is debited and the bank account is credited.'
          }
        >
          <Select value={accountId} onChange={(event) => setAccountId(event.target.value)}>
            <option value="">Select an account…</option>
            {(accounts.data ?? []).map((account: any) => (
              <option key={account.id} value={account.id}>
                {account.code} — {account.name}
              </option>
            ))}
          </Select>
        </Field>

        <label className="flex items-start gap-2">
          <input
            type="checkbox"
            checked={learn}
            onChange={(event) => setLearn(event.target.checked)}
            className="mt-0.5 h-4 w-4 rounded border-border"
          />
          <span>
            <span className="block text-sm text-strong">Remember this categorisation</span>
            <span className="block text-2xs text-muted">
              Saves a deterministic rule so transactions with this description are categorised
              automatically — no model call needed.
            </span>
          </span>
        </label>
      </div>
    </Modal>
  )
}
