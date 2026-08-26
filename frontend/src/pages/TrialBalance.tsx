/** Trial balance — with the balanced/unbalanced verdict stated plainly. */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Download } from 'lucide-react'
import { api } from '@/lib/api'
import { formatMoney, todayISO, toPaise } from '@/lib/money'
import {
  Alert,
  Button,
  Card,
  ErrorState,
  Field,
  Input,
  PageHeader,
  Spinner,
  Toolbar,
} from '@/components/ui'

export default function TrialBalance() {
  const navigate = useNavigate()
  const [asOf, setAsOf] = useState(todayISO())
  const [includeZero, setIncludeZero] = useState(false)

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['trial-balance', asOf, includeZero],
    queryFn: () =>
      api.get('/accounting/trial-balance', { as_of: asOf, include_zero: includeZero }),
  })

  const invariants = useQuery({
    queryKey: ['invariants', asOf],
    queryFn: () => api.get('/accounting/invariants', { as_of: asOf }),
  })

  if (isError) return <ErrorState error={error} retry={() => refetch()} />

  return (
    <div>
      <PageHeader
        title="Trial balance"
        description="Aggregated from posted journal lines. Nothing here is cached."
        actions={
          <Button
            size="sm"
            icon={Download}
            onClick={() =>
              api.download('/reports/export/trial-balance.csv', `trial-balance-${asOf}.csv`, {
                as_of: asOf,
              })
            }
          >
            Export CSV
          </Button>
        }
      >
        <Toolbar>
          <Field label="As at" className="w-44">
            <Input
              type="date"
              value={asOf}
              onChange={(event) => setAsOf(event.target.value)}
            />
          </Field>
          <label className="mt-5 flex items-center gap-2 text-xs text-muted">
            <input
              type="checkbox"
              checked={includeZero}
              onChange={(event) => setIncludeZero(event.target.checked)}
              className="h-4 w-4 rounded border-border"
            />
            Include zero-balance accounts
          </label>
        </Toolbar>
      </PageHeader>

      {data && (
        <div className="mb-3">
          {data.balanced ? (
            <Alert tone="positive" title="The trial balance balances">
              Total debits of {formatMoney(data.total_debit)} equal total credits across{' '}
              {data.account_count} accounts.
            </Alert>
          ) : (
            <Alert tone="negative" title="The trial balance does not balance">
              Debits {formatMoney(data.total_debit)} against credits{' '}
              {formatMoney(data.total_credit)} — a difference of {formatMoney(data.difference)}.
              This should be impossible; investigate immediately.
            </Alert>
          )}
        </div>
      )}

      {invariants.data && (
        <Card title="Accounting invariants" className="mb-3" bodyClassName="p-3">
          <ul className="grid gap-1.5 sm:grid-cols-2">
            {invariants.data.checks.map((check: any) => (
              <li key={check.name} className="flex items-start gap-2">
                {check.passed ? (
                  <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-positive" aria-hidden />
                ) : (
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-negative" aria-hidden />
                )}
                <span className="min-w-0">
                  <span className="block text-xs font-medium text-strong">{check.name}</span>
                  <span className="block text-2xs text-muted">{check.detail}</span>
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card bodyClassName="p-0">
        {isLoading ? (
          <Spinner label="Computing the trial balance" />
        ) : (
          <div className="scroll-x">
            <table className="w-full min-w-[560px]">
              <thead>
                <tr className="table-head">
                  <th className="px-3 py-2 text-left">Code</th>
                  <th className="px-3 py-2 text-left">Account</th>
                  <th className="hidden px-3 py-2 text-left md:table-cell">Type</th>
                  <th className="px-3 py-2 text-right">Debit</th>
                  <th className="px-3 py-2 text-right">Credit</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {data.rows.map((row: any) => (
                  <tr
                    key={row.account_id}
                    onClick={() => navigate(`/accounting/ledger/${row.account_id}`)}
                    className="cursor-pointer transition-colors hover:bg-raised"
                  >
                    <td className="px-3 py-2 font-mono text-2xs text-subtle">{row.code}</td>
                    <td className="px-3 py-2 text-sm text-strong">{row.name}</td>
                    <td className="hidden px-3 py-2 text-2xs uppercase text-muted md:table-cell">
                      {row.type}
                    </td>
                    <td className="px-3 py-2 text-right text-sm tabular">
                      {toPaise(row.debit) ? formatMoney(row.debit) : ''}
                    </td>
                    <td className="px-3 py-2 text-right text-sm tabular">
                      {toPaise(row.credit) ? formatMoney(row.credit) : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
              <tfoot className="border-t-2 border-border bg-raised">
                <tr>
                  <td colSpan={3} className="px-3 py-2.5 text-xs font-semibold text-muted">
                    Totals
                  </td>
                  <td className="px-3 py-2.5 text-right text-sm font-bold tabular text-strong">
                    {formatMoney(data.total_debit)}
                  </td>
                  <td className="px-3 py-2.5 text-right text-sm font-bold tabular text-strong">
                    {formatMoney(data.total_credit)}
                  </td>
                </tr>
              </tfoot>
            </table>
          </div>
        )}
      </Card>
    </div>
  )
}
