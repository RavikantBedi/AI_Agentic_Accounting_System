/** Customers with live receivable balances. */
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Users } from 'lucide-react'
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

export default function Customers() {
  const [creating, setCreating] = useState(false)
  const list = useListQuery<any>('customers', '/sales/customers', { with_balance: true })

  const columns: Column<any>[] = [
    {
      key: 'code',
      header: 'Code',
      render: (row) => <span className="font-mono text-2xs text-subtle">{row.code}</span>,
    },
    {
      key: 'name',
      header: 'Customer',
      render: (row) => <span className="font-medium text-strong">{row.name}</span>,
    },
    {
      key: 'gstin',
      header: 'GSTIN',
      secondary: true,
      render: (row) =>
        row.gstin ? (
          <span className="font-mono text-2xs">{row.gstin}</span>
        ) : (
          <Badge tone="neutral">Unregistered</Badge>
        ),
    },
    { key: 'state', header: 'State', secondary: true, render: (row) => row.state || '—' },
    {
      key: 'contact',
      header: 'Contact',
      secondary: true,
      render: (row) => <span className="text-muted">{row.email || row.phone || '—'}</span>,
    },
    {
      key: 'terms',
      header: 'Terms',
      align: 'right',
      secondary: true,
      render: (row) => <span className="tabular text-muted">Net {row.payment_terms_days}</span>,
    },
    {
      key: 'balance',
      header: 'Outstanding',
      align: 'right',
      render: (row) => <Amount value={row.balance ?? '0'} className="font-medium" />,
    },
  ]

  return (
    <ListPage
      title="Customers"
      description="Balances are computed from open invoices rather than stored on the customer record."
      actions={
        <Button variant="primary" size="sm" icon={Plus} onClick={() => setCreating(true)}>
          New customer
        </Button>
      }
      columns={columns}
      list={list}
      rowKey={(row) => row.id}
      searchPlaceholder="Search by name, code or GSTIN"
      emptyIcon={Users}
      emptyTitle="No customers yet"
      emptyDescription="Add a customer to start invoicing."
      emptyAction={
        <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
          New customer
        </Button>
      }
    >
      {creating && <CustomerForm onClose={() => setCreating(false)} />}
    </ListPage>
  )
}

function CustomerForm({ onClose }: { onClose: () => void }) {
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
    billing_address: '',
    payment_terms_days: 30,
    gst_treatment: 'REGISTERED',
  })
  const [error, setError] = useState('')

  const create = useMutation({
    mutationFn: () => api.post('/sales/customers', form),
    onSuccess: (customer) => {
      push({
        tone: 'positive',
        title: `${customer.name} added`,
        description: `Code ${customer.code}`,
      })
      queryClient.invalidateQueries({ queryKey: ['customers'] })
      queryClient.invalidateQueries({ queryKey: ['customers-all'] })
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not save the customer.'),
  })

  const set = (patch: Partial<typeof form>) => setForm((current) => ({ ...current, ...patch }))

  return (
    <Modal
      open
      onClose={onClose}
      title="New customer"
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
            Save customer
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name" required className="sm:col-span-2">
            <Input
              value={form.name}
              onChange={(event) => set({ name: event.target.value })}
              placeholder="Company or individual"
            />
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
          <Field label="GSTIN" hint="Validated including the checksum digit">
            <Input
              value={form.gstin}
              onChange={(event) => set({ gstin: event.target.value.toUpperCase() })}
              placeholder="27AAPFU0939F1ZV"
              maxLength={15}
              className="font-mono"
            />
          </Field>
          <Field label="PAN">
            <Input
              value={form.pan}
              onChange={(event) => set({ pan: event.target.value.toUpperCase() })}
              maxLength={10}
              className="font-mono"
            />
          </Field>
          <Field label="GST treatment">
            <Select
              value={form.gst_treatment}
              onChange={(event) => set({ gst_treatment: event.target.value })}
            >
              {['REGISTERED', 'UNREGISTERED', 'COMPOSITION', 'SEZ', 'EXPORT', 'OVERSEAS'].map(
                (option) => (
                  <option key={option} value={option}>
                    {option.replace(/_/g, ' ')}
                  </option>
                ),
              )}
            </Select>
          </Field>
          <Field label="State code" hint="Decides CGST + SGST versus IGST">
            <Input
              value={form.state_code}
              onChange={(event) => set({ state_code: event.target.value })}
              placeholder="27"
              maxLength={2}
            />
          </Field>
          <Field label="State">
            <Input value={form.state} onChange={(event) => set({ state: event.target.value })} />
          </Field>
          <Field label="City">
            <Input value={form.city} onChange={(event) => set({ city: event.target.value })} />
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
          <Field label="Billing address" className="sm:col-span-2">
            <Input
              value={form.billing_address}
              onChange={(event) => set({ billing_address: event.target.value })}
            />
          </Field>
        </div>
      </div>
    </Modal>
  )
}
