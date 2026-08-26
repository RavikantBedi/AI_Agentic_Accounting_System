"""The AI Finance Copilot.

Query architecture, strictly:

    question -> intent parser -> typed tool call(s) -> real data -> explanation

The model never sees a database connection and never writes SQL. It selects a
tool by name from a fixed registry; the tool runs a parameterised query. The
answer is then grounded: every record id it cites must be one that was actually
returned by a tool, or the answer is replaced with an honest non-answer.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.ai.agents.base import AgentResult, BaseAgent
from app.ai.grounding import collect_allowed_ids, ground_answer, insufficient_evidence
from app.ai.tools import REGISTRY, call_tool, tool_specs
from app.enums import AgentKind, AgentRunStatus
from app.models import Conversation, ConversationMessage
from app.models.base import utcnow
from app.money import ZERO, D, fmt_inr, money

# A question can name more than one subject. When it does, the primary intent's
# tools are supplemented with these so the model is given every topic the user
# actually asked about, rather than answering half the question.
_SECONDARY_TOPICS: list[tuple[str, list[str], list[tuple[str, dict]]]] = [
    ("cash_position", ["cash", "bank balance"], [("get_cash_position", {})]),
    ("payables", ["payable", "owe our vendors", "owe suppliers", "vendor", "supplier", "bills due"],
     [("get_payables_aging", {}), ("list_outstanding_bills", {"limit": 10})]),
    ("receivables", ["receivable", "customers owe", "owed to us", "collect", "chase", "unpaid invoice"],
     [("get_receivables_aging", {}), ("get_top_customers_by_balance", {"limit": 10})]),
    ("profit_loss", ["profit", "margin", "revenue", "expenses"], [("get_profit_and_loss", {})]),
    ("inventory", ["stock", "inventory"], [("get_inventory_summary", {})]),
    ("gst", ["gst", "tax"], [("get_gst_summary", {})]),
    ("reconciliation", ["reconcil", "unmatched", "unreconciled"],
     [("get_reconciliation_status", {})]),
    ("anomalies", ["anomaly", "anomalies", "unusual", "suspicious", "duplicate"],
     [("list_anomalies", {"limit": 10})]),
]

# Ceiling on how many tools one question may trigger. Retrieval should widen
# enough to answer the question, not sweep the whole ledger into a prompt.
_MAX_TOOLS_PER_QUESTION = 6


# Input that is not a question about the books. These are matched before the
# financial router, so a greeting never comes back as a cash balance.
_GREETING_PHRASES = {
    "hi", "hello", "hey", "yo", "hiya", "good morning", "good afternoon",
    "good evening", "greetings", "namaste", "hi there", "hello there",
}
_THANKS_PHRASES = {
    "thanks", "thank you", "thx", "ta", "cheers", "appreciated", "got it",
    "ok thanks", "perfect", "great thanks",
}
_IDENTITY_PHRASES = {
    "who are you", "what are you", "you know me", "do you know me",
    "your name", "what is your name", "who am i", "do you know who i am",
    "what can you do", "what do you do", "help", "how do you work",
    "what are your capabilities",
}
_STATUS_PHRASES = {
    "are you working", "are you there", "are you online", "you working",
    "is this working", "does this work", "are you alive", "you up",
    "are you ok", "still there",
}
_OUT_OF_SCOPE_PHRASES = {
    "weather", "joke", "story", "poem", "song", "movie", "football",
    "cricket score", "who won", "recipe", "translate", "capital of",
    "president of", "prime minister", "write code", "python script",
}
# A request to mutate the books. The copilot can propose, never act.
_ACTION_PHRASES = {
    "delete all", "delete every", "drop table", "remove all",
    "transfer ", "pay this", "make a payment", "release payment",
    "post this journal", "post the journal", "approve this", "approve all",
    "close the period", "change the tax rate", "edit the ledger",
    "update the balance", "write off all",
}
# Attempts to extract configuration, credentials or the system prompt.
_PROBE_PHRASES = {
    "api key", "api_key", "secret", "password", "credential", "token",
    "system prompt", "your instructions", "ignore previous",
    "ignore all previous", "disregard your", "env file", "environment variable",
}
# Words that mark a question as genuinely financial, used to stop an
# out-of-scope keyword from hijacking a real question ("translate the GST
# summary" is still a GST question).
_FINANCE_WORDS = {
    "cash", "invoice", "invoices", "bill", "bills", "gst", "tds", "tax",
    "profit", "revenue", "expense", "expenses", "ledger", "journal", "stock",
    "inventory", "vendor", "vendors", "customer", "customers", "payable",
    "payables", "receivable", "receivables", "balance", "reconcile",
    "reconciliation", "close", "audit", "payment", "payments", "account",
    "accounts", "margin", "aging", "overdue", "books",
}


# Stems rather than a word list: "bill" covers billing/billed/bills, "invoic"
# covers invoice/invoices/invoicing. Enumerating every inflection is a losing
# game, and a missed one silently diverts a real question to small talk.
_FINANCE_STEMS = (
    "bill", "invoic", "payab", "receivab", "reconcil", "ledger", "journal",
    "vendor", "custom", "suppli", "creditor", "debtor", "expens", "revenu",
    "profit", "margin", "overdue", "aging", "ageing", "voucher", "accrual",
    "deprec", "invent", "purchas", "sale", "sell", "sold", "buy", "bought",
    "spend", "spent", "charg", "collect", "owe", "owing", "paid", "pay",
    "credit", "debit", "balanc", "audit", "tax", "gst", "tds", "cash",
    "bank", "stock", "close", "closing", "account", "budget", "forecast",
)


def _mentions_finance(words: list[str]) -> bool:
    """True when any word names something in the accounting domain."""
    if set(words) & _FINANCE_WORDS:
        return True
    return any(word.startswith(_FINANCE_STEMS) for word in words)


def _matches(words: list[str], text: str, phrases: set[str]) -> bool:
    """True when the input *is* one of these phrases, not merely contains one.

    Containment alone is too eager: "who are you invoicing" would be caught by
    "who are you". A short input is matched loosely, a longer one must start
    with the phrase, so real questions keep going to the financial router.
    """
    if text in phrases:
        return True
    if len(words) <= 4:
        joined = " ".join(words)
        if joined in phrases:
            return True
        if any(joined.startswith(phrase) or phrase.startswith(joined) for phrase in phrases):
            return True
    return any(text.startswith(phrase) for phrase in phrases) and len(words) <= 6


# Deterministic intent routing. Each entry maps trigger phrases to the tools
# that answer them. This runs before the model, so common questions are
# answered from real data even with no LLM configured at all.
INTENT_ROUTES: list[tuple[str, list[str], list[tuple[str, dict]]]] = [
    (
        "cash_position",
        ["how much cash", "cash position", "cash do we have", "bank balance", "money in the bank"],
        [("get_cash_position", {})],
    ),
    (
        "cash_forecast",
        ["forecast", "cash flow projection", "runway", "will we have enough cash", "cash next"],
        [("get_cash_forecast", {"days": 30}), ("get_cash_position", {})],
    ),
    (
        "profit_explain",
        ["why is profit", "why did profit", "why are expenses", "why did expenses",
         "profit down", "profit up", "expenses increase", "expenses go up", "explain the change"],
        [("compare_periods", {}), ("get_profit_and_loss", {})],
    ),
    (
        "profit_loss",
        ["profit and loss", "p&l", "how profitable", "net profit", "revenue this", "income statement"],
        [("get_profit_and_loss", {})],
    ),
    (
        "balance_sheet",
        ["balance sheet", "does the balance sheet balance", "total assets", "net worth"],
        [("get_balance_sheet", {})],
    ),
    (
        "receivables",
        ["who owes us", "unpaid invoices", "outstanding invoices", "receivable", "customers owe",
         "overdue invoice", "collect"],
        [("get_top_customers_by_balance", {}), ("get_receivables_aging", {})],
    ),
    (
        "gst",
        ["gst", "gstr", "input tax", "output tax", "tax payable"],
        [("get_gst_summary", {})],
    ),
    (
        "tds",
        ["tds", "tax deducted", "194"],
        [("get_tds_summary", {})],
    ),
    (
        "payables",
        ["what do we owe", "unpaid bills", "bills due", "accounts payable", "vendors due",
         "vendors are due", "pay this week", "supplier due", "owe our vendors"],
        [("list_outstanding_bills", {"due_within_days": 7}), ("get_payables_aging", {})],
    ),
    (
        "duplicates",
        ["duplicate", "paid twice", "billed twice", "double payment"],
        [("find_duplicate_bills", {})],
    ),
    (
        "reconciliation",
        ["reconcile", "unreconciled", "bank matching", "unmatched transaction"],
        [("get_reconciliation_status", {}), ("list_unreconciled_transactions", {})],
    ),
    (
        "anomalies",
        ["unusual", "anomal", "suspicious", "fraud", "strange transaction"],
        [("list_anomalies", {})],
    ),
    (
        "exceptions",
        ["needs attention", "what should i do", "what needs review", "exceptions", "to do"],
        [("list_open_exceptions", {})],
    ),
    (
        "inventory",
        ["stock", "inventory", "low stock", "out of stock", "reorder"],
        [("get_inventory_summary", {}), ("get_stock_movement_analysis", {})],
    ),
    (
        "close",
        ["month end", "close the books", "month-end", "closing", "close status"],
        [("get_close_status", {})],
    ),
    (
        "search",
        ["find ", "show me all", "search for", "transactions from", "list transactions"],
        [("search_transactions", {})],
    ),
]


class CopilotAgent(BaseAgent):
    kind = AgentKind.COPILOT
    display_name = "AI Finance Copilot"
    role_description = (
        "You are the finance lead for this company, talking to a colleague who "
        "has asked you a question about the books. Speak the way a competent "
        "accountant speaks in a meeting: plain sentences, the answer first, the "
        "number in context, then what it means and what you would do about it. "
        "Say 'we' and 'our'. Round to lakhs or crores when discussing the shape "
        "of things and give the exact figure when precision matters. Never open "
        "with a label such as 'Cash position:' and never present a bare figure "
        "with no interpretation — a colleague can read the dashboard, they came "
        "to you for judgement.\n"
        "\n"
        "Your authority comes entirely from the query results you are given. "
        "Every figure you state must appear in that data. Never estimate, never "
        "extrapolate, never invent a name, id, date or amount. If the data does "
        "not answer the question, say so directly and say what you would need "
        "to look at — an honest 'I can't tell from this' is worth more than a "
        "confident guess, because someone will act on what you say."
    )

    async def run_agent(self, **params: Any) -> AgentResult:
        question = str(params.get("question", "")).strip()
        conversation_id = params.get("conversation_id")

        result = AgentResult(agent_kind=self.kind, items_processed=1)
        if not question:
            result.summary = "No question was asked."
            result.status = AgentRunStatus.FAILED
            return result

        conversation = self._conversation(conversation_id, question)
        self._add_message(conversation, "user", question)

        # Not everything typed into a chat box is a question about the books.
        # Answering "hello" with the cash balance is not a small blemish: it
        # tells the user the thing is pattern-matching rather than listening,
        # and it buries the cases where the figures actually mean something.
        conversational = self._conversational_reply(question)
        if conversational is not None:
            message = self._add_message(
                conversation,
                "assistant",
                conversational["answer"],
                answer=conversational,
                intent=conversational["intent"],
                tools_used=[],
            )
            result.summary = conversational["answer"]
            result.metrics = {
                "intent": conversational["intent"],
                "tools_used": [],
                "conversation_id": conversation.id,
                "message_id": message.id,
                "confidence": str(conversational.get("confidence", "1")),
            }
            result.used_llm = False
            return result

        intent, tool_calls = self._route(question)
        tool_results: dict[str, Any] = {}
        tools_used: list[str] = []

        for tool_name, arguments in tool_calls:
            try:
                enriched = self._enrich_arguments(tool_name, dict(arguments), question)
                tool_results[tool_name] = call_tool(
                    self.db, self.company_id, tool_name, arguments=enriched, actor="AI"
                )
                tools_used.append(tool_name)
            except Exception as exc:  # noqa: BLE001 — a failing tool must not break the answer
                tool_results[tool_name] = {"error": str(exc)}

        answer = await self._compose_answer(question, intent, tool_results, tools_used)

        message = self._add_message(
            conversation,
            "assistant",
            answer.get("answer", ""),
            answer=answer,
            intent=intent,
            tools_used=tools_used,
        )

        result.summary = answer.get("answer", "")[:800]
        result.metrics = {
            "intent": intent,
            "tools_used": tools_used,
            "conversation_id": conversation.id,
            "message_id": message.id,
            "confidence": str(answer.get("confidence", ZERO)),
            "insufficient_evidence": bool(answer.get("insufficient_evidence")),
        }
        result.findings = answer.get("evidence", [])[:10]
        result.used_llm = bool(answer.get("used_llm"))
        return result

    # ---------------------------------------------------- conversational --

    def _conversational_reply(self, question: str) -> dict[str, Any] | None:
        """Handle input that is not a question about the books.

        Returns ``None`` for anything that should go to the financial router,
        so this can only ever divert input the router would have answered
        badly. Every branch is deterministic and needs no model, which matters
        because these are exactly the cases where a model is most tempted to
        make something up.
        """
        text = question.strip().lower()
        stripped = re.sub(r"[^a-z0-9\s]", " ", text).strip()
        words = stripped.split()

        def reply(intent: str, answer: str, actions: list[str] | None = None) -> dict[str, Any]:
            return {
                "answer": answer,
                "key_figures": [],
                "suggested_actions": actions or SUGGESTED_PROMPTS[:4],
                "evidence": [],
                "source_records": [],
                "confidence": D("1"),
                "intent": intent,
                "tools_used": [],
                "used_llm": False,
            }

        # Too short or punctuation-only to carry a question.
        if not stripped or (len(words) == 1 and len(stripped) < 3):
            return reply(
                "clarify",
                "I did not catch a question there. Ask me about the books — "
                "cash, invoices, bills, GST, stock, reconciliation or the "
                "month-end close.",
            )

        # A request to change data. The copilot reads and proposes; it never
        # posts, pays or deletes, and saying so plainly is more useful than
        # answering with unrelated figures.
        if any(phrase in text for phrase in _ACTION_PHRASES):
            return reply(
                "action_refused",
                "I can look things up and suggest actions, but I cannot change "
                "the books. Posting entries, releasing payments, approving "
                "proposals and closing periods are all human-only, and every "
                "one of them is recorded in the audit trail. I can show you "
                "what is involved if that helps.",
                ["What needs my attention?", "Show unpaid invoices."],
            )

        # Attempts to extract configuration or override instructions.
        if any(phrase in text for phrase in _PROBE_PHRASES):
            return reply(
                "refused",
                "I cannot share configuration, credentials or my instructions, "
                "and I do not take instructions from message content. I answer "
                "questions about this company's books using its own data.",
            )

        # Past this point every branch is small talk. If the question names
        # anything financial it is a real question that merely opens like one —
        # "who are you billing the most?", "help me find duplicate bills" —
        # so hand it to the router rather than answering with a description of
        # myself. Mutation and probe attempts are already handled above, since
        # those name financial things too and must still be refused.
        if _mentions_finance(words):
            return None

        if _matches(words, text, _IDENTITY_PHRASES):
            return reply(
                "identity",
                "I am the finance copilot for these books. I read this "
                "company's ledger through a fixed set of queries and answer "
                "from what is actually recorded — I do not know anything about "
                "you personally, and I never invent a figure. Ask me about "
                "cash, receivables, payables, GST, stock or the close.",
            )

        if _matches(words, text, _STATUS_PHRASES):
            return reply(
                "status",
                "Yes — I am working. Ask a question about the books and I will "
                "answer it from the ledger, showing which queries produced each "
                "figure. Settings > AI providers shows which model is answering.",
            )

        if _matches(words, text, _GREETING_PHRASES):
            return reply(
                "greeting",
                "Hello. I can answer questions about this company's books — "
                "cash position, who owes what, GST, stock, reconciliation and "
                "the month-end close. What would you like to know?",
            )

        if _matches(words, text, _THANKS_PHRASES):
            return reply(
                "thanks", "Happy to help. Ask me anything else about the books."
            )

        # Clearly outside the domain, and no finance word anywhere to redeem it.
        if any(phrase in text for phrase in _OUT_OF_SCOPE_PHRASES) and not (
            set(words) & _FINANCE_WORDS
        ):
            return reply(
                "out_of_scope",
                "That is outside what I can help with — I only answer questions "
                "about this company's accounting data. Ask me about cash, "
                "invoices, bills, tax, stock or the close.",
            )

        # No letters at all (emoji, digits, symbols): nothing to route on.
        if not any(character.isalpha() for character in stripped):
            return reply(
                "clarify",
                "I could not read a question there. Try asking something like "
                '"What is our cash position?" or "Who owes us the most?"',
            )

        return None

    # ------------------------------------------------------------ routing --

    def _route(self, question: str) -> tuple[str, list[tuple[str, dict]]]:
        """Match an intent by phrase, then by keyword overlap.

        Phrase matching is exact and cheap; the keyword pass catches natural
        rewordings ("which vendors are due" vs the "vendors due" trigger)
        without needing a model to classify the question.
        """
        lowered = question.lower()

        for intent, triggers, calls in INTENT_ROUTES:
            if any(trigger in lowered for trigger in triggers):
                return intent, self._augment(lowered, intent, calls)

        asked = {word for word in re.findall(r"[a-z']+", lowered) if len(word) > 2}
        # Words too common to be evidence of any particular intent.
        stopwords = {
            "the", "our", "what", "which", "who", "how", "why", "are", "was", "were",
            "for", "and", "this", "that", "with", "from", "have", "has", "show", "get",
            "does", "did", "can", "you", "his", "her", "its", "any", "all", "much",
            "many", "week", "month", "year", "day", "days", "now", "next", "last",
        }
        asked -= stopwords

        best_intent, best_calls, best_score = "", [], 0
        for intent, triggers, calls in INTENT_ROUTES:
            trigger_words: set[str] = set()
            for trigger in triggers:
                trigger_words |= {
                    word for word in re.findall(r"[a-z']+", trigger) if len(word) > 2
                }
            trigger_words -= stopwords
            score = len(asked & trigger_words)
            if score > best_score:
                best_intent, best_calls, best_score = intent, calls, score

        if best_score >= 1:
            return best_intent, self._augment(lowered, best_intent, best_calls)

        # Unmatched questions get a broad but bounded context.
        return "general", [
            ("get_cash_position", {}),
            ("get_profit_and_loss", {}),
            ("list_open_exceptions", {"limit": 5}),
        ]

    def _augment(
        self, lowered: str, intent: str, calls: list[tuple[str, dict]]
    ) -> list[tuple[str, dict]]:
        """Add tools for other topics the question explicitly mentions.

        A single intent retrieves a single topic's data, so "is our cash healthy
        given overdue payables" fetched cash and nothing else. The model then
        answered — correctly and uselessly — that it had no payables data. The
        question named two subjects; both need retrieving.

        This stays inside the typed-tool contract: it only ever adds tools from
        the same registry, each still company-scoped and permission-gated. There
        is no path here to an arbitrary query.
        """
        extra: list[tuple[str, dict]] = []
        for topic, words, tools in _SECONDARY_TOPICS:
            if topic == intent:
                continue
            if any(word in lowered for word in words):
                extra.extend(tools)

        merged = list(calls)
        seen = {name for name, _ in merged}
        for name, params in extra:
            if name not in seen:
                merged.append((name, params))
                seen.add(name)
            if len(merged) >= _MAX_TOOLS_PER_QUESTION:
                break
        return merged

    def _enrich_arguments(self, tool_name: str, arguments: dict, question: str) -> dict:
        """Fill in dates and search terms parsed from the question itself."""
        spec = REGISTRY.get(tool_name)
        accepted = set(spec.parameters) if spec else set()

        start, end = self._parse_period(question)
        if "start_date" in accepted and "start_date" not in arguments:
            arguments["start_date"] = start.isoformat()
        if "end_date" in accepted and "end_date" not in arguments:
            arguments["end_date"] = end.isoformat()
        if "as_of" in accepted and "as_of" not in arguments:
            arguments["as_of"] = end.isoformat()

        if "compare_start" in accepted and "compare_start" not in arguments:
            previous_end = start - timedelta(days=1)
            arguments["compare_start"] = previous_end.replace(day=1).isoformat()
            arguments["compare_end"] = previous_end.isoformat()

        if "period" in accepted and "period" not in arguments:
            arguments["period"] = f"{end.year}-{end.month:02d}"

        if tool_name == "search_transactions" and "query" not in arguments:
            arguments["query"] = self._extract_search_term(question)
            amount = self._extract_amount(question)
            if amount is not None and "min_amount" in accepted:
                arguments["min_amount"] = str(amount)

        return {key: value for key, value in arguments.items() if key in accepted}

    def _parse_period(self, question: str) -> tuple[date, date]:
        """Understand 'last month', 'this quarter', 'this year' etc."""
        lowered = question.lower()
        today = date.today()
        month_start = today.replace(day=1)

        if "last month" in lowered or "previous month" in lowered:
            end = month_start - timedelta(days=1)
            return end.replace(day=1), end
        if "this year" in lowered or "ytd" in lowered or "year to date" in lowered:
            fiscal_start = (
                date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)
            )
            return fiscal_start, today
        if "last year" in lowered:
            fiscal_start = (
                date(today.year - 1, 4, 1) if today.month >= 4 else date(today.year - 2, 4, 1)
            )
            return fiscal_start, fiscal_start.replace(year=fiscal_start.year + 1) - timedelta(days=1)
        if "last quarter" in lowered:
            return today - timedelta(days=90), today
        if "last 30" in lowered or "past 30" in lowered:
            return today - timedelta(days=30), today
        if "last week" in lowered or "past week" in lowered:
            return today - timedelta(days=7), today

        # Explicit month names, e.g. "in July"
        months = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ]
        for index, name in enumerate(months, start=1):
            if name in lowered:
                year = today.year if index <= today.month else today.year - 1
                from calendar import monthrange

                return date(year, index, 1), date(year, index, monthrange(year, index)[1])

        return month_start, today

    def _extract_search_term(self, question: str) -> str:
        cleaned = re.sub(
            r"\b(find|show|me|all|the|search|for|transactions?|from|list|invoices?|bills?|please|over|above|under)\b",
            " ",
            question.lower(),
        )
        cleaned = re.sub(r"[^\w\s]", " ", cleaned)
        words = [word for word in cleaned.split() if len(word) > 2 and not word.isdigit()]
        return " ".join(words[:4])

    def _extract_amount(self, question: str) -> Decimal | None:
        match = re.search(
            r"(?:over|above|more than|greater than|exceeding)\s*(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)\s*(k|lakh|lac|cr|crore|l)?",
            question.lower(),
        )
        if not match:
            return None
        try:
            value = D(match.group(1).replace(",", ""))
        except Exception:  # noqa: BLE001
            return None
        suffix = match.group(2)
        multipliers = {"k": 1_000, "l": 100_000, "lakh": 100_000, "lac": 100_000, "cr": 10_000_000, "crore": 10_000_000}
        if suffix in multipliers:
            value = value * D(multipliers[suffix])
        return money(value)

    # ------------------------------------------------------------ answers --

    async def _compose_answer(
        self, question: str, intent: str, tool_results: dict, tools_used: list[str]
    ) -> dict:
        """Build the structured answer, grounded in the tool results."""
        if not tool_results or all(
            isinstance(value, dict) and "error" in value for value in tool_results.values()
        ):
            return {
                **insufficient_evidence("No data could be retrieved for this question."),
                "used_llm": False,
                "intent": intent,
            }

        deterministic = self._deterministic_answer(intent, tool_results, question)
        evidence, source_records = self._build_evidence(tool_results)
        # Derive the whitelist from the exact object handed to the model as
        # `data` below, so what it may cite matches what it was shown.
        allowed_ids = collect_allowed_ids(tool_results)

        parsed, meta = await self.ask_json(
            task="answer_question",
            instruction=(
                "Answer the user's actual question using ONLY the query results in "
                "the context. "
                "`verified_figures` holds totals already computed from the ledger: "
                "treat them as ground truth and never contradict or recompute them, "
                "but do NOT simply repeat them. The user asked a specific question, "
                "so answer THAT, using the figures as evidence. If they asked whether "
                "something is healthy, judge it. If they asked which one to act on "
                "first, name it and say why. If they asked why something changed, "
                "explain the drivers visible in the data. "
                "Write the way you would say it out loud to a colleague. Lead "
                "with the answer, not a restatement of the question. No bullet "
                "points, no headings, no 'Cash position:' label — just a short "
                "paragraph of connected sentences. Where a number carries a "
                "consequence, say the consequence: 'that covers about six weeks "
                "of outgoings', 'nearly two-thirds of that is past due'. Close "
                "with what you would do next, if the data supports one. "
                'Reply with JSON: {"answer": "<2-5 sentences, conversational, '
                'answering the question and saying what it means>", '
                '"key_figures": [{"label": "...", "value": "..."}], '
                '"suggested_actions": ["..."], "confidence": 0-1, '
                '"source_records": [{"type": "invoice|bill|account|...", "id": "..."}]}. '
                "Every number must come from the data. Do not invent an id, a name, "
                "a date or an amount that is not present. If the data genuinely does "
                'not answer the question, reply {"insufficient_evidence": true, '
                '"answer": "<say what is missing>"}.'
            ),
            context={
                "question": question,
                "intent": intent,
                "data": tool_results,
                "verified_figures": deterministic["answer"],
            },
            untrusted_text=question,
            untrusted_label="user question",
            max_tokens=1200,
        )

        if parsed is None or parsed.get("insufficient_evidence"):
            # The deterministic answer is still real and useful.
            return {
                **deterministic,
                "used_llm": bool(meta.get("used_llm")),
                "evidence": evidence,
                "source_records": source_records,
                "intent": intent,
                "tools_used": tools_used,
                "provider": meta.get("provider", ""),
                "note": (
                    "Answered from computed figures; the model could not add to it."
                    if meta.get("error")
                    else ""
                ),
            }

        grounding = ground_answer(
            self.db, self.company_id, parsed, allowed_ids=allowed_ids or None
        )
        if not grounding.grounded:
            # The model cited records it was never given: distrust the prose.
            return {
                **deterministic,
                "used_llm": bool(meta.get("used_llm")),
                "evidence": evidence,
                "source_records": source_records,
                "intent": intent,
                "tools_used": tools_used,
                "grounding_warning": (
                    "The model referenced records that were not in the retrieved data, so "
                    "its wording was discarded and the computed figures are shown instead."
                ),
                "grounding_errors": grounding.errors[:5],
            }

        return {
            "answer": str(parsed.get("answer", deterministic["answer"]))[:2000],
            "key_figures": parsed.get("key_figures", deterministic.get("key_figures", [])),
            "suggested_actions": parsed.get(
                "suggested_actions", deterministic.get("suggested_actions", [])
            ),
            "confidence": D(str(parsed.get("confidence", 0.8))),
            "evidence": evidence,
            "source_records": source_records,
            "financial_impact": deterministic.get("financial_impact"),
            "intent": intent,
            "tools_used": tools_used,
            "used_llm": bool(meta.get("used_llm")),
            "provider": meta.get("provider", ""),
            "computed_answer": deterministic["answer"],
        }

    def _deterministic_answer(self, intent: str, results: dict, question: str) -> dict:
        """A real answer computed from the tool output, with no model involved."""
        figures: list[dict] = []
        actions: list[str] = []
        parts: list[str] = []
        impact: Decimal | None = None

        cash = results.get("get_cash_position")
        if isinstance(cash, dict) and "total_cash" in cash:
            parts.append(f"Cash and bank balances total {fmt_inr(cash['total_cash'])}")
            figures.append({"label": "Total cash", "value": fmt_inr(cash["total_cash"])})
            impact = cash["total_cash"]

        pl = results.get("get_profit_and_loss")
        if isinstance(pl, dict) and "net_profit" in pl:
            parts.append(
                f"revenue is {fmt_inr(pl['revenue'])} and net profit "
                f"{fmt_inr(pl['net_profit'])} ({pl['net_margin_percent']}% margin)"
            )
            figures.extend(
                [
                    {"label": "Revenue", "value": fmt_inr(pl["revenue"])},
                    {"label": "Net profit", "value": fmt_inr(pl["net_profit"])},
                ]
            )

        comparison = results.get("compare_periods")
        if isinstance(comparison, dict) and "net_profit_change" in comparison:
            change = comparison["net_profit_change"]
            direction = "increased" if change > ZERO else "decreased"
            percent = comparison.get("net_profit_change_percent")
            parts.append(
                f"net profit {direction} by {fmt_inr(abs(change))}"
                + (f" ({abs(percent)}%)" if percent is not None else "")
                + " against the prior period"
            )
            drivers = comparison.get("drivers", [])[:3]
            if drivers:
                driver_text = "; ".join(
                    f"{driver['name']} {'up' if driver['change'] > ZERO else 'down'} "
                    f"{fmt_inr(abs(driver['change']))}"
                    for driver in drivers
                )
                parts.append(f"the largest movements were {driver_text}")
                for driver in drivers:
                    figures.append(
                        {"label": driver["name"], "value": fmt_inr(driver["change"])}
                    )
            impact = change

        receivables = results.get("get_top_customers_by_balance")
        if isinstance(receivables, dict) and receivables.get("customers"):
            top = receivables["customers"][0]
            parts.append(
                f"{fmt_inr(receivables['total_receivable'])} is receivable, the largest being "
                f"{top['name']} at {fmt_inr(top['total_due'])} ({top['oldest_days']} days old)"
            )
            figures.append(
                {"label": "Total receivable", "value": fmt_inr(receivables["total_receivable"])}
            )
            actions.append("Review the collections list and send reminders to the oldest accounts.")
            impact = receivables["total_receivable"]

        aging = results.get("get_receivables_aging")
        if isinstance(aging, dict) and "total_overdue" in aging:
            parts.append(f"{fmt_inr(aging['total_overdue'])} of that is overdue")

        bills = results.get("list_outstanding_bills")
        if isinstance(bills, dict) and "total_outstanding" in bills:
            parts.append(
                f"{bills['count']} bill(s) totalling {fmt_inr(bills['total_outstanding'])} are due"
            )
            figures.append({"label": "Bills due", "value": fmt_inr(bills["total_outstanding"])})
            impact = bills["total_outstanding"]

        duplicates = results.get("find_duplicate_bills")
        if isinstance(duplicates, dict) and "group_count" in duplicates:
            if duplicates["group_count"]:
                parts.append(
                    f"{duplicates['group_count']} possible duplicate bill group(s) were found, "
                    f"with {fmt_inr(duplicates['total_at_risk'])} at risk of double payment"
                )
                actions.append("Compare the source documents before releasing these payments.")
                impact = duplicates["total_at_risk"]
            else:
                parts.append("no duplicate bills were found")

        recon = results.get("get_reconciliation_status")
        if isinstance(recon, dict) and "needs_attention_count" in recon:
            parts.append(
                f"{recon['needs_attention_count']} bank transaction(s) still need attention "
                f"({recon['match_rate']}% matched)"
            )
            if recon["needs_attention_count"]:
                actions.append("Open Reconciliation to review the outstanding items.")

        forecast = results.get("get_cash_forecast")
        if isinstance(forecast, dict) and "closing_cash" in forecast:
            parts.append(
                f"over the next {forecast['horizon_days']} days cash is projected to move from "
                f"{fmt_inr(forecast['opening_cash'])} to {fmt_inr(forecast['closing_cash'])} "
                f"(range {fmt_inr(forecast['confidence_low'])} to {fmt_inr(forecast['confidence_high'])})"
            )
            figures.append(
                {"label": "Projected closing cash", "value": fmt_inr(forecast["closing_cash"])}
            )
            actions.extend(forecast.get("warnings", [])[:2])

        gst = results.get("get_gst_summary")
        if isinstance(gst, dict) and "net_payable" in gst:
            parts.append(
                f"for {gst['period']}, output GST is {fmt_inr(gst['output']['total'])} and input "
                f"GST {fmt_inr(gst['input']['total'])}, leaving "
                + (
                    f"{fmt_inr(gst['net_payable'])} payable"
                    if gst["net_payable"] > ZERO
                    else f"{fmt_inr(gst['credit_carried_forward'])} of credit"
                )
            )
            impact = gst["net_payable"]

        tds = results.get("get_tds_summary")
        if isinstance(tds, dict) and "total_deducted" in tds:
            parts.append(
                f"TDS deducted is {fmt_inr(tds['total_deducted'])}, of which "
                f"{fmt_inr(tds['total_pending'])} is still to be deposited"
            )

        anomalies = results.get("list_anomalies")
        if isinstance(anomalies, dict) and "total_open" in anomalies:
            if anomalies["total_open"]:
                top = anomalies["anomalies"][:2]
                count = anomalies["total_open"]
                parts.append(
                    f"{count} anomal{'y is' if count == 1 else 'ies are'} open, including: "
                    + "; ".join(a["title"] for a in top)
                )
                actions.append("Review the flagged items in the Exceptions centre.")
            else:
                parts.append("no anomalies are currently open")

        exceptions = results.get("list_open_exceptions")
        if isinstance(exceptions, dict) and "total_open" in exceptions:
            if exceptions["total_open"]:
                parts.append(f"{exceptions['total_open']} item(s) need your attention")
                actions.append("Open the Exceptions centre to work through them.")
            else:
                parts.append("nothing is currently waiting for your decision")

        inventory = results.get("get_inventory_summary")
        if isinstance(inventory, dict) and "total_stock_value" in inventory:
            parts.append(
                f"stock is worth {fmt_inr(inventory['total_stock_value'])} with "
                f"{inventory['low_stock_count']} item(s) low and "
                f"{inventory['out_of_stock_count']} out of stock"
            )
            impact = inventory["total_stock_value"]

        close = results.get("get_close_status")
        if isinstance(close, dict) and close.get("exists"):
            parts.append(
                f"the {close['period_label']} close is {close['progress_percent']}% complete "
                f"({close['completed_tasks']} of {close['total_tasks']} checks passing)"
            )
            if close.get("blocked_tasks"):
                actions.append(
                    f"Resolve {close['blocked_tasks']} blocking task(s) before closing the period."
                )
        elif isinstance(close, dict):
            parts.append(close.get("message", "the close checklist has not been run"))

        balance_sheet = results.get("get_balance_sheet")
        if isinstance(balance_sheet, dict) and "total_assets" in balance_sheet:
            parts.append(
                f"total assets are {fmt_inr(balance_sheet['total_assets'])} against liabilities "
                f"of {fmt_inr(balance_sheet['total_liabilities'])} and equity of "
                f"{fmt_inr(balance_sheet['total_equity'])}"
                + ("" if balance_sheet["balanced"] else " — the balance sheet does NOT balance")
            )

        search = results.get("search_transactions")
        if isinstance(search, dict) and "total_matches" in search:
            parts.append(
                f"{search['total_matches']} matching transaction(s) were found"
                + (f", showing the first {search['showing']}" if search["showing"] else "")
            )

        unreconciled = results.get("list_unreconciled_transactions")
        if isinstance(unreconciled, dict) and unreconciled.get("total_unreconciled"):
            parts.append(
                f"{unreconciled['total_unreconciled']} unreconciled bank transaction(s) remain"
            )

        if not parts:
            return insufficient_evidence(
                "The available data does not contain an answer to this question."
            )

        answer = ". ".join(part[0].upper() + part[1:] if part else part for part in parts[:1])
        if len(parts) > 1:
            answer += ". " + ". ".join(parts[1:])
        answer = answer.rstrip(".") + "."

        return {
            "answer": answer,
            "key_figures": figures[:6],
            "suggested_actions": actions[:4],
            "confidence": D("0.95"),
            "financial_impact": impact,
        }

    def _collect_rows(self, results: dict) -> list[list[dict]]:
        collections: list[list[dict]] = []
        for payload in results.values():
            if not isinstance(payload, dict):
                continue
            for value in payload.values():
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    collections.append(value)
        return collections

    def _build_evidence(self, results: dict) -> tuple[list[dict], list[dict]]:
        """Click-through evidence rows for the UI."""
        evidence: list[dict] = []
        sources: list[dict] = []

        mapping = [
            ("invoices", "invoice", "number", "balance_due"),
            ("bills", "bill", "number", "balance_due"),
            ("transactions", "bank_transaction", "description", "amount"),
            ("customers", "customer", "name", "total_due"),
            ("top_customers", "customer", "name", "total"),
            ("top_vendors", "vendor", "name", "total"),
            ("anomalies", "anomaly", "title", "amount"),
            ("exceptions", "exception", "title", "amount"),
            ("accounts", "account", "name", "balance"),
            ("results", "journal_line", "description", "debit"),
            ("drivers", "account", "name", "change"),
        ]

        for payload in results.values():
            if not isinstance(payload, dict):
                continue
            for key, entity_type, label_field, amount_field in mapping:
                rows = payload.get(key)
                if not isinstance(rows, list):
                    continue
                for row in rows[:8]:
                    if not isinstance(row, dict):
                        continue
                    entity_id = (
                        row.get("id")
                        or row.get(f"{entity_type}_id")
                        or row.get("account_id")
                        or row.get("customer_id")
                        or row.get("vendor_id")
                    )
                    evidence.append(
                        {
                            "type": entity_type,
                            "id": entity_id,
                            "label": str(row.get(label_field, ""))[:120],
                            "amount": str(row.get(amount_field, "")) if row.get(amount_field) is not None else None,
                            "date": str(row.get("date") or row.get("due_date") or ""),
                        }
                    )
                    if entity_id:
                        sources.append({"type": entity_type, "id": entity_id})

        return evidence[:20], sources[:20]

    # ----------------------------------------------------- conversations --

    def _conversation(self, conversation_id: str | None, question: str) -> Conversation:
        if conversation_id:
            conversation = self.db.get(Conversation, conversation_id)
            if conversation is not None and conversation.company_id == self.company_id:
                return conversation

        conversation = Conversation(
            company_id=self.company_id,
            title=question[:120],
            user_id=self.user_id,
        )
        self.db.add(conversation)
        self.db.flush()
        return conversation

    def _add_message(
        self,
        conversation: Conversation,
        role: str,
        content: str,
        *,
        answer: dict | None = None,
        intent: str = "",
        tools_used: list[str] | None = None,
    ) -> ConversationMessage:
        message = ConversationMessage(
            company_id=self.company_id,
            conversation_id=conversation.id,
            role=role,
            content=content[:8000],
            answer=_jsonable(answer or {}),
            intent=intent,
            tools_used=tools_used or [],
            provider=(answer or {}).get("provider", ""),
            user_id=self.user_id,
        )
        self.db.add(message)
        conversation.message_count += 1
        conversation.last_message_at = utcnow()
        self.db.flush()
        return message


def _jsonable(value: Any) -> Any:
    """Decimals and dates must survive the JSON column round-trip."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


SUGGESTED_PROMPTS = [
    "What is our cash position?",
    "Why is profit down this month?",
    "Who owes us the most?",
    "Show unpaid invoices.",
    "Which vendors are due this week?",
    "Find duplicate bills.",
    "Show unreconciled transactions.",
    "Forecast cash for 30 days.",
    "Explain GST payable.",
    "Show unusual transactions.",
    "What needs my attention?",
    "How is the month-end close going?",
]
