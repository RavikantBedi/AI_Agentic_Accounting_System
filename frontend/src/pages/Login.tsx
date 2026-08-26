/** Sign-in, plus the first-run path that loads the demo company. */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Database, Loader2, Sparkles } from 'lucide-react'
import { api, isAuthenticated, login } from '@/lib/api'
import { Alert, Button, Field, Input, useToast } from '@/components/ui'

export default function Login() {
  const navigate = useNavigate()
  const { push } = useToast()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loadingDemo, setLoadingDemo] = useState(false)

  const { data: demo, refetch } = useQuery({
    queryKey: ['demo-status'],
    queryFn: () => api.get('/demo/status'),
    retry: false,
  })

  useEffect(() => {
    if (isAuthenticated()) navigate('/', { replace: true })
  }, [navigate])

  // Pre-fill the demo credentials so evaluation is one click.
  useEffect(() => {
    if (demo?.demo_loaded && !email) {
      setEmail(demo.login_email ?? '')
      setPassword(demo.login_password ?? '')
    }
  }, [demo, email])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setError('')
    setBusy(true)
    try {
      await login(email.trim(), password)
      navigate('/', { replace: true })
    } catch (caught: any) {
      setError(caught?.message ?? 'Sign-in failed.')
    } finally {
      setBusy(false)
    }
  }

  const loadDemo = async () => {
    setLoadingDemo(true)
    setError('')
    try {
      const result = await api.post('/demo/load', undefined, { months: 12 })
      push({
        tone: 'positive',
        title: 'Demo company created',
        description: `${result.invoices} invoices, ${result.bills} bills and ${result.journal_entries} journal entries.`,
      })
      await refetch()
      setEmail(result.login_email)
      setPassword(result.login_password)
    } catch (caught: any) {
      setError(caught?.message ?? 'Could not create the demo company.')
    } finally {
      setLoadingDemo(false)
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center bg-bg p-4">
      <div className="grid w-full max-w-4xl overflow-hidden rounded-2xl border border-border bg-surface shadow-pop lg:grid-cols-2">
        {/* Brand panel */}
        <div className="hidden flex-col justify-between bg-brand p-8 text-brand-fg lg:flex">
          <div className="flex items-center gap-2.5">
            <span className="grid h-9 w-9 place-items-center rounded-lg bg-white/15">
              <Sparkles className="h-5 w-5" aria-hidden />
            </span>
            <span className="text-lg font-semibold">AI Ledger</span>
          </div>

          <div>
            <h1 className="text-2xl font-semibold leading-tight">Your books, managed by AI.</h1>
            <p className="mt-2 text-sm opacity-90">
              From transaction to financial insight — automated, verified, and always under
              your control.
            </p>

            <ul className="mt-6 space-y-2.5 text-sm opacity-90">
              {[
                'A real double-entry engine — debits always equal credits',
                'AI proposes; deterministic rules validate; you approve',
                'Every change recorded in a tamper-evident audit trail',
                'Runs entirely on your machine, with no API key required',
              ].map((line) => (
                <li key={line} className="flex items-start gap-2">
                  <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-current" />
                  {line}
                </li>
              ))}
            </ul>
          </div>

          <p className="text-2xs opacity-70">
            Open-source and local-first. No paid service is required to run it.
          </p>
        </div>

        {/* Form */}
        <div className="p-6 sm:p-8">
          <div className="mb-6 flex items-center gap-2.5 lg:hidden">
            <span className="grid h-9 w-9 place-items-center rounded-lg bg-brand text-brand-fg">
              <Sparkles className="h-5 w-5" aria-hidden />
            </span>
            <span className="text-lg font-semibold">AI Ledger</span>
          </div>

          <h2 className="text-lg font-semibold text-strong">Sign in</h2>
          <p className="mt-0.5 text-xs text-muted">
            Access your company's books and AI finance team.
          </p>

          <form onSubmit={submit} className="mt-5 space-y-3.5">
            <Field label="Email" required>
              <Input
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="you@company.com"
              />
            </Field>

            <Field label="Password" required>
              <Input
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder="••••••••"
              />
            </Field>

            {error && <Alert tone="negative">{error}</Alert>}

            <Button type="submit" variant="primary" size="lg" loading={busy} className="w-full">
              Sign in
            </Button>
          </form>

          <div className="my-5 flex items-center gap-3">
            <span className="h-px flex-1 bg-border" />
            <span className="text-2xs uppercase tracking-wider text-subtle">
              First time here?
            </span>
            <span className="h-px flex-1 bg-border" />
          </div>

          {demo?.demo_loaded ? (
            <Alert tone="info" title="Demo company is loaded">
              Sign in as{' '}
              <code className="rounded bg-surface px-1 py-0.5 font-mono text-2xs">
                {demo.login_email}
              </code>{' '}
              with password{' '}
              <code className="rounded bg-surface px-1 py-0.5 font-mono text-2xs">
                {demo.login_password}
              </code>
              . The credentials above are already filled in.
            </Alert>
          ) : (
            <div className="space-y-3">
              <p className="text-xs text-muted">
                Load <strong className="text-strong">NovaTech Solutions</strong> — a synthetic
                company with twelve months of real transactions, so every screen has
                something meaningful in it. Nothing is faked: each invoice, bill and payment
                is posted through the actual accounting engine.
              </p>
              <Button
                onClick={loadDemo}
                variant="secondary"
                size="lg"
                className="w-full"
                loading={loadingDemo}
                icon={Database}
              >
                {loadingDemo ? 'Generating 12 months of books…' : 'Load demo company'}
              </Button>
              {loadingDemo && (
                <p className="flex items-center justify-center gap-1.5 text-2xs text-subtle">
                  <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
                  Posting invoices, bills, receipts and payments — this takes about 30 seconds.
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
