/** TDS: deductions, deposits and the configured, dated rate table. */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Landmark, Receipt } from 'lucide-react'
import { api } from '@/lib/api'
import { fiscalYearStartISO, formatDate, formatMoney, todayISO } from '@/lib/money'
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
  StatCard,
  Tabs,
  useToast,
  type Column,
} from '@/components/ui'

export default function TDS() {
  const [tab, setTab] = useState('entries')
  const [depositing, setDepositing] = useState<any>(null)

  const summary = useQuery({
    queryKey: ['tds-summary'],
    queryFn: () => api.get('/tax/tds/summary', { start_date: fiscalYearStartISO() }),
  })
  const entries = useQuery({
    queryKey: ['tds-entries'],
    queryFn: () => api.get('/tax/tds/entries', { start_date: fiscalYearStartISO(), limit: 200 }),
  })
  const sections = useQuery({
    queryKey: ['tds-sections'],
    queryFn: () => api.get('/tax/tds/sections'),
    enabled: tab === 'sections',
  })

  const entryColumns: Column<any>[] = [
    {
      key: 'date',
      header: 'Date',
      render: (row) => (
        <span className="tabular text-muted">{formatDate(row.deduction_date)}</span>
      ),
    },
    { key: 'vendor', header: 'Vendor', render: (row) => row.vendor_name },
    {
      key: 'section',
      header: 'Section',
      render: (row) => <Badge tone="brand">{row.section}</Badge>,
    },
    {
      key: 'rate',
      header: 'Rate',
      align: 'right',
      secondary: true,
      render: (row) => <span className="tabular">{Number(row.rate).toFixed(2)}%</span>,
    },
    {
      key: 'base',
      header: 'Base amount',
      align: 'right',
      render: (row) => <Amount value={row.base_amount} />,
    },
    {
      key: 'tds',
      header: 'TDS',
      align: 'right',
      render: (row) => <Amount value={row.tds_amount} className="font-semibold" />,
    },
    { key: 'quarter', header: 'Quarter', align: 'center', secondary: true, render: (row) => row.quarter },
    {
      key: 'status',
      header: 'Status',
      render: (row) =>
        row.deposited ? (
          <Badge tone="positive">Deposited</Badge>
        ) : (
          <Badge tone="warning">Pending</Badge>
        ),
    },
    {
      key: 'action',
      header: '',
      align: 'right',
      render: (row) =>
        !row.deposited ? (
          <Button
            size="sm"
            onClick={(event) => {
              event.stopPropagation()
              setDepositing(row)
            }}
          >
            Mark deposited
          </Button>
        ) : (
          <span className="font-mono text-2xs text-subtle">{row.challan_number}</span>
        ),
    },
  ]

  const sectionColumns: Column<any>[] = [
    {
      key: 'section',
      header: 'Section',
      render: (row) => <span className="font-medium text-strong">{row.section}</span>,
    },
    { key: 'name', header: 'Nature of payment', render: (row) => row.name },
    {
      key: 'rate',
      header: 'Rate (with PAN)',
      align: 'right',
      render: (row) => <span className="tabular">{Number(row.rate_with_pan).toFixed(2)}%</span>,
    },
    {
      key: 'no_pan',
      header: 'Without PAN',
      align: 'right',
      secondary: true,
      render: (row) => (
        <span className="tabular text-warning">{Number(row.rate_without_pan).toFixed(2)}%</span>
      ),
    },
    {
      key: 'single',
      header: 'Single-payment threshold',
      align: 'right',
      secondary: true,
      render: (row) => <Amount value={row.single_payment_threshold} className="text-muted" />,
    },
    {
      key: 'annual',
      header: 'Annual threshold',
      align: 'right',
      secondary: true,
      render: (row) => <Amount value={row.annual_threshold} className="text-muted" />,
    },
    {
      key: 'effective',
      header: 'Effective from',
      secondary: true,
      render: (row) => (
        <span className="tabular text-muted">{formatDate(row.effective_from)}</span>
      ),
    },
    {
      key: 'active',
      header: 'Status',
      render: (row) =>
        row.is_active ? <Badge tone="positive">Active</Badge> : <Badge tone="neutral">Inactive</Badge>,
    },
  ]

  return (
    <div>
      <PageHeader
        title="TDS"
        description="Tax deducted at source, computed from dated rules with real thresholds — never from a hardcoded rate."
      />

      <div className="mb-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="Deducted"
          value={formatMoney(summary.data?.total_deducted, { compact: true })}
          sub={`${summary.data?.entry_count ?? 0} deductions this year`}
          icon={Receipt}
          tone="brand"
          loading={summary.isLoading}
        />
        <StatCard
          label="Deposited"
          value={formatMoney(summary.data?.total_deposited, { compact: true })}
          sub="Paid to the government"
          icon={Landmark}
          tone="positive"
          loading={summary.isLoading}
        />
        <StatCard
          label="Pending deposit"
          value={formatMoney(summary.data?.total_pending, { compact: true })}
          sub="Still to be paid"
          icon={Landmark}
          tone={Number(summary.data?.total_pending ?? 0) > 0 ? 'warning' : 'positive'}
          loading={summary.isLoading}
        />
        <StatCard
          label="Sections used"
          value={summary.data?.by_section?.length ?? 0}
          sub="Distinct TDS sections"
          icon={Receipt}
          loading={summary.isLoading}
        />
      </div>

      {summary.data?.by_section?.length > 0 && (
        <Card title="By section" className="mb-3" bodyClassName="p-3">
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {summary.data.by_section.map((section: any) => (
              <div key={section.section} className="rounded-lg bg-raised p-2.5">
                <div className="flex items-center justify-between">
                  <Badge tone="brand">{section.section}</Badge>
                  <span className="text-2xs text-muted">{section.count} entries</span>
                </div>
                <p className="mt-1.5 text-sm font-semibold tabular text-strong">
                  {formatMoney(section.deducted)}
                </p>
                {Number(section.pending) > 0 && (
                  <p className="text-2xs text-warning">
                    {formatMoney(section.pending)} pending deposit
                  </p>
                )}
              </div>
            ))}
          </div>
        </Card>
      )}

      <Tabs
        tabs={[
          { id: 'entries', label: 'Deductions', count: entries.data?.length },
          { id: 'sections', label: 'Configured rates' },
        ]}
        active={tab}
        onChange={setTab}
        className="mb-3"
      />

      {tab === 'sections' && (
        <Alert tone="info" className="mb-3" title="Rates are configuration, not code">
          Each section is stored with an effective-from date and a version. Historical
          transactions keep computing with the rule that applied on their own date, so a rate
          change never rewrites the past.
          {sections.data?.notice && <p className="mt-1">{sections.data.notice}</p>}
        </Alert>
      )}

      <Card bodyClassName="p-0">
        {tab === 'entries' ? (
          <DataTable
            columns={entryColumns}
            rows={entries.data ?? []}
            loading={entries.isLoading}
            rowKey={(row) => row.id}
            empty={
              <EmptyState
                icon={Receipt}
                title="No TDS deducted this year"
                description="TDS is applied automatically on bills from vendors marked TDS-applicable, once a threshold is crossed."
              />
            }
          />
        ) : (
          <DataTable
            columns={sectionColumns}
            rows={sections.data?.sections ?? []}
            loading={sections.isLoading}
            rowKey={(row) => row.id}
            empty={<EmptyState title="No TDS sections configured" />}
          />
        )}
      </Card>

      {depositing && (
        <DepositDialog entry={depositing} onClose={() => setDepositing(null)} />
      )}
    </div>
  )
}

function DepositDialog({ entry, onClose }: { entry: any; onClose: () => void }) {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [challan, setChallan] = useState('')
  const [depositedDate, setDepositedDate] = useState(todayISO())
  const [error, setError] = useState('')

  const markDeposited = useMutation({
    mutationFn: () =>
      api.post(`/tax/tds/entries/${entry.id}/mark-deposited`, undefined, {
        challan_number: challan,
        deposited_date: depositedDate,
      }),
    onSuccess: () => {
      push({
        tone: 'positive',
        title: 'Recorded as deposited',
        description: `${formatMoney(entry.tds_amount)} under ${entry.section}.`,
      })
      queryClient.invalidateQueries({ queryKey: ['tds-entries'] })
      queryClient.invalidateQueries({ queryKey: ['tds-summary'] })
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not record the deposit.'),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title="Record TDS deposit"
      description={`${entry.section} · ${formatMoney(entry.tds_amount)} · ${entry.vendor_name}`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={markDeposited.isPending}
            disabled={!challan.trim()}
            onClick={() => {
              setError('')
              markDeposited.mutate()
            }}
          >
            Record deposit
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}
        <Field label="Challan number" required>
          <Input
            value={challan}
            onChange={(event) => setChallan(event.target.value)}
            className="font-mono"
          />
        </Field>
        <Field label="Deposited on" required>
          <Input
            type="date"
            value={depositedDate}
            onChange={(event) => setDepositedDate(event.target.value)}
          />
        </Field>
      </div>
    </Modal>
  )
}
