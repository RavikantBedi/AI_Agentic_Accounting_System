/** Vendors, including the TDS and MSME flags that drive automation. */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Building2, Plus } from 'lucide-react'
import { api } from '@/lib/api'
import { useListQuery } from '@/lib/hooks'
import { ListPage } from '@/components/ListPage'
import {
  Alert,
  Amount,
  Badge,
  Button,
  Field,
  Input,
  Modal,
  Select,
  useToast,
  type Column,
} from '@/components/ui'

export default function Vendors() {
  const [creating, setCreating] = useState(false)
  const list = useListQuery<any>('vendors', '/purchases/vendors', { with_balance: true })

  const columns: Column<any>[] = [
    {
      key: 'code',
      header: 'Code',
      render: (row) => <span className="font-mono text-2xs text-subtle">{row.code}</span>,
    },
    {
      key: 'name',
      header: 'Vendor',
      render: (row) => (
        <div className="flex items-center gap-1.5">
          <span className="font-medium text-strong">{row.name}</span>
          {row.is_msme && <Badge tone="info">MSME</Badge>}
        </div>
      ),
    },
    {
      key: 'gstin',
      header: 'GSTIN',
      secondary: true,
      render: (row) =>
        row.gstin ? <span className="font-mono text-2xs">{row.gstin}</span> : '—',
    },
    {
      key: 'tds',
      header: 'TDS',
      secondary: true,
      render: (row) =>
        row.tds_applicable ? (
          <Badge tone="warning">{row.default_tds_section || 'Applicable'}</Badge>
        ) : (
          <span className="text-subtle">—</span>
        ),
    },
    { key: 'state', header: 'State', secondary: true, render: (row) => row.state || '—' },
    {
      key: 'terms',
      header: 'Terms',
      align: 'right',
      secondary: true,
      render: (row) => <span className="tabular text-muted">Net {row.payment_terms_days}</span>,
    },
    {
      key: 'balance',
      header: 'Payable',
      align: 'right',
      render: (row) => <Amount value={row.balance ?? '0'} className="font-medium" />,
    },
  ]

  return (
    <ListPage
      title="Vendors"
      description="TDS-applicable vendors have tax deducted automatically, using dated rules with real thresholds."
      actions={
        <Button variant="primary" size="sm" icon={Plus} onClick={() => setCreating(true)}>
          New vendor
        </Button>
      }
      columns={columns}
      list={list}
      rowKey={(row) => row.id}
      searchPlaceholder="Search by name, code or GSTIN"
      emptyIcon={Building2}
      emptyTitle="No vendors yet"
      emptyDescription="Add a vendor to start recording bills."
      emptyAction={
        <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
          New vendor
        </Button>
      }
    >
      {creating && <VendorForm onClose={() => setCreating(false)} />}
    </ListPage>
  )
}

function VendorForm({ onClose }: { onClose: () => void }) {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    name: '',
    email: '',
    phone: '',
    gstin: '',
    pan: '',
    state: '',
    state_code: '',
    city: '',
    address: '',
    payment_terms_days: 30,
    tds_applicable: false,
    default_tds_section: '',
    is_msme: false,
  })
  const [error, setError] = useState('')

  const create = useMutation({
    mutationFn: () => api.post('/purchases/vendors', form),
    onSuccess: (vendor) => {
      push({
        tone: 'positive',
        title: `${vendor.name} added`,
        description: `Code ${vendor.code}`,
      })
      queryClient.invalidateQueries({ queryKey: ['vendors'] })
      queryClient.invalidateQueries({ queryKey: ['vendors-all'] })
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not save the vendor.'),
  })

  const set = (patch: Partial<typeof form>) => setForm((current) => ({ ...current, ...patch }))

  return (
    <Modal
      open
      onClose={onClose}
      title="New vendor"
      size="lg"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!form.name.trim()}
            onClick={() => {
              setError('')
              create.mutate()
            }}
          >
            Save vendor
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name" required className="sm:col-span-2">
            <Input value={form.name} onChange={(event) => set({ name: event.target.value })} />
          </Field>
          <Field label="Email">
            <Input
              type="email"
              value={form.email}
              onChange={(event) => set({ email: event.target.value })}
            />
          </Field>
          <Field label="Phone">
            <Input value={form.phone} onChange={(event) => set({ phone: event.target.value })} />
          </Field>
          <Field label="GSTIN">
            <Input
              value={form.gstin}
              onChange={(event) => set({ gstin: event.target.value.toUpperCase() })}
              maxLength={15}
              className="font-mono"
            />
          </Field>
          <Field label="PAN" hint="Without a PAN, TDS applies at the higher rate">
            <Input
              value={form.pan}
              onChange={(event) => set({ pan: event.target.value.toUpperCase() })}
              maxLength={10}
              className="font-mono"
            />
          </Field>
          <Field label="State code">
            <Input
              value={form.state_code}
              onChange={(event) => set({ state_code: event.target.value })}
              maxLength={2}
            />
          </Field>
          <Field label="State">
            <Input value={form.state} onChange={(event) => set({ state: event.target.value })} />
          </Field>
          <Field label="Payment terms (days)">
            <Input
              type="number"
              min="0"
              max="365"
              value={form.payment_terms_days}
              onChange={(event) => set({ payment_terms_days: Number(event.target.value) })}
            />
          </Field>
          <Field
            label="Default TDS section"
            hint="Applied only when the configured threshold is crossed"
          >
            <Select
              value={form.default_tds_section}
              onChange={(event) =>
                set({
                  default_tds_section: event.target.value,
                  tds_applicable: Boolean(event.target.value),
                })
              }
            >
              <option value="">Not applicable</option>
              {['194C', '194J', '194I', '194H', '194A', '194Q'].map((section) => (
                <option key={section} value={section}>
                  {section}
                </option>
              ))}
            </Select>
          </Field>
          <label className="flex items-center gap-2 sm:col-span-2">
            <input
              type="checkbox"
              checked={form.is_msme}
              onChange={(event) => set({ is_msme: event.target.checked })}
              className="h-4 w-4 rounded border-border"
            />
            <span className="text-sm text-strong">Registered MSME</span>
            <span className="text-2xs text-muted">— prioritised in the payment run</span>
          </label>
        </div>
      </div>
    </Modal>
  )
}
