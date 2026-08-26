/** AI Command Center — ask your finance team. */
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowUp,
  Bot,
  CheckCircle2,
  Cpu,
  FileSearch,
  Lightbulb,
  Loader2,
  Play,
  Sparkles,
  User,
  Workflow,
} from 'lucide-react'
import { api } from '@/lib/api'
import {
  Alert,
  Amount,
  Badge,
  Button,
  Card,
  ConfidenceBadge,
  PageHeader,
  StatusBadge,
  useToast,
} from '@/components/ui'

interface Turn {
  role: 'user' | 'assistant'
  question?: string
  answer?: any
  pending?: boolean
}

export default function CommandCenter() {
  const { push } = useToast()
  const queryClient = useQueryClient()
  const [turns, setTurns] = useState<Turn[]>([])
  const [input, setInput] = useState('')
  const [conversationId, setConversationId] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  const prompts = useQuery({
    queryKey: ['suggested-prompts'],
    queryFn: () => api.get('/ai/suggested-prompts'),
  })
  const providers = useQuery({ queryKey: ['providers'], queryFn: () => api.get('/ai/providers') })
  const workflows = useQuery({ queryKey: ['workflows'], queryFn: () => api.get('/ai/workflows') })

  const ask = useMutation({
    mutationFn: (question: string) =>
      api.post('/ai/ask', { question, conversation_id: conversationId }),
    onMutate: (question) => {
      setTurns((current) => [
        ...current,
        { role: 'user', question },
        { role: 'assistant', pending: true },
      ])
    },
    onSuccess: (data) => {
      setConversationId(data.conversation_id)
      setTurns((current) => [
        ...current.slice(0, -1),
        { role: 'assistant', answer: data },
      ])
    },
    onError: (error: any) => {
      setTurns((current) => [
        ...current.slice(0, -1),
        {
          role: 'assistant',
          answer: {
            answer: error?.message ?? 'The request failed.',
            confidence: '0',
            evidence: [],
            insufficient_evidence: true,
          },
        },
      ])
    },
  })

  const runWorkflow = useMutation({
    mutationFn: (key: string) => api.post(`/ai/workflows/${key}/run`, { params: {} }),
    onSuccess: (data) => {
      push({
        tone: data.status === 'FAILED' ? 'negative' : 'positive',
        title: data.name,
        description: data.summary,
      })
      queryClient.invalidateQueries()
      setTurns((current) => [
        ...current,
        { role: 'user', question: `Run workflow: ${data.name}` },
        {
          role: 'assistant',
          answer: {
            answer: data.summary,
            workflow: data,
            confidence: '0.95',
            evidence: [],
          },
        },
      ])
    },
    onError: (error: any) =>
      push({ tone: 'negative', title: 'Workflow failed', description: error?.message }),
  })

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [turns])

  const submit = (event?: React.FormEvent) => {
    event?.preventDefault()
    const question = input.trim()
    if (!question || ask.isPending) return
    setInput('')
    ask.mutate(question)
  }

  const activeProvider = (providers.data ?? []).find((provider: any) => provider.available)

  return (
    <div className="mx-auto flex h-full max-w-6xl flex-col">
      <PageHeader
        title="Ask your finance team"
        description="Questions are answered from your ledger through typed queries — never free-form SQL, and never from memory."
        actions={
          activeProvider && (
            <Badge tone={activeProvider.name === 'mock' ? 'neutral' : 'brand'} icon={Cpu}>
              {activeProvider.name === 'mock' ? 'Deterministic mode' : activeProvider.name}
            </Badge>
          )
        }
      />

      {activeProvider?.name === 'mock' && (
        <Alert tone="info" className="mb-3" title="Running without a language model">
          Answers come from real queries against your books and are phrased deterministically.
          Configure Gemini, Grok or a local Ollama model in Settings for more fluent wording —
          the underlying numbers do not change.
        </Alert>
      )}

      <div className="grid min-h-0 flex-1 gap-3 lg:grid-cols-[1fr_260px]">
        <Card className="flex min-h-0 flex-col" bodyClassName="flex min-h-0 flex-1 flex-col p-0">
          <div ref={scrollRef} className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
            {turns.length === 0 ? (
              <Welcome
                prompts={prompts.data?.prompts ?? []}
                onPick={(prompt) => ask.mutate(prompt)}
              />
            ) : (
              turns.map((turn, index) => <TurnView key={index} turn={turn} />)
            )}
          </div>

          <form onSubmit={submit} className="border-t border-border p-3">
            <div className="flex items-end gap-2 rounded-xl border border-border bg-raised p-1.5 focus-within:border-brand/50">
              <textarea
                value={input}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey) submit(event as any)
                }}
                rows={1}
                placeholder="Why is profit down this month?  ·  Who owes us the most?  ·  Find duplicate bills."
                aria-label="Ask a question about your books"
                className="max-h-32 min-h-[2.25rem] flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none placeholder:text-subtle"
              />
              <Button
                type="submit"
                variant="primary"
                size="sm"
                className="h-8 w-8 shrink-0 p-0"
                disabled={!input.trim() || ask.isPending}
                aria-label="Send"
              >
                {ask.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                ) : (
                  <ArrowUp className="h-4 w-4" aria-hidden />
                )}
              </Button>
            </div>
            <p className="mt-1.5 px-1 text-2xs text-subtle">
              The assistant reads your books but cannot change them. Anything it proposes needs
              your approval.
            </p>
          </form>
        </Card>

        <aside className="space-y-3">
          <Card title="Workflows" description="Chained agent runs" bodyClassName="p-2">
            <div className="space-y-1">
              {(workflows.data?.workflows ?? []).map((workflow: any) => (
                <button
                  key={workflow.key}
                  onClick={() => runWorkflow.mutate(workflow.key)}
                  disabled={runWorkflow.isPending}
                  className="w-full rounded-lg p-2 text-left transition-colors hover:bg-raised disabled:opacity-50"
                >
                  <div className="flex items-center gap-1.5">
                    {runWorkflow.isPending && runWorkflow.variables === workflow.key ? (
                      <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-brand" aria-hidden />
                    ) : (
                      <Play className="h-3.5 w-3.5 shrink-0 text-brand" aria-hidden />
                    )}
                    <span className="text-xs font-medium text-strong">{workflow.name}</span>
                  </div>
                  <p className="mt-0.5 line-clamp-2 pl-5 text-2xs text-muted">
                    {workflow.description}
                  </p>
                  <p className="mt-1 pl-5 text-2xs text-subtle">
                    {workflow.steps.length} agents
                  </p>
                </button>
              ))}
            </div>
          </Card>

          <Card title="How this works" bodyClassName="p-3">
            <ol className="space-y-2 text-2xs text-muted">
              {[
                ['Intent', 'Your question is matched to a typed query tool.'],
                ['Query', 'The tool runs a parameterised query — no SQL is generated.'],
                ['Ground', 'Every record cited is re-read from the database.'],
                ['Answer', 'Figures come from the ledger, with evidence attached.'],
              ].map(([step, detail], index) => (
                <li key={step} className="flex gap-2">
                  <span className="grid h-4 w-4 shrink-0 place-items-center rounded-full bg-brand-soft text-2xs font-bold text-brand">
                    {index + 1}
                  </span>
                  <span>
                    <strong className="text-strong">{step}.</strong> {detail}
                  </span>
                </li>
              ))}
            </ol>
          </Card>
        </aside>
      </div>
    </div>
  )
}

function Welcome({ prompts, onPick }: { prompts: string[]; onPick: (prompt: string) => void }) {
  return (
    <div className="flex h-full flex-col items-center justify-center py-8 text-center">
      <span className="grid h-12 w-12 place-items-center rounded-xl bg-brand-soft">
        <Sparkles className="h-6 w-6 text-brand" aria-hidden />
      </span>
      <h2 className="mt-3 text-base font-semibold text-strong">
        What would you like to know?
      </h2>
      <p className="mt-1 max-w-md text-xs text-muted">
        Every answer is computed from your posted ledger. If the books don't contain the
        answer, you'll be told that rather than given a guess.
      </p>

      <div className="mt-5 grid w-full max-w-2xl gap-1.5 sm:grid-cols-2">
        {prompts.slice(0, 10).map((prompt) => (
          <button
            key={prompt}
            onClick={() => onPick(prompt)}
            className="flex items-center gap-2 rounded-lg border border-border bg-surface px-3 py-2 text-left text-xs text-muted transition-colors hover:border-brand/40 hover:text-strong"
          >
            <Lightbulb className="h-3.5 w-3.5 shrink-0 text-subtle" aria-hidden />
            <span className="truncate">{prompt}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function TurnView({ turn }: { turn: Turn }) {
  if (turn.role === 'user') {
    return (
      <div className="flex justify-end gap-2.5">
        <div className="max-w-[85%] rounded-xl rounded-br-sm bg-brand px-3.5 py-2 text-sm text-brand-fg">
          {turn.question}
        </div>
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-raised">
          <User className="h-3.5 w-3.5 text-muted" aria-hidden />
        </span>
      </div>
    )
  }

  if (turn.pending) {
    return (
      <div className="flex gap-2.5">
        <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-brand-soft">
          <Bot className="h-3.5 w-3.5 text-brand" aria-hidden />
        </span>
        <div className="flex items-center gap-2 rounded-xl rounded-bl-sm bg-raised px-3.5 py-2.5 text-sm text-muted">
          <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
          Querying the ledger…
        </div>
      </div>
    )
  }

  const answer = turn.answer ?? {}

  return (
    <div className="flex gap-2.5">
      <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-brand-soft">
        <Bot className="h-3.5 w-3.5 text-brand" aria-hidden />
      </span>

      <div className="min-w-0 flex-1 space-y-2.5">
        <div className="rounded-xl rounded-bl-sm border border-border bg-surface px-3.5 py-3">
          <p className="whitespace-pre-wrap text-sm leading-relaxed text-strong">
            {answer.answer}
          </p>

          {answer.key_figures?.length > 0 && (
            <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {answer.key_figures.map((figure: any, index: number) => (
                <div key={index} className="rounded-lg bg-raised px-2.5 py-1.5">
                  <p className="text-2xs uppercase tracking-wider text-muted">{figure.label}</p>
                  <p className="text-sm font-semibold tabular text-strong">{figure.value}</p>
                </div>
              ))}
            </div>
          )}

          {answer.workflow && <WorkflowResult workflow={answer.workflow} />}

          {answer.suggested_actions?.length > 0 && (
            <div className="mt-3 border-t border-border pt-2.5">
              <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-muted">
                Suggested next steps
              </p>
              <ul className="space-y-1">
                {answer.suggested_actions.map((action: string, index: number) => (
                  <li key={index} className="flex items-start gap-1.5 text-xs text-muted">
                    <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0 text-brand" aria-hidden />
                    {action}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {answer.grounding_warning && (
            <Alert tone="warning" className="mt-3">
              {answer.grounding_warning}
            </Alert>
          )}

          <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-border pt-2.5">
            {!answer.insufficient_evidence && answer.confidence && (
              <ConfidenceBadge value={answer.confidence} label={answer.confidence_label} />
            )}
            {answer.insufficient_evidence && <Badge tone="warning">Insufficient evidence</Badge>}
            {answer.tools_used?.map((tool: string) => (
              <Badge key={tool} tone="neutral" icon={FileSearch}>
                {tool.replace(/_/g, ' ')}
              </Badge>
            ))}
            {answer.provider && (
              <span className="ml-auto text-2xs text-subtle">
                {answer.used_llm ? `via ${answer.provider}` : 'computed'}
              </span>
            )}
          </div>
        </div>

        {answer.evidence?.length > 0 && <Evidence items={answer.evidence} />}
      </div>
    </div>
  )
}

function WorkflowResult({ workflow }: { workflow: any }) {
  return (
    <div className="mt-3 space-y-1.5 border-t border-border pt-2.5">
      <p className="flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-wider text-muted">
        <Workflow className="h-3 w-3" aria-hidden />
        Agent steps
      </p>
      {workflow.steps.map((step: any) => (
        <div key={step.agent_kind} className="flex items-start gap-2 rounded-lg bg-raised p-2">
          <StatusBadge status={step.status} />
          <div className="min-w-0 flex-1">
            <p className="text-xs font-medium text-strong">{step.name}</p>
            <p className="mt-0.5 text-2xs text-muted">{step.summary}</p>
          </div>
          {step.proposals > 0 && (
            <Badge tone="brand" className="shrink-0">
              {step.proposals} proposals
            </Badge>
          )}
        </div>
      ))}
    </div>
  )
}

function Evidence({ items }: { items: any[] }) {
  const [expanded, setExpanded] = useState(false)
  const visible = expanded ? items : items.slice(0, 4)

  const linkFor = (item: any) => {
    const map: Record<string, string> = {
      invoice: '/sales/invoices',
      bill: '/purchases/bills',
      customer: '/sales/customers',
      vendor: '/purchases/vendors',
      account: '/accounting/ledger',
      bank_transaction: '/banking/transactions',
      journal_line: '/accounting/journals',
      anomaly: '/exceptions',
      exception: '/exceptions',
    }
    const base = map[item.type]
    return base && item.id ? `${base}/${item.id}` : base || '#'
  }

  return (
    <div className="rounded-xl border border-border bg-raised/50 p-2.5">
      <p className="mb-1.5 text-2xs font-semibold uppercase tracking-wider text-muted">
        Source records ({items.length})
      </p>
      <div className="space-y-1">
        {visible.map((item, index) => (
          <Link
            key={index}
            to={linkFor(item)}
            className="flex items-center gap-2 rounded-lg bg-surface px-2.5 py-1.5 transition-colors hover:bg-brand-soft/40"
          >
            <Badge tone="neutral" className="shrink-0">
              {item.type?.replace(/_/g, ' ')}
            </Badge>
            <span className="min-w-0 flex-1 truncate text-xs text-strong">{item.label}</span>
            {item.date && <span className="shrink-0 text-2xs text-subtle">{item.date}</span>}
            {item.amount && (
              <Amount value={item.amount} className="shrink-0 text-xs font-medium" />
            )}
          </Link>
        ))}
      </div>
      {items.length > 4 && (
        <button
          onClick={() => setExpanded((value) => !value)}
          className="mt-1.5 text-2xs font-medium text-brand hover:underline"
        >
          {expanded ? 'Show less' : `Show all ${items.length}`}
        </button>
      )}
    </div>
  )
}
