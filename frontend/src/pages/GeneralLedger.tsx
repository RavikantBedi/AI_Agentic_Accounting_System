/** General ledger for one account, with a running balance. */
import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Download, ExternalLink } from 'lucide-react'
import { api } from '@/lib/api'
import { fiscalYearStartISO, formatDate, formatMoney, todayISO, toPaise } from '@/lib/money'
import { usePostableAccounts } from '@/lib/hooks'
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Field,
  Input,
  PageHeader,
  Pagination,
  Select,
  Spinner,
  Toolbar,
} from '@/components/ui'

export default function GeneralLedger() {
  const { accountId } = useParams()
  const navigate = useNavigate()
  const accounts = usePostableAccounts()

  const [selected, setSelected] = useState(accountId ?? '')
  const [startDate, setStartDate] = useState(fiscalYearStartISO())
  const [endDate, setEndDate] = useState(todayISO())
  const [offset, setOffset] = useState(0)
  const limit = 100

  useEffect(() => {
    if (accountId) setSelected(accountId)
  }, [accountId])

  // Default to the first account with activity so the page is never blank.
  useEffect(() => {
    if (!selected && accounts.data?.length) {
      setSelected(accounts.data[0].id)
    }
  }, [accounts.data, selected])

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['ledger', selected, startDate, endDate, offset],
    queryFn: () =>
      api.get(`/accounting/ledger/${selected}`, {
        start_date: startDate,
        end_date: endDate,
        limit,
        offset,
      }),
    enabled: Boolean(selected),
  })

  if (isError) return <ErrorState error={error} retry={() => refetch()} />

  return (
    <div>
      <PageHeader
        title="General ledger"
        description="Every posted movement on one account, with the balance carried forward."
        actions={
          selected && (
            <Button
              size="sm"
              icon={Download}
              onClick={() =>
                api.download(
                  '/reports/export/general-ledger.csv',
                  `ledger-${data?.account?.code ?? selected}.csv`,
                  { account_id: selected, start_date: startDate, end_date: endDate },
                )
              }
            >
              Export CSV
            </Button>
          )
        }
      >
        <Toolbar>
          <Field label="Account" className="min-w-0 flex-1 sm:max-w-md">
            <Select
              value={selected}
              onChange={(event) => {
                setSelected(event.target.value)
                setOffset(0)
                navigate(`/accounting/ledger/${event.target.value}`, { replace: true })
              }}
            >
              <option value="">Select an account…</option>
              {(accounts.data ?? []).map((account: any) => (
                <option key={account.id} value={account.id}>
                  {account.code} — {account.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="From" className="w-40">
            <Input
              type="date"
              value={startDate}
              onChange={(event) => {
                setStartDate(event.target.value)
                setOffset(0)
              }}
            />
          </Field>
          <Field label="To" className="w-40">
            <Input
              type="date"
              value={endDate}
              onChange={(event) => {
                setEndDate(event.target.value)
                setOffset(0)
              }}
            />
          </Field>
        </Toolbar>
      </PageHeader>

      {data?.account && (
        <div className="mb-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Metric label="Account" value={`${data.account.code} ${data.account.name}`} text />
          <Metric label="Opening balance" value={formatMoney(data.opening_balance)} />
          <Metric label="Closing balance" value={formatMoney(data.closing_balance)} strong />
          <Metric label="Entries" value={String(data.total)} />
        </div>
      )}

      <Card bodyClassName="p-0">
        {!selected ? (
          <EmptyState title="Choose an account" description="Pick an account to see its ledger." />
        ) : isLoading ? (
          <Spinner label="Loading the ledger" />
        ) : !data?.rows?.length ? (
          <EmptyState
            title="No movements in this period"
            description="This account has no posted entries in the selected date range."
          />
        ) : (
          <>
            <div className="scroll-x">
              <table className="w-full min-w-[720px]">
                <thead>
                  <tr className="table-head">
                    <th className="px-3 py-2 text-left">Date</th>
                    <th className="px-3 py-2 text-left">Entry</th>
                    <th className="px-3 py-2 text-left">Description</th>
                    <th className="hidden px-3 py-2 text-left lg:table-cell">Type</th>
                    <th className="px-3 py-2 text-right">Debit</th>
                    <th className="px-3 py-2 text-right">Credit</th>
                    <th className="px-3 py-2 text-right">Balance</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  <tr className="bg-raised/50">
                    <td colSpan={6} className="px-3 py-1.5 text-2xs font-medium text-muted">
                      Opening balance
                    </td>
                    <td className="px-3 py-1.5 text-right text-xs font-semibold tabular">
                      {formatMoney(data.opening_balance)}
                    </td>
                  </tr>
                  {data.rows.map((row: any) => (
                    <tr
                      key={row.line_id}
                      className="cursor-pointer transition-colors hover:bg-raised"
                      onClick={() => navigate(`/accounting/journals/${row.entry_id}`)}
                    >
                      <td className="px-3 py-2 text-sm tabular text-muted">
                        {formatDate(row.date)}
                      </td>
                      <td className="px-3 py-2">
                        <span className="inline-flex items-center gap-1 text-xs font-medium text-brand">
                          {row.entry_number}
                          <ExternalLink className="h-3 w-3" aria-hidden />
                        </span>
                      </td>
                      <td className="px-3 py-2 text-sm">
                        <span className="line-clamp-1">{row.description}</span>
                      </td>
                      <td className="hidden px-3 py-2 lg:table-cell">
                        <Badge tone="neutral">{row.voucher_type?.replace(/_/g, ' ')}</Badge>
                      </td>
                      <td className="px-3 py-2 text-right text-sm tabular">
                        {toPaise(row.debit) ? formatMoney(row.debit) : ''}
                      </td>
                      <td className="px-3 py-2 text-right text-sm tabular">
                        {toPaise(row.credit) ? formatMoney(row.credit) : ''}
                      </td>
                      <td className="px-3 py-2 text-right text-sm font-medium tabular">
                        {formatMoney(row.balance)}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot className="border-t-2 border-border bg-raised">
                  <tr>
                    <td colSpan={6} className="px-3 py-2 text-xs font-semibold text-muted">
                      Closing balance
                    </td>
                    <td className="px-3 py-2 text-right text-sm font-bold tabular text-strong">
                      {formatMoney(data.closing_balance)}
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>
            <Pagination total={data.total} limit={limit} offset={offset} onChange={setOffset} />
          </>
        )}
      </Card>
    </div>
  )
}

function Metric({
  label,
  value,
  strong,
  text,
}: {
  label: string
  value: string
  strong?: boolean
  text?: boolean
}) {
  return (
    <div className="card p-2.5">
      <p className="text-2xs uppercase tracking-wider text-muted">{label}</p>
      <p
        className={`mt-0.5 truncate ${text ? 'text-xs' : 'text-sm tabular'} ${
          strong ? 'font-bold text-strong' : 'font-semibold text-strong'
        }`}
      >
        {value}
      </p>
    </div>
  )
}
