/** Inventory: stock levels, valuation and movement analysis. */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Package, Plus, RefreshCw, TrendingDown, TrendingUp } from 'lucide-react'
import { api } from '@/lib/api'
import { formatMoney, formatQuantity } from '@/lib/money'
import { useListQuery, useRefreshBooks } from '@/lib/hooks'
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
  Pagination,
  Select,
  StatCard,
  Tabs,
  Toolbar,
  useToast,
  type Column,
} from '@/components/ui'

export default function Inventory() {
  const [tab, setTab] = useState('items')
  const [lowOnly, setLowOnly] = useState(false)
  const [creating, setCreating] = useState(false)
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()

  const summary = useQuery({
    queryKey: ['inventory-summary'],
    queryFn: () => api.get('/inventory/summary'),
  })
  const analysis = useQuery({
    queryKey: ['movement-analysis'],
    queryFn: () => api.get('/inventory/movement-analysis'),
    enabled: tab === 'movement',
  })
  const list = useListQuery<any>('items', '/inventory/items', {
    low_stock_only: lowOnly || undefined,
  })

  const rebuild = useMutation({
    mutationFn: () => api.post('/inventory/rebuild-valuation'),
    onSuccess: (result) => {
      push({
        tone: result.inconsistent > 0 ? 'warning' : 'positive',
        title: 'Valuation verified',
        description: result.message,
      })
      queryClient.invalidateQueries({ queryKey: ['items'] })
      queryClient.invalidateQueries({ queryKey: ['inventory-summary'] })
      refresh()
    },
  })

  const columns: Column<any>[] = [
    {
      key: 'sku',
      header: 'SKU',
      render: (row) => <span className="font-mono text-2xs text-subtle">{row.sku}</span>,
    },
    {
      key: 'name',
      header: 'Item',
      render: (row) => (
        <div className="flex items-center gap-1.5">
          <span className="font-medium text-strong">{row.name}</span>
          {!row.track_inventory && <Badge tone="neutral">Service</Badge>}
          {row.is_low_stock && row.track_inventory && <Badge tone="warning">Low</Badge>}
        </div>
      ),
    },
    {
      key: 'hsn',
      header: 'HSN/SAC',
      secondary: true,
      render: (row) => <span className="font-mono text-2xs">{row.hsn_sac || '—'}</span>,
    },
    {
      key: 'qty',
      header: 'In stock',
      align: 'right',
      render: (row) =>
        row.track_inventory ? (
          <span
            className={`tabular ${
              Number(row.current_qty) <= 0
                ? 'font-medium text-negative'
                : row.is_low_stock
                  ? 'font-medium text-warning'
                  : 'text-strong'
            }`}
          >
            {formatQuantity(row.current_qty, row.uom)}
          </span>
        ) : (
          <span className="text-subtle">—</span>
        ),
    },
    {
      key: 'reorder',
      header: 'Reorder at',
      align: 'right',
      secondary: true,
      render: (row) =>
        row.track_inventory ? (
          <span className="tabular text-muted">{formatQuantity(row.reorder_level)}</span>
        ) : (
          '—'
        ),
    },
    {
      key: 'cost',
      header: 'Avg cost',
      align: 'right',
      secondary: true,
      render: (row) => <Amount value={row.average_cost} className="text-muted" />,
    },
    {
      key: 'selling',
      header: 'Selling price',
      align: 'right',
      secondary: true,
      render: (row) => <Amount value={row.selling_price} />,
    },
    {
      key: 'value',
      header: 'Stock value',
      align: 'right',
      render: (row) => <Amount value={row.stock_value} className="font-semibold" />,
    },
  ]

  return (
    <div>
      <PageHeader
        title="Inventory"
        description="Stock is an append-only ledger of movements; the on-hand figure is derived from it and can be re-proved at any time."
        actions={
          <>
            <Button
              size="sm"
              icon={RefreshCw}
              loading={rebuild.isPending}
              onClick={() => rebuild.mutate()}
            >
              Verify valuation
            </Button>
            <Button variant="primary" size="sm" icon={Plus} onClick={() => setCreating(true)}>
              New item
            </Button>
          </>
        }
      />

      <div className="mb-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard
          label="Stock value"
          value={formatMoney(summary.data?.total_stock_value, { compact: true })}
          sub={`${summary.data?.tracked_items ?? 0} tracked items`}
          icon={Package}
          tone="brand"
          loading={summary.isLoading}
        />
        <StatCard
          label="Low stock"
          value={summary.data?.low_stock_count ?? 0}
          sub="At or below reorder level"
          icon={TrendingDown}
          tone={summary.data?.low_stock_count > 0 ? 'warning' : 'positive'}
          loading={summary.isLoading}
          onClick={() => setLowOnly(true)}
        />
        <StatCard
          label="Out of stock"
          value={summary.data?.out_of_stock_count ?? 0}
          sub="Cannot be fulfilled"
          icon={AlertTriangle}
          tone={summary.data?.out_of_stock_count > 0 ? 'negative' : 'positive'}
          loading={summary.isLoading}
        />
        <StatCard
          label="Total items"
          value={summary.data?.total_items ?? 0}
          sub="Goods and services"
          icon={Package}
          loading={summary.isLoading}
        />
      </div>

      <Tabs
        tabs={[
          { id: 'items', label: 'Items', count: list.total },
          { id: 'movement', label: 'Movement analysis' },
        ]}
        active={tab}
        onChange={setTab}
        className="mb-3"
      />

      {tab === 'items' ? (
        <>
          <Toolbar className="mb-3">
            <div className="min-w-0 flex-1 sm:max-w-xs">
              <Input
                value={list.search}
                onChange={(event) => list.setSearch(event.target.value)}
                placeholder="Search by SKU, name or HSN"
                aria-label="Search items"
              />
            </div>
            <label className="flex items-center gap-2 text-xs text-muted">
              <input
                type="checkbox"
                checked={lowOnly}
                onChange={(event) => setLowOnly(event.target.checked)}
                className="h-4 w-4 rounded border-border"
              />
              Low stock only
            </label>
          </Toolbar>

          <Card bodyClassName="p-0">
            <DataTable
              columns={columns}
              rows={list.items}
              loading={list.isLoading}
              rowKey={(row) => row.id}
              empty={
                <EmptyState
                  icon={Package}
                  title="No items"
                  description="Add an item to start tracking stock."
                  action={
                    <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
                      New item
                    </Button>
                  }
                />
              }
            />
            <Pagination
              total={list.total}
              limit={list.limit}
              offset={list.offset}
              onChange={list.setOffset}
            />
          </Card>
        </>
      ) : (
        <div className="grid gap-3 lg:grid-cols-3">
          <MovementCard
            title="Fast moving"
            description="Highest outward movement in the window"
            icon={TrendingUp}
            rows={analysis.data?.fast_moving ?? []}
            loading={analysis.isLoading}
            metric={(row) => formatQuantity(row.outward_qty)}
            metricLabel="sold"
          />
          <MovementCard
            title="Slow moving"
            description="No recent sales, still in stock"
            icon={TrendingDown}
            rows={analysis.data?.slow_moving ?? []}
            loading={analysis.isLoading}
            metric={(row) => `${row.days_since_sale ?? '—'}d`}
            metricLabel="since last sale"
          />
          <MovementCard
            title="Dead stock"
            description={
              analysis.data
                ? `${formatMoney(analysis.data.dead_stock_value)} tied up`
                : 'Capital tied up in stock that is not selling'
            }
            icon={AlertTriangle}
            rows={analysis.data?.dead_stock ?? []}
            loading={analysis.isLoading}
            metric={(row) => formatMoney(row.stock_value, { compact: true })}
            metricLabel="value"
          />
        </div>
      )}

      {creating && <ItemForm onClose={() => setCreating(false)} />}
    </div>
  )
}

interface MovementCardProps {
  title: string
  description: string
  icon: any
  rows: any[]
  loading: boolean
  metric: (row: any) => string
  metricLabel: string
}

function MovementCard({
  title,
  description,
  icon: Icon,
  rows,
  loading,
  metric,
  metricLabel,
}: MovementCardProps) {
  return (
    <Card title={title} description={description} bodyClassName="p-0">
      {loading ? (
        <div className="space-y-2 p-3">
          {Array.from({ length: 4 }).map((_, index) => (
            <div key={index} className="skeleton h-10" />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <div className="p-4">
          <EmptyState icon={Icon} title="Nothing in this category" />
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {rows.slice(0, 8).map((row: any) => (
            <li key={row.id} className="flex items-start justify-between gap-2 px-3.5 py-2.5">
              <div className="min-w-0">
                <p className="truncate text-xs font-medium text-strong">{row.name}</p>
                <p className="font-mono text-2xs text-subtle">{row.sku}</p>
                {row.reason && <p className="mt-0.5 text-2xs text-muted">{row.reason}</p>}
              </div>
              <div className="shrink-0 text-right">
                <p className="text-xs font-semibold tabular text-strong">{metric(row)}</p>
                <p className="text-2xs text-subtle">{metricLabel}</p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

function ItemForm({ onClose }: { onClose: () => void }) {
  const { push } = useToast()
  const refresh = useRefreshBooks()
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    sku: '',
    name: '',
    item_type: 'GOODS',
    uom: 'NOS',
    hsn_sac: '',
    gst_rate: '18',
    purchase_price: '',
    selling_price: '',
    reorder_level: '0',
    reorder_quantity: '0',
    track_inventory: true,
    opening_qty: '0',
    opening_rate: '',
  })
  const [error, setError] = useState('')

  const create = useMutation({
    mutationFn: () => api.post('/inventory/items', form),
    onSuccess: (item) => {
      push({
        tone: 'positive',
        title: `${item.name} added`,
        description:
          Number(form.opening_qty) > 0
            ? 'Opening stock was posted to the inventory account.'
            : undefined,
      })
      queryClient.invalidateQueries({ queryKey: ['items'] })
      queryClient.invalidateQueries({ queryKey: ['items-all'] })
      queryClient.invalidateQueries({ queryKey: ['inventory-summary'] })
      refresh()
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not save the item.'),
  })

  const set = (patch: Partial<typeof form>) => setForm((current) => ({ ...current, ...patch }))

  return (
    <Modal
      open
      onClose={onClose}
      title="New item"
      size="lg"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={create.isPending}
            disabled={!form.sku.trim() || !form.name.trim()}
            onClick={() => {
              setError('')
              create.mutate()
            }}
          >
            Save item
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="SKU" required>
            <Input
              value={form.sku}
              onChange={(event) => set({ sku: event.target.value.toUpperCase() })}
              className="font-mono"
            />
          </Field>
          <Field label="Name" required>
            <Input value={form.name} onChange={(event) => set({ name: event.target.value })} />
          </Field>
          <Field label="Type">
            <Select
              value={form.item_type}
              onChange={(event) =>
                set({
                  item_type: event.target.value,
                  track_inventory: event.target.value === 'GOODS',
                })
              }
            >
              <option value="GOODS">Goods (tracked)</option>
              <option value="SERVICE">Service (not tracked)</option>
            </Select>
          </Field>
          <Field label="Unit">
            <Select value={form.uom} onChange={(event) => set({ uom: event.target.value })}>
              {['NOS', 'PCS', 'KG', 'LTR', 'MTR', 'BOX', 'SET', 'HRS', 'DAY', 'LIC'].map(
                (unit) => (
                  <option key={unit} value={unit}>
                    {unit}
                  </option>
                ),
              )}
            </Select>
          </Field>
          <Field label="HSN/SAC" hint="Required for GST reporting">
            <Input
              value={form.hsn_sac}
              onChange={(event) => set({ hsn_sac: event.target.value })}
              className="font-mono"
            />
          </Field>
          <Field label="GST rate %">
            <Select
              value={form.gst_rate}
              onChange={(event) => set({ gst_rate: event.target.value })}
            >
              {['0', '5', '12', '18', '28'].map((rate) => (
                <option key={rate} value={rate}>
                  {rate}%
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Purchase price">
            <Input
              type="number"
              step="0.01"
              value={form.purchase_price}
              onChange={(event) => set({ purchase_price: event.target.value })}
              className="tabular"
            />
          </Field>
          <Field label="Selling price">
            <Input
              type="number"
              step="0.01"
              value={form.selling_price}
              onChange={(event) => set({ selling_price: event.target.value })}
              className="tabular"
            />
          </Field>
          {form.track_inventory && (
            <>
              <Field label="Reorder level">
                <Input
                  type="number"
                  step="any"
                  value={form.reorder_level}
                  onChange={(event) => set({ reorder_level: event.target.value })}
                  className="tabular"
                />
              </Field>
              <Field label="Reorder quantity">
                <Input
                  type="number"
                  step="any"
                  value={form.reorder_quantity}
                  onChange={(event) => set({ reorder_quantity: event.target.value })}
                  className="tabular"
                />
              </Field>
              <Field label="Opening quantity" hint="Posts a real opening-stock journal entry">
                <Input
                  type="number"
                  step="any"
                  value={form.opening_qty}
                  onChange={(event) => set({ opening_qty: event.target.value })}
                  className="tabular"
                />
              </Field>
              <Field label="Opening rate">
                <Input
                  type="number"
                  step="0.01"
                  value={form.opening_rate}
                  onChange={(event) => set({ opening_rate: event.target.value })}
                  className="tabular"
                />
              </Field>
            </>
          )}
        </div>
      </div>
    </Modal>
  )
}
