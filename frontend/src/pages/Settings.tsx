/** Settings: company profile, AI providers, business policy, RBAC and users. */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  CheckCircle2,
  Cpu,
  ShieldCheck,
  XCircle,
} from 'lucide-react'
import { api } from '@/lib/api'
import { formatMoney } from '@/lib/money'
import { useCompany } from '@/lib/hooks'
import {
  Alert,
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
  Tabs,
  cn,
  useToast,
  type Column,
} from '@/components/ui'

const TABS = [
  { id: 'company', label: 'Company' },
  { id: 'ai', label: 'AI providers' },
  { id: 'policy', label: 'Business policy' },
  { id: 'approvals', label: 'Approvals' },
  { id: 'permissions', label: 'Permissions' },
  { id: 'users', label: 'Users' },
]

export default function Settings() {
  const [tab, setTab] = useState('company')

  return (
    <div>
      <PageHeader
        title="Settings"
        description="Business rules are configuration, not code. Everything here is versioned and audited."
      />
      <Tabs tabs={TABS} active={tab} onChange={setTab} className="mb-3" />

      {tab === 'company' && <CompanyTab />}
      {tab === 'ai' && <ProvidersTab />}
      {tab === 'policy' && <PolicyTab />}
      {tab === 'approvals' && <ApprovalsTab />}
      {tab === 'permissions' && <PermissionsTab />}
      {tab === 'users' && <UsersTab />}
    </div>
  )
}

function CompanyTab() {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const { data: me } = useCompany()
  const [form, setForm] = useState<any>(null)

  const company = me?.company
  const current = form ?? company ?? {}

  const save = useMutation({
    mutationFn: () => api.patch('/companies/current', form),
    onSuccess: () => {
      push({ tone: 'positive', title: 'Company profile updated' })
      queryClient.invalidateQueries({ queryKey: ['me'] })
      setForm(null)
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Could not save', description: error?.message }),
  })

  if (!company) return <Spinner />

  const set = (patch: any) => setForm({ ...(form ?? company), ...patch })

  return (
    <Card
      title="Company profile"
      description="Used on invoices, and to decide CGST + SGST versus IGST."
      actions={
        form && (
          <>
            <Button size="sm" onClick={() => setForm(null)}>
              Cancel
            </Button>
            <Button size="sm" variant="primary" loading={save.isPending} onClick={() => save.mutate()}>
              Save changes
            </Button>
          </>
        )
      }
    >
      {company.is_demo && (
        <Alert tone="warning" className="mb-3" title="This is the demo company">
          Every figure comes from generated transactions posted through the real accounting
          engine — but it is not your data.
        </Alert>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Trading name">
          <Input value={current.name ?? ''} onChange={(event) => set({ name: event.target.value })} />
        </Field>
        <Field label="Legal name">
          <Input
            value={current.legal_name ?? ''}
            onChange={(event) => set({ legal_name: event.target.value })}
          />
        </Field>
        <Field label="GSTIN" hint="Validated including the checksum digit">
          <Input
            value={current.gstin ?? ''}
            onChange={(event) => set({ gstin: event.target.value.toUpperCase() })}
            maxLength={15}
            className="font-mono"
          />
        </Field>
        <Field label="PAN">
          <Input
            value={current.pan ?? ''}
            onChange={(event) => set({ pan: event.target.value.toUpperCase() })}
            maxLength={10}
            className="font-mono"
          />
        </Field>
        <Field label="TAN" hint="Required for TDS returns">
          <Input
            value={current.tan ?? ''}
            onChange={(event) => set({ tan: event.target.value.toUpperCase() })}
            maxLength={10}
            className="font-mono"
          />
        </Field>
        <Field label="State code" hint="Drives the intra-state versus inter-state decision">
          <Input
            value={current.state_code ?? ''}
            onChange={(event) => set({ state_code: event.target.value })}
            maxLength={2}
          />
        </Field>
        <Field label="State">
          <Input value={current.state ?? ''} onChange={(event) => set({ state: event.target.value })} />
        </Field>
        <Field label="City">
          <Input value={current.city ?? ''} onChange={(event) => set({ city: event.target.value })} />
        </Field>
        <Field label="Address" className="sm:col-span-2">
          <Input
            value={current.address_line1 ?? ''}
            onChange={(event) => set({ address_line1: event.target.value })}
          />
        </Field>
        <Field label="Email">
          <Input value={current.email ?? ''} onChange={(event) => set({ email: event.target.value })} />
        </Field>
        <Field label="Phone">
          <Input value={current.phone ?? ''} onChange={(event) => set({ phone: event.target.value })} />
        </Field>
      </div>

      <div className="mt-4 grid grid-cols-2 gap-2 border-t border-border pt-3 sm:grid-cols-4">
        <ReadOnly label="Currency" value={company.currency} />
        <ReadOnly
          label="Fiscal year starts"
          value={`Month ${company.fiscal_year_start_month}`}
        />
        <ReadOnly label="Timezone" value={company.timezone} />
        <ReadOnly label="Books begin" value={company.books_begin_on ?? '—'} />
      </div>
    </Card>
  )
}

function ReadOnly({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-2xs uppercase tracking-wider text-muted">{label}</p>
      <p className="mt-0.5 text-sm text-strong">{value}</p>
    </div>
  )
}

function ProvidersTab() {
  const { data, isLoading, refetch, isFetching } = useQuery({
    queryKey: ['providers'],
    queryFn: () => api.get('/ai/providers'),
  })

  if (isLoading) return <Spinner label="Probing providers" />

  return (
    <div className="space-y-3">
      <Alert tone="info" title="The app never requires an API key">
        Providers are tried in order — Gemini, then Grok, then a local Ollama model, then the
        deterministic provider. The deterministic provider is always available and answers from
        real queries against your books, so every feature works with no key at all.
      </Alert>

      <Card
        title="Provider status"
        description="Probed live. Keys are read from the environment and never sent to the browser."
        actions={
          <Button size="sm" loading={isFetching} onClick={() => refetch()}>
            Re-probe
          </Button>
        }
        bodyClassName="p-0"
      >
        <ul className="divide-y divide-border">
          {(data ?? []).map((provider: any) => (
            <li key={provider.name} className="flex items-start gap-3 px-4 py-3">
              <span
                className={cn(
                  'mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg',
                  provider.available
                    ? 'bg-positive-soft text-positive'
                    : 'bg-raised text-subtle',
                )}
              >
                <Cpu className="h-3.5 w-3.5" aria-hidden />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium capitalize text-strong">
                    {provider.name}
                  </span>
                  {provider.available ? (
                    <Badge tone="positive" icon={CheckCircle2}>
                      Available
                    </Badge>
                  ) : (
                    <Badge tone="neutral" icon={XCircle}>
                      Unavailable
                    </Badge>
                  )}
                  {provider.is_local && <Badge tone="info">Local</Badge>}
                  {!provider.requires_key && <Badge tone="neutral">No key needed</Badge>}
                </div>
                <p className="mt-0.5 text-2xs text-muted">{provider.detail}</p>
                {provider.model && (
                  <p className="mt-0.5 font-mono text-2xs text-subtle">{provider.model}</p>
                )}
              </div>
            </li>
          ))}
        </ul>
      </Card>

      <Card title="Configuring a provider" bodyClassName="p-4">
        <p className="mb-2 text-xs text-muted">
          Set these in <code className="rounded bg-raised px-1">backend/.env</code> and restart
          the API. Keys are never stored in the database and never returned by any endpoint.
        </p>
        <pre className="overflow-x-auto rounded-lg bg-raised p-3 text-2xs">
{`# Optional — the app runs fully without any of these
LLM_PROVIDER=auto          # auto | gemini | grok | ollama | mock

GEMINI_API_KEY=
GEMINI_MODEL=gemini-2.0-flash

GROK_API_KEY=
GROK_MODEL=grok-2-latest

# Local and free — no key required
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1`}
        </pre>
      </Card>
    </div>
  )
}

function PolicyTab() {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<any>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['policies'],
    queryFn: () => api.get('/settings/policies'),
  })

  const save = useMutation({
    mutationFn: ({ key, value }: any) => api.put(`/settings/policies/${key}`, { value }),
    onSuccess: () => {
      push({
        tone: 'positive',
        title: 'Policy updated',
        description: 'A new version was written; the previous one is retained and audited.',
      })
      queryClient.invalidateQueries({ queryKey: ['policies'] })
      setEditing(null)
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Could not save the policy', description: error?.message }),
  })

  if (isLoading) return <Spinner label="Loading policy" />

  return (
    <div className="space-y-3">
      <Alert tone="info" title="Policy is data, not code">
        Approval thresholds, AI autonomy limits and matching tolerances all live here. Changing
        one writes a new version — the old value is kept, and the change is recorded in the
        audit trail.
      </Alert>

      <div className="grid gap-3 md:grid-cols-2">
        {(data ?? []).map((policy: any) => (
          <Card
            key={policy.key}
            title={policy.key}
            description={policy.description}
            actions={
              <Button size="sm" onClick={() => setEditing(policy)}>
                Edit
              </Button>
            }
            bodyClassName="p-3"
          >
            <div className="space-y-1">
              {Object.entries(policy.value as Record<string, unknown>).map(([key, value]: [string, unknown]) => (
                <div key={key} className="flex items-baseline justify-between gap-3 text-2xs">
                  <span className="min-w-0 truncate text-muted">{key.replace(/_/g, ' ')}</span>
                  <span className="shrink-0 font-mono text-strong">
                    {typeof value === 'object' ? JSON.stringify(value).slice(0, 24) + '…' : String(value)}
                  </span>
                </div>
              ))}
            </div>
            {policy.version > 0 && (
              <p className="mt-2 text-2xs text-subtle">Version {policy.version}</p>
            )}
          </Card>
        ))}
      </div>

      {editing && (
        <PolicyEditor
          policy={editing}
          onClose={() => setEditing(null)}
          onSave={(value) => save.mutate({ key: editing.key, value })}
          busy={save.isPending}
        />
      )}
    </div>
  )
}

interface PolicyEditorProps {
  policy: { key: string; description: string; value: Record<string, unknown> }
  onClose: () => void
  onSave: (value: Record<string, unknown>) => void
  busy: boolean
}

function PolicyEditor({ policy, onClose, onSave, busy }: PolicyEditorProps) {
  const [text, setText] = useState(JSON.stringify(policy.value, null, 2))
  const [error, setError] = useState('')

  const submit = () => {
    try {
      const parsed = JSON.parse(text)
      setError('')
      onSave(parsed)
    } catch {
      setError('That is not valid JSON. Check for a missing comma or bracket.')
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={`Edit ${policy.key}`}
      description={policy.description}
      size="lg"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={busy} onClick={submit}>
            Save new version
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}
        <Alert tone="warning">
          These values change how the engine behaves immediately. The AI autonomy limits in
          particular decide what may be applied without a human.
        </Alert>
        <textarea
          value={text}
          onChange={(event) => setText(event.target.value)}
          spellCheck={false}
          className="input h-80 resize-y py-2 font-mono text-2xs"
          aria-label="Policy JSON"
        />
      </div>
    </Modal>
  )
}

function ApprovalsTab() {
  const { data, isLoading } = useQuery({
    queryKey: ['approval-rules'],
    queryFn: () => api.get('/settings/approval-rules'),
  })

  const columns: Column<any>[] = [
    { key: 'name', header: 'Rule', render: (row) => <span className="font-medium">{row.name}</span> },
    {
      key: 'entity',
      header: 'Applies to',
      render: (row) => <Badge tone="neutral">{row.entity_type.replace(/_/g, ' ')}</Badge>,
    },
    {
      key: 'range',
      header: 'Amount band',
      align: 'right',
      render: (row) => (
        <span className="tabular text-muted">
          {formatMoney(row.min_amount, { compact: true })} –{' '}
          {row.max_amount ? formatMoney(row.max_amount, { compact: true }) : '∞'}
        </span>
      ),
    },
    {
      key: 'role',
      header: 'Requires',
      render: (row) => <Badge tone="brand">{row.required_role.replace(/_/g, ' ')}</Badge>,
    },
    {
      key: 'auto',
      header: 'Auto-apply',
      align: 'center',
      render: (row) =>
        row.auto_approve ? (
          <Badge tone="warning">Allowed</Badge>
        ) : (
          <Badge tone="neutral">Never</Badge>
        ),
    },
    {
      key: 'confidence',
      header: 'Min confidence',
      align: 'right',
      secondary: true,
      render: (row) => (
        <span className="tabular">{(Number(row.min_ai_confidence) * 100).toFixed(0)}%</span>
      ),
    },
  ]

  return (
    <div className="space-y-3">
      <Alert tone="info" title="How approval routing works">
        The first rule whose amount band contains the transaction decides who must approve it.
        Ledger-affecting proposals and payments are never auto-applied, whatever these rules
        say — that safety rule is applied before any configuration is consulted.
      </Alert>

      <Card bodyClassName="p-0">
        <DataTable
          columns={columns}
          rows={data ?? []}
          loading={isLoading}
          rowKey={(row) => row.id}
          empty={<EmptyState title="No approval rules configured" />}
        />
      </Card>
    </div>
  )
}

function PermissionsTab() {
  const { data, isLoading } = useQuery({
    queryKey: ['permission-matrix'],
    queryFn: () => api.get('/settings/permissions'),
  })

  if (isLoading) return <Spinner label="Loading the permission matrix" />

  const roles = Object.keys(data.roles ?? {})
  const permissions: string[] = data.all_permissions ?? []

  return (
    <div className="space-y-3">
      <Alert tone="info" title="What AI agents can and cannot do">
        {data.note}
        <div className="mt-2 flex flex-wrap gap-1.5">
          {data.ai_forbidden.map((permission: string) => (
            <Badge key={permission} tone="negative">
              {permission.replace(/_/g, ' ')}
            </Badge>
          ))}
        </div>
      </Alert>

      <Card
        title="Role permissions"
        description={`Your role is ${data.your_role}`}
        bodyClassName="p-0"
      >
        <div className="scroll-x">
          <table className="w-full min-w-[720px]">
            <thead>
              <tr className="table-head">
                <th className="px-3 py-2 text-left">Permission</th>
                {roles.map((role) => (
                  <th key={role} className="px-2 py-2 text-center">
                    <span className={cn(role === data.your_role && 'text-brand')}>
                      {role.replace(/_/g, ' ')}
                    </span>
                  </th>
                ))}
                <th className="px-2 py-2 text-center">AI</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {permissions.map((permission) => (
                <tr key={permission} className="hover:bg-raised">
                  <td className="px-3 py-1.5 text-xs text-strong">
                    {permission.replace(/_/g, ' ')}
                  </td>
                  {roles.map((role) => (
                    <td key={role} className="px-2 py-1.5 text-center">
                      {data.roles[role].includes(permission) ? (
                        <CheckCircle2
                          className="mx-auto h-3.5 w-3.5 text-positive"
                          aria-label="allowed"
                        />
                      ) : (
                        <span className="text-subtle" aria-label="not allowed">
                          —
                        </span>
                      )}
                    </td>
                  ))}
                  <td className="px-2 py-1.5 text-center">
                    {data.ai_allowed.includes(permission) ? (
                      <CheckCircle2
                        className="mx-auto h-3.5 w-3.5 text-positive"
                        aria-label="allowed for AI"
                      />
                    ) : (
                      <XCircle
                        className="mx-auto h-3.5 w-3.5 text-negative"
                        aria-label="never allowed for AI"
                      />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}

function UsersTab() {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [inviting, setInviting] = useState(false)

  const { data, isLoading, isError } = useQuery({
    queryKey: ['company-users'],
    queryFn: () => api.get('/companies/current/users'),
    retry: false,
  })

  const changeRole = useMutation({
    mutationFn: ({ userId, role }: any) =>
      api.patch(`/companies/current/users/${userId}/role`, undefined, { role }),
    onSuccess: () => {
      push({ tone: 'positive', title: 'Role updated' })
      queryClient.invalidateQueries({ queryKey: ['company-users'] })
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Could not change the role', description: error?.message }),
  })

  if (isError) {
    return (
      <Card>
        <EmptyState
          icon={ShieldCheck}
          title="You cannot manage users"
          description="Managing users requires the manage_users permission."
        />
      </Card>
    )
  }

  return (
    <div className="space-y-3">
      <Card
        title="Users"
        description="Each user has one role per company."
        actions={
          <Button size="sm" variant="primary" onClick={() => setInviting(true)}>
            Add user
          </Button>
        }
        bodyClassName="p-0"
      >
        {isLoading ? (
          <Spinner />
        ) : (
          <ul className="divide-y divide-border">
            {(data ?? []).map((membership: any) => (
              <li key={membership.id} className="flex items-center gap-3 px-4 py-3">
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-raised text-xs font-semibold text-muted">
                  {(membership.user?.full_name ?? '?').slice(0, 2).toUpperCase()}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-strong">
                    {membership.user?.full_name}
                  </p>
                  <p className="truncate text-2xs text-muted">{membership.user?.email}</p>
                </div>
                <Select
                  value={membership.role}
                  onChange={(event) =>
                    changeRole.mutate({ userId: membership.user_id, role: event.target.value })
                  }
                  className="w-44"
                  aria-label={`Role for ${membership.user?.full_name}`}
                >
                  {[
                    'OWNER',
                    'ADMIN',
                    'ACCOUNTANT',
                    'FINANCE_MANAGER',
                    'SALES_MANAGER',
                    'PURCHASE_MANAGER',
                    'AUDITOR',
                    'VIEWER',
                  ].map((role) => (
                    <option key={role} value={role}>
                      {role.replace(/_/g, ' ')}
                    </option>
                  ))}
                </Select>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {inviting && <InviteDialog onClose={() => setInviting(false)} />}
    </div>
  )
}

function InviteDialog({ onClose }: { onClose: () => void }) {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    email: '',
    full_name: '',
    password: '',
    role: 'VIEWER',
  })
  const [error, setError] = useState('')

  const invite = useMutation({
    mutationFn: () => api.post('/companies/current/users', form),
    onSuccess: () => {
      push({
        tone: 'positive',
        title: 'User added',
        description: 'They will be asked to change their password on first sign-in.',
      })
      queryClient.invalidateQueries({ queryKey: ['company-users'] })
      onClose()
    },
    onError: (caught: any) => setError(caught?.message ?? 'Could not add the user.'),
  })

  return (
    <Modal
      open
      onClose={onClose}
      title="Add user"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={invite.isPending}
            disabled={!form.email || !form.full_name || form.password.length < 8}
            onClick={() => {
              setError('')
              invite.mutate()
            }}
          >
            Add user
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        {error && <Alert tone="negative">{error}</Alert>}
        <Field label="Full name" required>
          <Input
            value={form.full_name}
            onChange={(event) => setForm({ ...form, full_name: event.target.value })}
          />
        </Field>
        <Field label="Email" required>
          <Input
            type="email"
            value={form.email}
            onChange={(event) => setForm({ ...form, email: event.target.value })}
          />
        </Field>
        <Field
          label="Temporary password"
          required
          hint="At least 8 characters, with a letter and a number. They must change it on first sign-in."
        >
          <Input
            type="text"
            value={form.password}
            onChange={(event) => setForm({ ...form, password: event.target.value })}
          />
        </Field>
        <Field label="Role" required>
          <Select
            value={form.role}
            onChange={(event) => setForm({ ...form, role: event.target.value })}
          >
            {[
              'VIEWER',
              'AUDITOR',
              'ACCOUNTANT',
              'FINANCE_MANAGER',
              'SALES_MANAGER',
              'PURCHASE_MANAGER',
              'ADMIN',
              'OWNER',
            ].map((role) => (
              <option key={role} value={role}>
                {role.replace(/_/g, ' ')}
              </option>
            ))}
          </Select>
        </Field>
      </div>
    </Modal>
  )
}
