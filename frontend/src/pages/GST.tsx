/** GST dashboard: output, input, net position, and return preparation. */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileCheck2, Percent } from 'lucide-react'
import { api } from '@/lib/api'
import { formatMoney, toPaise } from '@/lib/money'
import { currentPeriod, periodOptions } from '@/lib/hooks'
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
  Select,
  Spinner,
  StatCard,
  StatusBadge,
  Toolbar,
  useToast,
  type Column,
} from '@/components/ui'

export default function GST() {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [period, setPeriod] = useState(currentPeriod())
  const [filing, setFiling] = useState<any>(null)

  const summary = useQuery({
    queryKey: ['gst-summary', period],
    queryFn: () => api.get('/tax/gst/summary', { period }),
  })
  const returns = useQuery({ queryKey: ['gst-returns'], queryFn: () => api.get('/tax/gst/returns') })

  const prepare = useMutation({
    mutationFn: (type: string) =>
      api.post(`/tax/gst/returns/${type}/prepare`, undefined, { period }),
    onSuccess: (result) => {
      const issues = result.validation_errors?.length ?? 0
      push({
        tone: issues > 0 ? 'warning' : 'positive',
        title: `${result.return_type} prepared for ${result.period}`,
        description:
          issues > 0
            ? `${issues} validation issue(s) must be fixed before filing.`
            : `Taxable ${formatMoney(result.total_taxable_value)}, tax ${formatMoney(result.total_tax)}.`,
      })
      queryClient.invalidateQueries({ queryKey: ['gst-returns'] })
      queryClient.invalidateQueries({ queryKey: ['gst-summary'] })
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Could not prepare', description: error?.message }),
  })

  const data = summary.data

  const columns: Column<any>[] = [
    {
      key: 'type',
      header: 'Return',
      render: (row) => <span className="font-medium text-strong">{row.return_type}</span>,
    },
    { key: 'period', header: 'Period', render: (row) => row.period },
    {
      key: 'taxable',
      header: 'Taxable value',
      align: 'right',
      render: (row) => <Amount value={row.total_taxable_value} />,
    },
    {
      key: 'tax',
      header: 'Tax',
      align: 'right',
      render: (row) => <Amount value={row.total_tax} />,
    },
    {
      key: 'payable',
      header: 'Net payable',
      align: 'right',
      render: (row) => <Amount value={row.net_payable} className="font-semibold" />,
    },
    {
      key: 'issues',
      header: 'Issues',
      align: 'center',
      secondary: true,
      render: (row) =>
        row.validation_errors?.length ? (
          <Badge tone="negative">{row.validation_errors.length}</Badge>
        ) : (
          <Badge tone="positive">None</Badge>
        ),
    },
    { key: 'status', header: 'Status', render: (row) => <StatusBadge status={row.status} /> },
    {
      key: 'action',
      header: '',
      align: 'right',
      render: (row) => (
        <div className="flex justify-end gap-1.5">
          <Button
            size="sm"
            onClick={async (event) => {
              event.stopPropagation()
              const payload = await api.get(`/tax/gst/returns/${row.id}/payload`)
              const blob = new Blob([JSON.stringify(payload.payload, null, 2)], {
                type: 'application/json',
              })
              const url = URL.createObjectURL(blob)
              const anchor = document.createElement('a')
              anchor.href = url
              anchor.download = `${row.return_type}-${row.period}.json`
              anchor.click()
              URL.revokeObjectURL(url)
            }}
          >
            Download
          </Button>
          {row.status !== 'FILED_EXTERNALLY' && (
            <Button
              size="sm"
              variant="primary"
              onClick={(event) => {
                event.stopPropagation()
                setFiling(row)
              }}
            >
              Mark filed
            </Button>
          )}
        </div>
      ),
    },
  ]

  return (
    <div>
      <PageHeader
        title="GST"
        description="Return data is prepared from your ledger. AI Ledger does not file with the GST portal."
        actions={
          <>
            <Button
              size="sm"
              loading={prepare.isPending}
              onClick={() => prepare.mutate('GSTR1')}
            >
              Prepare GSTR-1
            </Button>
            <Button
              size="sm"
              variant="primary"
              loading={prepare.isPending}
              onClick={() => prepare.mutate('GSTR3B')}
            >
              Prepare GSTR-3B
            </Button>
          </>
        }
      >
        <Toolbar>
          <Field label="Period" className="w-52">
            <Select value={period} onChange={(event) => setPeriod(event.target.value)}>
              {periodOptions(18).map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </Field>
        </Toolbar>
      </PageHeader>

      <Alert tone="info" className="mb-3" title="What this does, and what it does not">
        AI Ledger computes your GST position from posted invoices and bills, and produces
        portal-shaped return data you can review and export. Filing happens on the GST portal;
        record the ARN here afterwards so the period is marked complete.
      </Alert>

      <div className="mb-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="Output GST"
          value={formatMoney(data?.output?.total, { compact: true })}
          sub={`${data?.output?.invoice_count ?? 0} invoices`}
          icon={Percent}
          tone="warning"
          loading={summary.isLoading}
        />
        <StatCard
          label="Input GST"
          value={formatMoney(data?.input?.total, { compact: true })}
          sub={`${data?.input?.bill_count ?? 0} bills · ITC eligible`}
          icon={Percent}
          tone="brand"
          loading={summary.isLoading}
        />
        <StatCard
          label="Net payable"
          value={formatMoney(data?.net_payable, { compact: true })}
          sub={
            toPaise(data?.credit_carried_forward ?? 0) > 0
              ? `${formatMoney(data.credit_carried_forward, { compact: true })} credit carried forward`
              : 'Payable this period'
          }
          icon={FileCheck2}
          tone={toPaise(data?.net_payable ?? 0) > 0 ? 'negative' : 'positive'}
          loading={summary.isLoading}
        />
        <StatCard
          label="Return status"
          value={data?.returns?.GSTR3B?.replace(/_/g, ' ') ?? '—'}
          sub={`GSTR-1: ${data?.returns?.GSTR1?.replace(/_/g, ' ') ?? '—'}`}
          icon={FileCheck2}
          loading={summary.isLoading}
        />
      </div>

      <div className="mb-3 grid gap-3 lg:grid-cols-2">
        <Card title="Output tax" description="Collected on sales">
          {summary.isLoading ? (
            <Spinner />
          ) : (
            <Breakdown block={data?.output} />
          )}
        </Card>
        <Card title="Input tax credit" description="Paid on eligible purchases">
          {summary.isLoading ? <Spinner /> : <Breakdown block={data?.input} />}
        </Card>
      </div>

      <Card title="Prepared returns" bodyClassName="p-0">
        <DataTable
          columns={columns}
          rows={returns.data ?? []}
          loading={returns.isLoading}
          rowKey={(row) => row.id}
          empty={
            <EmptyState
              icon={FileCheck2}
              title="No returns prepared yet"
              description="Prepare GSTR-1 or GSTR-3B for the selected period."
            />
          }
        />
      </Card>

      {filing && <MarkFiledDialog gstReturn={filing} onClose={() => setFiling(null)} />}
    </div>
  )
}

function Breakdown({ block }: { block: any }) {
  if (!block) return <EmptyState title="No data for this period" />
  return (
    <dl className="space-y-1.5">
      <Row label="Taxable value" value={block.taxable_value} strong />
      <Row label="CGST" value={block.cgst} />
      <Row label="SGST" value={block.sgst} />
      <Row label="IGST" value={block.igst} />
      {toPaise(block.cess) > 0 && <Row label="Cess" value={block.cess} />}
      <div className="border-t border-border pt-1.5">
        <Row label="Total tax" value={block.total} strong />
      </div>
    </dl>
  )
}

function Row({ label, value, strong }: { label: string; value: any; strong?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className={strong ? 'text-sm text-muted' : 'text-xs text-muted'}>{label}</dt>
      <dd
        className={`tabular ${strong ? 'text-sm font-semibold text-strong' : 'text-xs text-strong'}`}
      >
        {formatMoney(value)}
      </dd>
    </div>
  )
}

function MarkFiledDialog({ gstReturn, onClose }: { gstReturn: any; onClose: () => void }) {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [arn, setArn] = useState('')
  const [error, setError] = useState('')

  const markFiled = useMutation({
    mutationFn: () => api.post(`/tax/gst/returns/${gstReturn.id}/mark-filed`, { arn }),
    onSuccess: () => {
      push({
        tone: 'positive',
        title: 'Recorded as filed',
        description: `${gstReturn.return_type} ${gstReturn.period} marked filed with ARN ${arn}.`,
      })
      queryClient.invalidateQueries({ queryKey: ['gst-returns'] })
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not record the filing.'),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title={`Record filing — ${gstReturn.return_type} ${gstReturn.period}`}
      description="Enter the acknowledgement reference number the GST portal gave you."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={markFiled.isPending}
            disabled={arn.trim().length < 3}
            onClick={() => {
              setError('')
              markFiled.mutate()
            }}
          >
            Record as filed
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}
        <Alert tone="info">
          This records what <em>you</em> filed on the portal. AI Ledger did not submit anything
          and cannot confirm the filing independently.
        </Alert>
        {gstReturn.validation_errors?.length > 0 && (
          <Alert tone="warning" title="This return had validation issues">
            <ul className="mt-1 space-y-0.5">
              {gstReturn.validation_errors.slice(0, 4).map((issue: any, index: number) => (
                <li key={index}>{issue.message}</li>
              ))}
            </ul>
          </Alert>
        )}
        <Field label="ARN" required>
          <Input
            value={arn}
            onChange={(event) => setArn(event.target.value)}
            placeholder="AA270424000000X"
            className="font-mono"
          />
        </Field>
      </div>
    </Modal>
  )
}
