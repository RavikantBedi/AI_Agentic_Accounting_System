/**
 * e-Invoice and e-Way Bill.
 *
 * This screen is deliberately explicit about running in demo mode: identifiers
 * generated here are locally produced and carry no legal weight. Nothing on
 * this page ever claims a government confirmation it does not have.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileCode2, Truck } from 'lucide-react'
import { api } from '@/lib/api'
import { formatDate, formatDateTime, formatMoney } from '@/lib/money'
import { useListQuery } from '@/lib/hooks'
import {
  Alert,
  Amount,
  Button,
  Card,
  DataTable,
  EmptyState,
  Field,
  Input,
  Modal,
  PageHeader,
  Select,
  StatusBadge,
  Tabs,
  useToast,
  type Column,
} from '@/components/ui'

export default function EDocs() {
  const [tab, setTab] = useState('einvoice')
  const [target, setTarget] = useState<any>(null)

  const providers = useQuery({
    queryKey: ['tax-providers'],
    queryFn: () => api.get('/tax/providers'),
  })
  const invoices = useListQuery<any>('einvoice-invoices', '/sales/invoices', {
    status: 'SENT',
  })

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
      key: 'total',
      header: 'Value',
      align: 'right',
      render: (row) => <Amount value={row.total} />,
    },
    {
      key: 'status',
      header: tab === 'einvoice' ? 'e-Invoice' : 'e-Way bill',
      render: (row) => (
        <StatusBadge status={tab === 'einvoice' ? row.einvoice_status : row.ewaybill_status} />
      ),
    },
    {
      key: 'action',
      header: '',
      align: 'right',
      render: (row) => (
        <Button
          size="sm"
          onClick={(event) => {
            event.stopPropagation()
            setTarget(row)
          }}
        >
          {(tab === 'einvoice' ? row.einvoice_status : row.ewaybill_status) === 'ACKNOWLEDGED'
            ? 'View'
            : 'Generate'}
        </Button>
      ),
    },
  ]

  const mode =
    tab === 'einvoice' ? providers.data?.einvoice : providers.data?.ewaybill

  return (
    <div>
      <PageHeader
        title="e-Invoice & e-Way Bill"
        description="Payloads are built to the government schema and validated locally before submission."
      />

      {mode && (
        <Alert
          tone={mode.mode === 'MOCK' ? 'warning' : 'positive'}
          className="mb-3"
          title={
            mode.mode === 'MOCK'
              ? 'Demo mode — identifiers have no legal validity'
              : 'Live mode — credentials are configured'
          }
        >
          {mode.notice}
          {mode.mode === 'MOCK' && (
            <p className="mt-1">
              Every identifier produced here is prefixed <code>DEMO</code> and is stored with
              its provider mode, so a demo acknowledgement can never be mistaken for a real
              one. To connect a real IRP, set the e-invoicing credentials in your environment.
            </p>
          )}
        </Alert>
      )}

      <Tabs
        tabs={[
          { id: 'einvoice', label: 'e-Invoice' },
          { id: 'ewaybill', label: 'e-Way Bill' },
        ]}
        active={tab}
        onChange={setTab}
        className="mb-3"
      />

      <Card
        title={tab === 'einvoice' ? 'Invoices eligible for IRN' : 'Invoices needing an e-way bill'}
        description="Posted invoices that have been sent to the customer"
        bodyClassName="p-0"
      >
        <DataTable
          columns={columns}
          rows={invoices.items}
          loading={invoices.isLoading}
          rowKey={(row) => row.id}
          empty={
            <EmptyState
              icon={tab === 'einvoice' ? FileCode2 : Truck}
              title="No eligible invoices"
              description="Create and post an invoice first."
            />
          }
        />
      </Card>

      {target && tab === 'einvoice' && (
        <EInvoiceDialog invoice={target} onClose={() => setTarget(null)} />
      )}
      {target && tab === 'ewaybill' && (
        <EWayBillDialog invoice={target} onClose={() => setTarget(null)} />
      )}
    </div>
  )
}

function EInvoiceDialog({ invoice, onClose }: { invoice: any; onClose: () => void }) {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [result, setResult] = useState<any>(null)

  const existing = useQuery({
    queryKey: ['einvoice', invoice.id],
    queryFn: () => api.get(`/tax/einvoice/${invoice.id}`),
    retry: false,
  })

  const validation = useQuery({
    queryKey: ['einvoice-validate', invoice.id],
    queryFn: () => api.post('/tax/einvoice/validate', { invoice_id: invoice.id }),
  })

  const generate = useMutation({
    mutationFn: () => api.post('/tax/einvoice/generate', { invoice_id: invoice.id }),
    onSuccess: (data) => {
      setResult(data)
      push({
        tone: data.success ? 'positive' : 'negative',
        title: data.success ? 'e-Invoice generated (demo)' : 'Generation failed',
        description: data.success ? data.disclaimer : data.error_message,
      })
      queryClient.invalidateQueries({ queryKey: ['einvoice-invoices'] })
    },
  })

  const record = result ?? (existing.data?.irn ? existing.data : null)

  return (
    <Modal
      open
      onClose={onClose}
      title={`e-Invoice — ${invoice.number}`}
      description={`${invoice.customer_name} · ${formatMoney(invoice.total)}`}
      size="lg"
      footer={
        <>
          <Button onClick={onClose}>Close</Button>
          {!record && (
            <Button
              variant="primary"
              loading={generate.isPending}
              disabled={!validation.data?.valid}
              onClick={() => generate.mutate()}
            >
              Generate IRN
            </Button>
          )}
        </>
      }
    >
      <div className="space-y-3">
        {validation.data && !validation.data.valid && (
          <Alert tone="negative" title="This invoice would be rejected by the IRP">
            <ul className="mt-1 space-y-0.5">
              {validation.data.errors.map((issue: string, index: number) => (
                <li key={index}>{issue}</li>
              ))}
            </ul>
          </Alert>
        )}

        {validation.data?.valid && !record && (
          <Alert tone="positive" title="Passes IRP validation">
            All mandatory fields are present and the totals reconcile. The same checks the
            portal performs were run locally first.
          </Alert>
        )}

        {record && (
          <>
            <Alert tone={record.is_government_confirmed ? 'positive' : 'warning'}>
              {record.disclaimer ||
                (record.is_government_confirmed
                  ? 'Registered with the Invoice Registration Portal.'
                  : 'DEMO MODE — generated locally, with no legal standing.')}
            </Alert>
            <div className="space-y-2 rounded-lg bg-raised p-3">
              <KeyValue label="IRN" value={record.data?.Irn ?? record.irn} mono />
              <KeyValue label="Acknowledgement no." value={record.data?.AckNo ?? record.ack_no} mono />
              <KeyValue
                label="Acknowledged at"
                value={formatDateTime(record.data?.AckDt ?? record.ack_date)}
              />
              <KeyValue label="Provider" value={`${record.provider ?? 'mock'} (${record.mode ?? record.provider_mode})`} />
            </div>
          </>
        )}

        {validation.data?.payload && (
          <details className="rounded-lg border border-border">
            <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-strong">
              View the IRP payload
            </summary>
            <pre className="max-h-64 overflow-auto border-t border-border bg-raised p-3 text-2xs">
              {JSON.stringify(validation.data.payload, null, 2)}
            </pre>
          </details>
        )}
      </div>
    </Modal>
  )
}

function EWayBillDialog({ invoice, onClose }: { invoice: any; onClose: () => void }) {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    distance_km: 100,
    vehicle_number: '',
    transporter_name: '',
    transport_mode: 'ROAD',
    from_place: '',
    to_place: '',
  })
  const [result, setResult] = useState<any>(null)

  const existing = useQuery({
    queryKey: ['ewaybill', invoice.id],
    queryFn: () => api.get(`/tax/ewaybill/${invoice.id}`),
    retry: false,
  })

  const generate = useMutation({
    mutationFn: () =>
      api.post('/tax/ewaybill/generate', { invoice_id: invoice.id, ...form }),
    onSuccess: (data) => {
      setResult(data)
      push({
        tone: data.success ? 'positive' : 'negative',
        title: data.success ? 'e-Way bill generated (demo)' : 'Generation failed',
        description: data.success ? data.disclaimer : data.error_message,
      })
      queryClient.invalidateQueries({ queryKey: ['einvoice-invoices'] })
    },
  })

  const record = result ?? (existing.data?.ewb_number ? existing.data : null)
  const set = (patch: Partial<typeof form>) => setForm((current) => ({ ...current, ...patch }))

  return (
    <Modal
      open
      onClose={onClose}
      title={`e-Way Bill — ${invoice.number}`}
      description={`${invoice.customer_name} · ${formatMoney(invoice.total)}`}
      size="lg"
      footer={
        <>
          <Button onClick={onClose}>Close</Button>
          {!record && (
            <Button
              variant="primary"
              loading={generate.isPending}
              disabled={!form.distance_km || (!form.vehicle_number && !form.transporter_name)}
              onClick={() => generate.mutate()}
            >
              Generate e-way bill
            </Button>
          )}
        </>
      }
    >
      <div className="space-y-3">
        {record ? (
          <>
            <Alert tone={record.is_government_confirmed ? 'positive' : 'warning'}>
              {record.disclaimer ||
                'DEMO MODE — generated locally, with no legal standing.'}
            </Alert>
            <div className="space-y-2 rounded-lg bg-raised p-3">
              <KeyValue
                label="e-Way bill number"
                value={record.data?.ewayBillNo ?? record.ewb_number}
                mono
              />
              <KeyValue label="Generated" value={record.data?.ewayBillDate ?? formatDateTime(record.ewb_date)} />
              <KeyValue label="Valid until" value={record.data?.validUpto ?? formatDateTime(record.valid_until)} />
              <KeyValue label="Vehicle" value={record.vehicle_number || form.vehicle_number} mono />
            </div>
          </>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Distance (km)" required hint="Determines the validity period">
              <Input
                type="number"
                min="1"
                max="4000"
                value={form.distance_km}
                onChange={(event) => set({ distance_km: Number(event.target.value) })}
                className="tabular"
              />
            </Field>
            <Field label="Transport mode">
              <Select
                value={form.transport_mode}
                onChange={(event) => set({ transport_mode: event.target.value })}
              >
                {['ROAD', 'RAIL', 'AIR', 'SHIP'].map((mode) => (
                  <option key={mode} value={mode}>
                    {mode}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Vehicle number" hint="Required unless a transporter is named">
              <Input
                value={form.vehicle_number}
                onChange={(event) => set({ vehicle_number: event.target.value.toUpperCase() })}
                placeholder="MH01AB1234"
                className="font-mono"
              />
            </Field>
            <Field label="Transporter">
              <Input
                value={form.transporter_name}
                onChange={(event) => set({ transporter_name: event.target.value })}
              />
            </Field>
            <Field label="Dispatch from">
              <Input
                value={form.from_place}
                onChange={(event) => set({ from_place: event.target.value })}
              />
            </Field>
            <Field label="Deliver to">
              <Input
                value={form.to_place}
                onChange={(event) => set({ to_place: event.target.value })}
              />
            </Field>
          </div>
        )}
      </div>
    </Modal>
  )
}

function KeyValue({ label, value, mono }: { label: string; value?: string; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="shrink-0 text-2xs uppercase tracking-wider text-muted">{label}</span>
      <span className={`min-w-0 break-all text-right text-xs text-strong ${mono ? 'font-mono' : ''}`}>
        {value || '—'}
      </span>
    </div>
  )
}
