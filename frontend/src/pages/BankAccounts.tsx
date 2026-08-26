/** Bank accounts, with the book-versus-statement difference made obvious. */
import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Banknote, Plus, Upload } from 'lucide-react'
import { api } from '@/lib/api'
import { formatDate, formatMoney, toPaise } from '@/lib/money'
import { useBankAccounts, useRefreshBooks } from '@/lib/hooks'
import {
  Alert,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Input,
  Modal,
  PageHeader,
  Select,
  Spinner,
  useToast,
} from '@/components/ui'

export default function BankAccounts() {
  const navigate = useNavigate()
  const [creating, setCreating] = useState(false)
  const [importInto, setImportInto] = useState<any>(null)
  const accounts = useBankAccounts()
  const summary = useQuery({ queryKey: ['bank-summary'], queryFn: () => api.get('/banking/summary') })

  return (
    <div>
      <PageHeader
        title="Bank accounts"
        description="The ledger is the source of truth for the balance; the statement figure is the bank's own view."
        actions={
          <Button variant="primary" size="sm" icon={Plus} onClick={() => setCreating(true)}>
            Add account
          </Button>
        }
      />

      {summary.data && summary.data.total_transactions > 0 && (
        <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="Transactions" value={String(summary.data.total_transactions)} />
          <Stat label="Matched" value={String(summary.data.matched_count)} tone="positive" />
          <Stat
            label="Need attention"
            value={String(summary.data.needs_attention_count)}
            tone={summary.data.needs_attention_count > 0 ? 'warning' : 'positive'}
          />
          <Stat label="Match rate" value={`${Number(summary.data.match_rate).toFixed(1)}%`} />
        </div>
      )}

      {accounts.isLoading ? (
        <Spinner label="Loading bank accounts" />
      ) : !accounts.data?.length ? (
        <Card>
          <EmptyState
            icon={Banknote}
            title="No bank accounts yet"
            description="Add a bank account, then import a statement to start reconciling."
            action={
              <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
                Add account
              </Button>
            }
          />
        </Card>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {accounts.data.map((account: any) => {
            const difference = account.difference
            const hasDifference = difference != null && toPaise(difference) !== 0
            return (
              <Card key={account.id} bodyClassName="p-4">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-strong">{account.name}</p>
                    <p className="truncate text-2xs text-muted">
                      {account.bank_name} · {account.masked_number}
                    </p>
                  </div>
                  <Badge tone="neutral">{account.account_type}</Badge>
                </div>

                <div className="mt-3 space-y-1.5">
                  <Row label="Balance per books" value={formatMoney(account.book_balance)} strong />
                  {account.statement_balance != null && (
                    <>
                      <Row
                        label="Balance per statement"
                        value={formatMoney(account.statement_balance)}
                      />
                      <Row
                        label="Difference"
                        value={formatMoney(difference)}
                        tone={hasDifference ? 'negative' : 'positive'}
                      />
                    </>
                  )}
                  {account.statement_balance_date && (
                    <p className="text-2xs text-subtle">
                      Statement as at {formatDate(account.statement_balance_date)}
                    </p>
                  )}
                </div>

                {hasDifference && (
                  <Alert tone="warning" className="mt-3">
                    The books and the statement disagree. Reconcile to find the outstanding
                    items.
                  </Alert>
                )}

                <div className="mt-3 flex gap-2">
                  <Button size="sm" icon={Upload} onClick={() => setImportInto(account)}>
                    Import statement
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => navigate(`/banking/reconciliation?account=${account.id}`)}
                  >
                    Reconcile
                  </Button>
                </div>
              </Card>
            )
          })}
        </div>
      )}

      {creating && <BankAccountForm onClose={() => setCreating(false)} />}
      {importInto && (
        <ImportStatement account={importInto} onClose={() => setImportInto(null)} />
      )}
    </div>
  )
}

function Row({
  label,
  value,
  strong,
  tone,
}: {
  label: string
  value: string
  strong?: boolean
  tone?: 'positive' | 'negative'
}) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-xs text-muted">{label}</span>
      <span
        className={`tabular ${strong ? 'text-base font-semibold' : 'text-sm'} ${
          tone === 'negative'
            ? 'text-negative'
            : tone === 'positive'
              ? 'text-positive'
              : 'text-strong'
        }`}
      >
        {value}
      </span>
    </div>
  )
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="card p-2.5">
      <p className="text-2xs uppercase tracking-wider text-muted">{label}</p>
      <p
        className={`mt-0.5 text-sm font-semibold tabular ${
          tone === 'positive'
            ? 'text-positive'
            : tone === 'warning'
              ? 'text-warning'
              : 'text-strong'
        }`}
      >
        {value}
      </p>
    </div>
  )
}

function BankAccountForm({ onClose }: { onClose: () => void }) {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    name: '',
    bank_name: '',
    account_number: '',
    ifsc: '',
    account_type: 'CURRENT',
    currency: 'INR',
  })
  const [error, setError] = useState('')

  const create = useMutation({
    mutationFn: () => api.post('/banking/accounts', form),
    onSuccess: (account) => {
      push({
        tone: 'positive',
        title: `${account.name} added`,
        description: 'A dedicated ledger account was created for it.',
      })
      queryClient.invalidateQueries({ queryKey: ['bank-accounts'] })
      queryClient.invalidateQueries({ queryKey: ['accounts-flat'] })
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not add the account.'),
  })

  const set = (patch: Partial<typeof form>) => setForm((current) => ({ ...current, ...patch }))

  return (
    <Modal
      open
      onClose={onClose}
      title="Add bank account"
      description="A matching GL account is created automatically."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!form.name || !form.bank_name || form.account_number.length < 4}
            onClick={() => {
              setError('')
              create.mutate()
            }}
          >
            Add account
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}
        <Field label="Account name" required>
          <Input
            value={form.name}
            onChange={(event) => set({ name: event.target.value })}
            placeholder="Primary Current Account"
          />
        </Field>
        <Field label="Bank" required>
          <Input
            value={form.bank_name}
            onChange={(event) => set({ bank_name: event.target.value })}
            placeholder="HDFC Bank"
          />
        </Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Account number" required>
            <Input
              value={form.account_number}
              onChange={(event) => set({ account_number: event.target.value })}
              className="font-mono"
            />
          </Field>
          <Field label="IFSC">
            <Input
              value={form.ifsc}
              onChange={(event) => set({ ifsc: event.target.value.toUpperCase() })}
              maxLength={11}
              className="font-mono"
            />
          </Field>
          <Field label="Type">
            <Select
              value={form.account_type}
              onChange={(event) => set({ account_type: event.target.value })}
            >
              {['CURRENT', 'SAVINGS', 'OD', 'CC', 'WALLET'].map((type) => (
                <option key={type} value={type}>
                  {type}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Currency">
            <Select
              value={form.currency}
              onChange={(event) => set({ currency: event.target.value })}
            >
              {['INR', 'USD', 'EUR', 'GBP'].map((currency) => (
                <option key={currency} value={currency}>
                  {currency}
                </option>
              ))}
            </Select>
          </Field>
        </div>
      </div>
    </Modal>
  )
}

function ImportStatement({ account, onClose }: { account: any; onClose: () => void }) {
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()
  const fileRef = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<any>(null)
  const [error, setError] = useState('')

  const previewMutation = useMutation({
    mutationFn: (selected: File) =>
      api.upload(`/banking/accounts/${account.id}/import/preview`, selected),
    onSuccess: setPreview,
    onError: (caught: any) => setError(caught?.message ?? 'Could not read the file.'),
  })

  const importMutation = useMutation({
    mutationFn: () => api.upload(`/banking/accounts/${account.id}/import`, file!),
    onSuccess: (result) => {
      push({
        tone: result.imported > 0 ? 'positive' : 'info',
        title: `${result.imported} transaction(s) imported`,
        description: `${result.duplicates} already present, ${result.rejected} rejected.`,
      })
      queryClient.invalidateQueries({ queryKey: ['bank-transactions'] })
      refresh()
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'The import failed.'),
  })

  const choose = (selected: File | null) => {
    setError('')
    setPreview(null)
    setFile(selected)
    if (selected) previewMutation.mutate(selected)
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={`Import statement — ${account.name}`}
      description="CSV, XLSX or JSON. Re-importing overlapping periods is safe: duplicates are skipped."
      size="lg"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={importMutation.isPending}
            disabled={!file || !preview}
            onClick={() => {
              setError('')
              importMutation.mutate()
            }}
          >
            Import {preview ? `${preview.total_rows} rows` : ''}
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}

        <button
          onClick={() => fileRef.current?.click()}
          className="flex w-full flex-col items-center gap-2 rounded-xl border-2 border-dashed border-border p-6 transition-colors hover:border-brand/50 hover:bg-raised"
        >
          <Upload className="h-6 w-6 text-subtle" aria-hidden />
          <span className="text-sm font-medium text-strong">
            {file ? file.name : 'Choose a statement file'}
          </span>
          <span className="text-2xs text-muted">CSV, XLSX or JSON</span>
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".csv,.xlsx,.xls,.json"
          className="hidden"
          onChange={(event) => choose(event.target.files?.[0] ?? null)}
        />

        {previewMutation.isPending && <Spinner label="Reading the file" />}

        {preview && (
          <>
            <div className="rounded-lg bg-raised p-3">
              <p className="text-xs font-medium text-strong">
                Detected {preview.total_rows} rows
              </p>
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {Object.entries(preview.detected_mapping).map(([field, column]) => (
                  <Badge key={field} tone="neutral">
                    {field} → {String(column)}
                  </Badge>
                ))}
              </div>
              {preview.warnings?.map((warning: string) => (
                <Alert key={warning} tone="warning" className="mt-2">
                  {warning}
                </Alert>
              ))}
            </div>

            {preview.sample_rows?.length > 0 && (
              <div className="rounded-lg border border-border">
                <p className="border-b border-border px-3 py-1.5 text-2xs font-semibold uppercase tracking-wider text-muted">
                  Preview
                </p>
                <div className="scroll-x">
                  <table className="w-full min-w-[480px]">
                    <thead>
                      <tr className="table-head">
                        <th className="px-2 py-1.5 text-left">Date</th>
                        <th className="px-2 py-1.5 text-left">Description</th>
                        <th className="px-2 py-1.5 text-right">Amount</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {preview.sample_rows.map((row: any, index: number) => (
                        <tr key={index}>
                          <td className="px-2 py-1.5 text-2xs tabular">
                            {formatDate(row.date)}
                          </td>
                          <td className="max-w-[240px] truncate px-2 py-1.5 text-2xs">
                            {row.description}
                          </td>
                          <td
                            className={`px-2 py-1.5 text-right text-2xs tabular ${
                              row.direction === 'CREDIT' ? 'text-positive' : 'text-negative'
                            }`}
                          >
                            {row.direction === 'CREDIT' ? '+' : '−'}
                            {formatMoney(row.amount, { symbol: false })}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {preview.sample_errors?.length > 0 && (
              <Alert tone="warning" title={`${preview.sample_errors.length} row(s) could not be read`}>
                <ul className="mt-1 space-y-0.5">
                  {preview.sample_errors.slice(0, 3).map((issue: any, index: number) => (
                    <li key={index}>
                      Row {issue.row}: {issue.error}
                    </li>
                  ))}
                </ul>
              </Alert>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}
