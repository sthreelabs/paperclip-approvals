#!/usr/bin/env python3
"""approvals.py: a chat bot that puts every Paperclip approval in front of the right person with
Approve / Not now buttons, and records the decision in Paperclip as that person.

Copyright (c) 2026 sthreelabs LLC. MIT License (see LICENSE). Provided as-is.

Plain code, no AI: nothing it reads can steer it. One bot per Paperclip company. The chat side is Telegram or Slack (APPROVALS_CHAT); everything else is shared.

What it shows (polled every POLL_SECONDS):
  board  pending board approvals: hires, budgets, strategy (GET /companies/:id/approvals)
  card   pending confirmation cards on any open task, e.g. an agent's "OK to send?", and question
         cards a teammate raised for a person (ask_user_questions)
  review a task a teammate put In review and assigned to a person. Posted once with a link and no
         buttons (reviewing means opening the work), closed when the task leaves review, and
         posted again if it comes back to review later

Where it posts:
  telegram  a direct message to each approver allowed that kind
  slack     one message in the approvals channel that @mentions each approver allowed that kind

What a button press does:
  1. the presser must be on the approver list (Telegram ID or Slack user ID) and allowed that kind;
  2. the item is fetched again and must still be pending;
  3. Paperclip is called with THAT approver's own key, so the decision is recorded as them
     (human-only cards and the guard's approve-once still hold);
  4. every message about the item is edited, and one line goes to the audit log.
A question card gets an Answer button instead (Slack): it opens a form with the card's questions,
and the submitted answers go to Paperclip the same way, with the approver's own key. On Telegram a
question card shows its questions and a link to answer in Paperclip.

It never reads conversation: on Telegram it ignores typed text except /start (which answers with
the sender's ID for setup); on Slack it gets button presses and its own answer forms only. It never approves
anything on its own, and any error leaves the item pending. Telegram long polling and Slack
Socket Mode are both outbound: nothing has to reach it from the internet.

Configuration (environment, never logged):
  APPROVALS_CHAT           telegram (default) or slack
  TELEGRAM_BOT_TOKEN       telegram: the bot's token from BotFather
  SLACK_BOT_TOKEN          slack: the app's bot token (xoxb-…, scope chat:write)
  SLACK_APP_TOKEN          slack: the app-level token for Socket Mode (xapp-…, connections:write)
  SLACK_APPROVALS_CHANNEL  slack: channel ID to post in (the bot must be a member)
  PAPERCLIP_API_URL        e.g. https://paperclip.example.com
  PAPERCLIP_PUBLIC_URL     base for links (defaults to PAPERCLIP_API_URL)
  PAPERCLIP_COMPANY_ID     the company to watch
  APPROVERS_FILE           JSON list: [{"name", "telegram_id" or "slack_user_id", "key_env",
                           "kinds": ["board", "card", "review"], "paperclip_user_id" (optional:
                           review items then mention this approver only for their own reviews)}]
  READER_KEY_ENV           name of the env var holding the key used to list items (a board key)
  APPROVALS_STATE_FILE     JSON state (messages sent, update offset); default ./approvals-state.json
  APPROVALS_AUDIT_FILE     JSON lines, one per decision; default ./approvals-audit.jsonl
  APPROVALS_TZ             time zone for "Approved by … · 9:41 PM"; default America/New_York
  APPROVALS_LABEL          optional prefix for every item, e.g. the company ("Acme · Hire: …"),
                           when several Paperclip companies post into one channel

  approvals.py            run the bot
  approvals.py --once     one sync pass, then exit (setup check)
  approvals.py --env-file A --env-file B   load KEY=value files first (repeatable)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import queue
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

POLL_SECONDS = 20
NOT_NOW_REASON = 'Not now (declined in chat via the approvals bot).'
TIMEOUT = 15
KINDS = {'board', 'card', 'review'}
CHATS = {'telegram', 'slack'}
OPEN_STATUSES = 'backlog,todo,in_progress,in_review,blocked'
DETAILS_MAX = 700
CARD_KINDS = {'request_confirmation', 'ask_user_questions'}
ANSWER_FORM = 'answer_questions'  # Slack view callback_id
SLACK_CHOICES_MAX = 10  # radio buttons and checkboxes take at most 10 options; selects take 100
SLACK_SELECT_MAX = 100
SLACK_OPTION_TEXT = 75
APPROVAL_LABELS = {
    'hire_agent': 'Hire',
    'approve_ceo_strategy': 'Strategy',
    'budget_override_required': 'Budget',
    'request_board_approval': 'Decision',
}


class BotError(Exception):
    """A configuration problem the operator must fix (the bot exits)."""


class ApiError(Exception):
    """A failed call to the chat app or Paperclip (logged; the bot carries on)."""


# --- HTTP -----------------------------------------------------------------------------------------
def request_json(
    url: str,
    method: str = 'GET',
    body: Any = None,
    headers: dict | None = None,
    timeout: float = TIMEOUT,
) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Accept', 'application/json')
    if data is not None:
        req.add_header('Content-Type', 'application/json; charset=utf-8')
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors='replace')
        raise ApiError(f'{method} {redact(url)} failed ({exc.code}): {detail}') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ApiError(f'{method} {redact(url)} failed: {exc}') from exc
    try:
        return json.loads(raw) if raw else None
    except ValueError as exc:
        raise ApiError(f'{method} {redact(url)} returned non-JSON') from exc


def redact(url: str) -> str:
    """Telegram puts the bot token in the URL path; never let it reach a log."""
    return re.sub(r'/bot[^/]+/', '/bot<token>/', url)


class Paperclip:
    def __init__(self, base: str, company: str):
        base = base.rstrip('/')
        self.base = base if base.endswith('/api') else base + '/api'
        self.company = company

    def call(self, key: str, method: str, path: str, body: Any = None) -> Any:
        return request_json(
            self.base + path, method, body, {'Authorization': f'Bearer {key}'}
        )

    def dismiss_inbox(self, key: str, approval_id: str) -> None:
        # Clears the decided approval from that key's user's Paperclip inbox.
        self.call(
            key,
            'POST',
            f'/companies/{self.company}/inbox-dismissals',
            {'itemKey': f'approval:{approval_id}'},
        )

    def pending_approvals(self, key: str) -> list[dict]:
        return as_list(
            self.call(key, 'GET', f'/companies/{self.company}/approvals?status=pending')
        )

    def open_issues(self, key: str) -> list[dict]:
        return as_list(
            self.call(
                key, 'GET', f'/companies/{self.company}/issues?status={OPEN_STATUSES}'
            )
        )

    def cards(self, key: str, issue_id: str) -> list[dict]:
        return as_list(self.call(key, 'GET', f'/issues/{issue_id}/interactions'))

    def agents(self, key: str) -> dict[str, str]:
        return {
            a['id']: a.get('name') or a['id'][:8]
            for a in as_list(self.call(key, 'GET', f'/companies/{self.company}/agents'))
            if isinstance(a, dict) and a.get('id')
        }

    def prefix(self, key: str) -> str:
        return (self.call(key, 'GET', f'/companies/{self.company}') or {}).get(
            'issuePrefix'
        ) or ''


def as_list(data: Any) -> list:
    if isinstance(data, dict):
        for k in ('items', 'data', 'approvals', 'issues', 'interactions', 'agents'):
            if isinstance(data.get(k), list):
                return data[k]
        return []
    return data if isinstance(data, list) else []


# --- configuration --------------------------------------------------------------------------------
@dataclass
class Approver:
    name: str
    chat_id: Any  # Telegram user ID (int) or Slack user ID ('U…')
    key: str = field(repr=False)
    kinds: set[str]
    paperclip_user_id: str = ''  # optional; limits review items to this person's own reviews


@dataclass
class Config:
    token: str = field(repr=False)  # Telegram bot token, or the Slack bot token
    api_url: str
    public_url: str
    company: str
    reader_key: str = field(repr=False)
    approvers: list[Approver]
    state_file: Path
    audit_file: Path
    tz: ZoneInfo
    chat: str = 'telegram'
    slack_app_token: str = field(default='', repr=False)
    slack_channel: str = ''
    label: str = ''  # e.g. the company's name, when several boards share one channel


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)

    def need(name: str) -> str:
        value = (env.get(name) or '').strip()
        if not value:
            raise BotError(f'{name} must be set')
        return value

    chat = (env.get('APPROVALS_CHAT') or 'telegram').strip().lower()
    if chat not in CHATS:
        raise BotError(f'APPROVALS_CHAT must be one of {sorted(CHATS)}')
    id_field = 'telegram_id' if chat == 'telegram' else 'slack_user_id'
    approvers = []
    try:
        raw = json.loads(Path(need('APPROVERS_FILE')).read_text())
    except (OSError, ValueError) as exc:
        raise BotError(f'APPROVERS_FILE could not be read: {exc}') from exc
    for entry in raw:
        kinds = set(entry.get('kinds') or [])
        if not kinds or kinds - KINDS:
            raise BotError(
                f'approver {entry.get("name")}: kinds must be from {sorted(KINDS)}'
            )
        if not entry.get(id_field):
            raise BotError(
                f'approver {entry.get("name")}: {id_field} is needed for {chat}'
            )
        chat_id = int(entry[id_field]) if chat == 'telegram' else str(entry[id_field])
        approvers.append(
            Approver(
                name=str(entry['name']),
                chat_id=chat_id,
                key=need(entry['key_env']),
                kinds=kinds,
                paperclip_user_id=str(entry.get('paperclip_user_id') or ''),
            )
        )
    if not approvers:
        raise BotError('APPROVERS_FILE lists no approvers')
    api = need('PAPERCLIP_API_URL')
    slack = chat == 'slack'
    return Config(
        token=need('SLACK_BOT_TOKEN' if slack else 'TELEGRAM_BOT_TOKEN'),
        api_url=api,
        public_url=(env.get('PAPERCLIP_PUBLIC_URL') or api).rstrip('/'),
        company=need('PAPERCLIP_COMPANY_ID'),
        reader_key=need(need('READER_KEY_ENV')),
        approvers=approvers,
        state_file=Path(env.get('APPROVALS_STATE_FILE') or 'approvals-state.json'),
        audit_file=Path(env.get('APPROVALS_AUDIT_FILE') or 'approvals-audit.jsonl'),
        tz=ZoneInfo(env.get('APPROVALS_TZ') or 'America/New_York'),
        chat=chat,
        slack_app_token=need('SLACK_APP_TOKEN') if slack else '',
        slack_channel=need('SLACK_APPROVALS_CHANNEL') if slack else '',
        label=(env.get('APPROVALS_LABEL') or '').strip(),
    )


def load_env_file(path: str) -> None:
    """KEY=value lines into os.environ (values never printed); existing variables win."""
    for line in Path(path).read_text().splitlines():
        key, sep, val = line.partition('=')
        key = key.strip()
        if sep and key and not key.startswith('#'):
            os.environ.setdefault(key, val.strip().strip('"\''))


# --- items ----------------------------------------------------------------------------------------
@dataclass
class Item:
    key: str  # 'a:<approval id>', 'c:<card id>', 'q:<question card id>' or 'r:<issue id>'
    # (fits Telegram's 64 bytes)
    kind: str  # 'board', 'card' (question cards are cards: the same approvers answer them) or 'review'
    id: str
    title: str
    lines: list[str]
    url: str
    issue_id: str = ''
    questions: list[dict] = field(default_factory=list)  # question cards only, from the payload
    reviewer: str = ''  # review items only: the Paperclip user the task is assigned to


def plain(text: Any, limit: int) -> str:
    """Markdown-ish text flattened to one tidy paragraph for a chat message. Agents write this
    text, so it's cut to a bounded length before any pattern runs, and the patterns are bounded
    too: a huge field can't stall the bot."""
    out = str(text or '')[: limit * 4 + 2000]
    out = re.sub(r'\*\*|__|`', '', out)
    out = re.sub(r'\[([^\]\n]{1,500})\]\([^)\s]{1,2000}\)', r'\1', out)
    out = ' '.join(part.strip() for part in out.splitlines() if part.strip())
    return out if len(out) <= limit else out[: limit - 1].rstrip() + '…'


def approval_item(a: dict, agents: dict[str, str], public: str, prefix: str) -> Item:
    p: dict = a['payload'] if isinstance(a.get('payload'), dict) else {}
    label = APPROVAL_LABELS.get(str(a.get('type')), 'Approval')
    name = p.get('name') or p.get('title') or a.get('type') or 'request'
    title = f'{label}: {name}' + (
        f' · {p["title"]}' if p.get('title') and p.get('name') else ''
    )
    asker = agents.get(a.get('requestedByAgentId') or p.get('requestedByAgentId') or '') or (
        'The board' if a.get('requestedByUserId') else 'Someone'
    )
    facts = [f'{asker} asked']
    if p.get('reportsTo'):
        facts.append(f'reports to {agents.get(p["reportsTo"], "another agent")}')
    model = (
        (p.get('adapterConfig') or {}).get('model')
        if isinstance(p.get('adapterConfig'), dict)
        else None
    )
    if model:
        facts.append(str(model))
    lines = [' · '.join(facts)]
    if isinstance(p.get('budgetMonthlyCents'), int) and p['budgetMonthlyCents'] > 0:
        lines.append(f'Budget: ${p["budgetMonthlyCents"] / 100:,.0f}/mo')
    if p.get('summary') or p.get('reason'):
        lines.append(plain(p.get('summary') or p.get('reason'), DETAILS_MAX))
    return Item(
        key=f'a:{a["id"]}',
        kind='board',
        id=a['id'],
        title=title,
        lines=lines,
        url=f'{public}/{prefix}/approvals/{a["id"]}',
    )


def card_item(
    card: dict, issue: dict, agents: dict[str, str], public: str, prefix: str
) -> Item:
    p: dict = card['payload'] if isinstance(card.get('payload'), dict) else {}
    ref = issue.get('identifier') or issue.get('id', '')[:8]
    who = agents.get(card.get('createdByAgentId') or '', 'a teammate')
    lines = [f'{ref}, {who}']
    if p.get('detailsMarkdown'):
        lines.append(plain(p['detailsMarkdown'], DETAILS_MAX))
    return Item(
        key=f'c:{card["id"]}',
        kind='card',
        id=card['id'],
        issue_id=issue['id'],
        title=plain(p.get('prompt') or card.get('title') or 'Needs your OK', 200),
        lines=lines,
        url=f'{public}/{prefix}/issues/{ref}',
    )


def question_item(
    card: dict, issue: dict, agents: dict[str, str], public: str, prefix: str
) -> Item:
    """A question card: every question in full, numbered, with its choices, so it can be read
    and answered from the chat alone."""
    p: dict = card['payload'] if isinstance(card.get('payload'), dict) else {}
    ref = issue.get('identifier') or issue.get('id', '')[:8]
    who = agents.get(card.get('createdByAgentId') or '', 'a teammate')
    questions = [q for q in p.get('questions') or [] if isinstance(q, dict) and q.get('id')]
    lines = [f'{ref}, {who} asks:']
    for n, q in enumerate(questions, 1):
        labels = [str(o.get('label')) for o in q.get('options') or [] if o.get('label')]
        if q.get('allowOther') or any(o.get('freeText') for o in q.get('options') or []):
            labels.append('or your own answer')
        line = f'{n}. {plain(q.get("prompt"), DETAILS_MAX)}'
        if labels:
            line += f' ({"; ".join(plain(x, 200) for x in labels)})'
        lines.append(line)
    return Item(
        key=f'q:{card["id"]}',
        kind='card',
        id=card['id'],
        issue_id=issue['id'],
        title=plain(card.get('title') or p.get('title') or 'Questions for you', 200),
        lines=lines,
        url=f'{public}/{prefix}/issues/{ref}',
        questions=questions,
    )


def review_item(issue: dict, agents: dict[str, str], public: str, prefix: str) -> Item:
    """A task waiting on a person's review: who handed it over, and a link. No buttons."""
    ref = issue.get('identifier') or issue['id'][:8]
    who = agents.get(issue.get('createdByAgentId') or '', 'A teammate')
    return Item(
        key=f'r:{issue["id"]}',
        kind='review',
        id=issue['id'],
        issue_id=issue['id'],
        title=plain(issue.get('title') or 'A task', 200),
        lines=[f'{ref}, {who} put it in review for you'],
        url=f'{public}/{prefix}/issues/{ref}',
        reviewer=str(issue.get('assigneeUserId') or ''),
    )


def is_review(issue: dict) -> bool:
    return issue.get('status') == 'in_review' and bool(issue.get('assigneeUserId'))


def is_open_card(card: dict) -> bool:
    return (
        isinstance(card, dict)
        and card.get('kind') in CARD_KINDS
        and card.get('status') == 'pending'
        and bool(card.get('id'))
    )


def collect(pc: Paperclip, key: str, public: str) -> dict[str, Item]:
    """Everything currently waiting on a person, by item key."""
    agents, prefix = pc.agents(key), pc.prefix(key)
    items = {}
    for a in pc.pending_approvals(key):
        if (
            isinstance(a, dict)
            and a.get('id')
            and a.get('status', 'pending') == 'pending'
        ):
            item = approval_item(a, agents, public, prefix)
            items[item.key] = item
    for issue in pc.open_issues(key):
        if not (isinstance(issue, dict) and issue.get('id')):
            continue
        if is_review(issue):
            item = review_item(issue, agents, public, prefix)
            items[item.key] = item
        for card in pc.cards(key, issue['id']):
            if is_open_card(card):
                make = question_item if card['kind'] == 'ask_user_questions' else card_item
                item = make(card, issue, agents, public, prefix)
                items[item.key] = item
    return items


def still_pending(pc: Paperclip, key: str, item_key: str, issue_id: str) -> bool:
    kind, _, ident = item_key.partition(':')
    if kind == 'a':
        return (pc.call(key, 'GET', f'/approvals/{ident}') or {}).get(
            'status'
        ) == 'pending'
    return any(is_open_card(c) and c['id'] == ident for c in pc.cards(key, issue_id))


def decide(
    pc: Paperclip, key: str, item_key: str, issue_id: str, approve: bool
) -> None:
    kind, _, ident = item_key.partition(':')
    if kind == 'a':
        pc.call(
            key,
            'POST',
            f'/approvals/{ident}/{"approve" if approve else "reject"}',
            {'decisionNote': 'Decided in chat (approvals bot).'},
        )
    else:
        pc.call(
            key,
            'POST',
            f'/issues/{issue_id}/interactions/{ident}/{"accept" if approve else "reject"}',
            # Some cards require a decline reason (payload.rejectRequiresReason) and answer 422
            # without one; v1 has no typed reasons, so "Not now" says where it came from.
            {} if approve else {'reason': NOT_NOW_REASON},
        )


def respond(pc: Paperclip, key: str, item_key: str, issue_id: str, answers: list[dict]) -> None:
    """Answer a question card as the approver (their key), which resolves it and wakes the
    teammate who asked (continuation policy wake_assignee)."""
    ident = item_key.partition(':')[2]
    pc.call(key, 'POST', f'/issues/{issue_id}/interactions/{ident}/respond', {'answers': answers})


def free_text_allowed(q: dict) -> bool:
    return bool(q.get('allowOther')) or any(o.get('freeText') for o in q.get('options') or [])


def answer_summary(questions: list[dict], answers: list[dict]) -> str:
    """'Acme Corp; Reuse as-is' for the closed message."""
    labels = {
        (q['id'], o.get('id')): str(o.get('label'))
        for q in questions
        for o in q.get('options') or []
    }
    parts = []
    for a in answers:
        picked = [str(labels.get((a['questionId'], oid), oid)) for oid in a.get('optionIds') or []]
        if a.get('otherText'):
            picked.append(f'"{a["otherText"]}"')
        parts.append(', '.join(picked))
    return plain('; '.join(p for p in parts if p), 300)


def link_label(item: Item) -> str:
    if item.key.startswith('q:'):
        return 'answer in Paperclip'
    if item.kind == 'review':
        return 'open the task in Paperclip'
    return 'open in Paperclip' if item.kind == 'board' else 'open the card in Paperclip'


# --- chat: Telegram -------------------------------------------------------------------------------
class Telegram:
    def __init__(self, token: str):
        self.base = f'https://api.telegram.org/bot{token}'

    def call(self, method: str, body: dict, timeout: float = TIMEOUT) -> Any:
        out = request_json(f'{self.base}/{method}', 'POST', body, timeout=timeout)
        if not (isinstance(out, dict) and out.get('ok')):
            raise ApiError(f'Telegram {method} failed: {str(out)[:200]}')
        return out.get('result')

    def updates(self, offset: int, wait: int) -> list[dict]:
        return (
            self.call(
                'getUpdates',
                {
                    'offset': offset,
                    'timeout': wait,
                    'allowed_updates': ['message', 'callback_query'],
                },
                timeout=wait + TIMEOUT,
            )
            or []
        )

    def send(self, chat_id: int, text: str, buttons: list | None) -> int:
        body = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'link_preview_options': {'is_disabled': True},
        }
        if buttons:
            body['reply_markup'] = {'inline_keyboard': buttons}
        return self.call('sendMessage', body)['message_id']

    def edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            self.call(
                'editMessageText',
                {
                    'chat_id': chat_id,
                    'message_id': message_id,
                    'text': text,
                    'parse_mode': 'HTML',
                    'link_preview_options': {'is_disabled': True},
                },
            )
        except ApiError as exc:
            if 'message is not modified' not in str(exc):
                raise

    def answer(self, callback_id: str, text: str) -> None:
        self.call(
            'answerCallbackQuery',
            {'callback_query_id': callback_id, 'text': text[:190]},
        )


def message_text(item: Item, outcome: str = '') -> str:
    """Telegram HTML."""
    head = '🟡 ' if not outcome else ''
    body = [f'<b>{head}{escape(item.title)}</b>'] + [
        escape(line) for line in item.lines
    ]
    body.append(f'<a href="{escape(item.url, quote=True)}">{link_label(item)}</a>')
    if outcome:
        body.append(escape(outcome))
    return '\n'.join(body)


def buttons(item: Item) -> list:
    if item.key.startswith('q:'):
        return []  # answered in Paperclip from Telegram for now (buttons there: a follow-up)
    if item.kind == 'review':
        return []  # reviewing means opening the work in Paperclip
    return [
        [
            {'text': 'Approve', 'callback_data': f'y|{item.key}'},
            {'text': 'Not now', 'callback_data': f'n|{item.key}'},
        ]
    ]


@dataclass
class Press:
    """A button press, whichever chat it came from."""

    user: Any
    data: str  # 'y|<item key>', 'n|<item key>', or 's|<item key>' (answers submitted)
    reply: Callable[[str], None]  # a short private answer to the presser
    answers: list[dict] = field(default_factory=list)  # 's' only: Paperclip answer objects


class TelegramChat:
    """One direct message per approver; /start answers with the sender's ID."""

    def __init__(self, tg: Any):
        self.tg = tg

    def post(self, item: Item, approvers: list[Approver]) -> list[list]:
        sent = []
        for a in approvers:
            try:
                sent.append(
                    [
                        a.chat_id,
                        self.tg.send(a.chat_id, message_text(item), buttons(item)),
                    ]
                )
            except ApiError as exc:
                log(f'could not message {a.name} about {item.key}: {exc}')
        return sent

    def update(self, ref: list, item: Item, outcome: str) -> None:
        self.tg.edit(ref[0], ref[1], message_text(item, outcome))

    def press(self, update: dict) -> Press | None:
        """A Telegram update as a Press; answers /start itself; ignores everything else."""
        if 'callback_query' in update:
            cq = update['callback_query']
            return Press(
                user=(cq.get('from') or {}).get('id'),
                data=str(cq.get('data') or ''),
                reply=lambda text, cid=cq['id']: self.tg.answer(cid, text),
            )
        msg = update.get('message') or {}
        chat = msg.get('chat') or {}
        if (
            chat.get('type') == 'private'
            and (msg.get('text') or '').strip().split(' ')[0] == '/start'
        ):
            uid = (msg.get('from') or {}).get('id')
            self.tg.send(
                chat['id'],
                f'Your Telegram ID is {uid}. Send it to your Paperclip admin to be added '
                'as an approver. This bot only posts approvals; it does not read messages.',
                None,
            )
        return None  # Everything else is ignored: this bot takes no instructions.

    def events(self, state: dict, wait: int) -> list[Press]:
        out = []
        for update in self.tg.updates(state['offset'], wait):
            state['offset'] = update['update_id'] + 1
            pressed = self.press(update)
            if pressed:
                out.append(pressed)
        return out


# --- chat: Slack ----------------------------------------------------------------------------------
def slack_escape(text: str) -> str:
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def slack_blocks(item: Item, mentions: list[str], outcome: str = '') -> list[dict]:
    """Slack Block Kit: the item, @mentions while it's open, buttons until it's decided."""
    who = ' '.join(f'<@{m}>' for m in mentions)
    head = f'{who} 🟡 ' if not outcome else ''
    text = '\n'.join(
        [f'{head}*{slack_escape(item.title)}*']
        + [slack_escape(line) for line in item.lines]
        + [f'<{item.url}|{link_label(item)}>']
    )
    blocks: list[dict] = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]
    if outcome:
        blocks.append(
            {
                'type': 'context',
                'elements': [{'type': 'mrkdwn', 'text': slack_escape(outcome)}],
            }
        )
    elif item.kind == 'review':
        pass  # a link only: reviewing means opening the work in Paperclip
    elif item.key.startswith('q:'):
        blocks.append(
            {
                'type': 'actions',
                'elements': [
                    {
                        'type': 'button',
                        'action_id': 'answer',
                        'style': 'primary',
                        'text': {'type': 'plain_text', 'text': 'Answer'},
                        'value': f'a|{item.key}',
                    }
                ],
            }
        )
    else:
        blocks.append(
            {
                'type': 'actions',
                'elements': [
                    {
                        'type': 'button',
                        'action_id': 'approve',
                        'style': 'primary',
                        'text': {'type': 'plain_text', 'text': 'Approve'},
                        'value': f'y|{item.key}',
                    },
                    {
                        'type': 'button',
                        'action_id': 'not_now',
                        'text': {'type': 'plain_text', 'text': 'Not now'},
                        'value': f'n|{item.key}',
                    },
                ],
            }
        )
    return blocks


def slack_text(text: str, limit: int) -> str:
    text = str(text or '')
    return text if len(text) <= limit else text[: limit - 1].rstrip() + '…'


def answer_form(item: Item, channel: str) -> dict:
    """The Slack form for a question card: per question its full text, a choice (radio buttons or
    checkboxes up to 10 options, a menu beyond that) and, where the card allows it, a free-text box.
    Inputs are named by question number, so long question ids never hit Slack's limits."""
    blocks: list[dict] = []
    for n, q in enumerate(item.questions):
        multi = q.get('selectionMode') == 'multi'
        options = [
            {
                'text': {'type': 'plain_text', 'text': slack_text(o.get('label'), SLACK_OPTION_TEXT)},
                'value': str(i),
            }
            for i, o in enumerate(q.get('options') or [])
            if o.get('label')
        ][:SLACK_SELECT_MAX]
        blocks.append(
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': slack_text(f'*{n + 1}. {slack_escape(str(q.get("prompt") or ""))}*', 2900),
                },
            }
        )
        if options:
            if len(options) <= SLACK_CHOICES_MAX:
                element = {'type': 'checkboxes' if multi else 'radio_buttons', 'options': options}
            else:
                element = {
                    'type': 'multi_static_select' if multi else 'static_select',
                    'placeholder': {'type': 'plain_text', 'text': 'Pick'},
                    'options': options,
                }
            element['action_id'] = 'choice'
            blocks.append(
                {
                    'type': 'input',
                    'block_id': f'q{n}',
                    'label': {'type': 'plain_text', 'text': 'Pick any' if multi else 'Your answer'},
                    'optional': not q.get('required') or free_text_allowed(q),
                    'element': element,
                }
            )
        if free_text_allowed(q) or not options:
            blocks.append(
                {
                    'type': 'input',
                    'block_id': f'o{n}',
                    'label': {'type': 'plain_text', 'text': 'Or in your own words' if options else 'Your answer'},
                    'optional': True,
                    'element': {'type': 'plain_text_input', 'action_id': 'other', 'multiline': True},
                }
            )
    return {
        'type': 'modal',
        'callback_id': ANSWER_FORM,
        'private_metadata': json.dumps({'key': item.key, 'channel': channel}),
        'title': {'type': 'plain_text', 'text': 'Answer'},
        'submit': {'type': 'plain_text', 'text': 'Send answers'},
        'close': {'type': 'plain_text', 'text': 'Cancel'},
        'blocks': blocks,
    }


def read_answers(item: Item, values: dict) -> tuple[list[dict], dict[str, str]]:
    """A submitted form as Paperclip answers, plus any errors to show in the form (by block id).
    Mirrors Paperclip's own checks: known options, one pick for single questions, required answered."""
    answers, errors = [], {}
    for n, q in enumerate(item.questions):
        opts = q.get('options') or []
        choice = (values.get(f'q{n}') or {}).get('choice') or {}
        picked = choice.get('selected_options') or (
            [choice['selected_option']] if choice.get('selected_option') else []
        )
        ids = []
        for opt in picked:
            try:
                ids.append(opts[int(opt.get('value'))]['id'])
            except (TypeError, ValueError, IndexError, KeyError):
                errors[f'q{n}'] = 'That choice is no longer on the card.'
        if q.get('selectionMode') != 'multi' and len(ids) > 1:
            errors[f'q{n}'] = 'Pick one.'
        other = (((values.get(f'o{n}') or {}).get('other') or {}).get('value') or '').strip()
        if q.get('required') and not ids and not other:
            errors[f'q{n}' if opts else f'o{n}'] = 'This one needs an answer.'
        if ids or other:
            answers.append(
                {'questionId': q['id'], 'optionIds': ids, **({'otherText': other} if other else {})}
            )
    return answers, errors


def notice(item: Item) -> str:
    if item.key.startswith('q:'):
        return 'Question for you'
    return 'Ready for your review' if item.kind == 'review' else 'Approval needed'


class SlackApi:
    def __init__(self, token: str):
        self.token = token

    def call(self, method: str, body: dict) -> dict:
        out = request_json(
            f'https://slack.com/api/{method}',
            'POST',
            body,
            {'Authorization': f'Bearer {self.token}'},
        )
        if not (isinstance(out, dict) and out.get('ok')):
            raise ApiError(f'Slack {method} failed: {(out or {}).get("error", out)}')
        return out


class SlackChat:
    """One message per item in the approvals channel, @mentioning its approvers (so phones buzz).
    Button presses arrive over Socket Mode (an outbound websocket). The app subscribes to no
    message events, so it never sees what people write."""

    def __init__(self, api: Any, channel: str, app_token: str = '', socket: Any = None):
        self.api, self.channel = api, channel
        self.inbox: queue.Queue = queue.Queue()
        # Set by the Bot: (user, item key) -> (the item, or None with a refusal) for answer forms.
        self.form_item: Callable[[Any, str], tuple[Item | None, str]] = lambda u, k: (None, 'Not ready.')
        self.socket = socket
        if socket is None and app_token:
            self.socket = self.connect(app_token)

    def on_interactive(self, payload: dict) -> dict | None:
        """Runs as each press or form arrives, before Slack's 3-second limits: an Answer press opens
        the form here (its trigger expires fast), and a submitted form is checked here so mistakes
        show in the form. Everything else goes to the main loop. Returns the reply for Slack."""
        kind = payload.get('type')
        user = (payload.get('user') or {}).get('id')
        if kind == 'block_actions':
            action = (payload.get('actions') or [{}])[0]
            if action.get('action_id') == 'answer':
                key = str(action.get('value') or '').partition('|')[2]
                channel = ((payload.get('channel') or {}).get('id')) or self.channel
                item, refusal = self.form_item(user, key)
                if item is None:
                    self.ephemeral(channel, user, refusal)
                    return None
                try:
                    self.api.call(
                        'views.open',
                        {'trigger_id': payload.get('trigger_id'), 'view': answer_form(item, channel)},
                    )
                except ApiError as exc:
                    log(f'could not open the answer form for {key}: {exc}')
                    self.ephemeral(channel, user, 'Could not open the form; answer in Paperclip.')
                return None
        if kind == 'view_submission':
            view = payload.get('view') or {}
            if view.get('callback_id') != ANSWER_FORM:
                return None
            try:
                meta = json.loads(view.get('private_metadata') or '{}')
            except ValueError:
                return None
            item, refusal = self.form_item(user, str(meta.get('key') or ''))
            if item is None:
                return {'response_action': 'errors', 'errors': {self.first_block(view): refusal}}
            answers, errors = read_answers(item, (view.get('state') or {}).get('values') or {})
            if errors:
                return {'response_action': 'errors', 'errors': errors}
            payload = {**payload, '_answers': answers, '_key': item.key,
                       '_channel': meta.get('channel') or self.channel}
        self.inbox.put(payload)
        return None

    @staticmethod
    def first_block(view: dict) -> str:
        ids = [b.get('block_id') for b in view.get('blocks') or [] if b.get('type') == 'input']
        return ids[0] if ids else 'q0'

    def ephemeral(self, channel: str, user: Any, text: str) -> None:
        try:
            self.api.call('chat.postEphemeral', {'channel': channel, 'user': user, 'text': text})
        except ApiError as exc:
            log(f'could not answer a press: {exc}')

    def connect(self, app_token: str) -> Any:
        from slack_sdk.socket_mode.builtin import SocketModeClient
        from slack_sdk.socket_mode.response import SocketModeResponse

        client = SocketModeClient(app_token=app_token)

        def on_request(c, req):
            reply = None
            if req.type == 'interactive':
                try:
                    reply = self.on_interactive(req.payload)
                except Exception as exc:  # never leave Slack waiting on an ack
                    log(f'interactive payload failed: {exc}')
            c.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id, payload=reply)
            )

        client.socket_mode_request_listeners.append(on_request)
        client.connect()
        return client

    def post(self, item: Item, approvers: list[Approver]) -> list[list]:
        mentions = [str(a.chat_id) for a in approvers]
        out = self.api.call(
            'chat.postMessage',
            {
                'channel': self.channel,
                # notification text; mentions below buzz
                'text': f'{notice(item)}: {slack_escape(item.title)}',
                'blocks': slack_blocks(item, mentions),
                'unfurl_links': False,
                'unfurl_media': False,
            },
        )
        return [[out['channel'], out['ts']]]

    def update(self, ref: list, item: Item, outcome: str) -> None:
        self.api.call(
            'chat.update',
            {
                'channel': ref[0],
                'ts': ref[1],
                'text': f'{slack_escape(item.title)}: {slack_escape(outcome)}',
                'blocks': slack_blocks(item, [], outcome),
            },
        )

    def press(self, payload: dict) -> Press | None:
        user = (payload.get('user') or {}).get('id')
        if payload.get('type') == 'view_submission' and payload.get('_key'):
            channel = payload.get('_channel') or self.channel
            return Press(
                user=user,
                data=f's|{payload["_key"]}',
                reply=lambda text: self.ephemeral(channel, user, text),
                answers=payload.get('_answers') or [],
            )
        if payload.get('type') != 'block_actions':
            return None
        actions = payload.get('actions') or []
        if not actions or actions[0].get('action_id') == 'answer':
            return None  # Answer presses are handled as they arrive (on_interactive)
        channel = ((payload.get('channel') or {}).get('id')) or self.channel
        return Press(
            user=user,
            data=str(actions[0].get('value') or ''),
            reply=lambda text: self.ephemeral(channel, user, text),
        )

    def events(self, state: dict, wait: int) -> list[Press]:
        out = []
        deadline = time.time() + wait
        while True:
            try:
                payload = self.inbox.get(timeout=max(0.0, deadline - time.time()))
            except queue.Empty:
                break
            pressed = self.press(payload)
            if pressed:
                out.append(pressed)
            if not self.inbox.qsize():
                break
        return out


# --- the bot --------------------------------------------------------------------------------------
class Bot:
    def __init__(
        self,
        cfg: Config,
        pc: Paperclip | None = None,
        tg: Any = None,
        now=time.time,
        chat: Any = None,
    ):
        self.cfg, self.now = cfg, now
        self.pc = pc or Paperclip(cfg.api_url, cfg.company)
        if chat is not None:
            self.chat = chat
        elif cfg.chat == 'slack':
            self.chat = SlackChat(
                SlackApi(cfg.token), cfg.slack_channel, cfg.slack_app_token
            )
        else:
            self.chat = TelegramChat(tg or Telegram(cfg.token))
        self.state = self.load_state()
        if isinstance(self.chat, SlackChat):
            self.chat.form_item = self.form_item

    # state: {"offset": int, "items": {key: {"kind", "issue_id", "item", "messages": [[chat, id]], "done"}}}
    def load_state(self) -> dict:
        try:
            data = json.loads(self.cfg.state_file.read_text())
            if isinstance(data, dict):
                data.setdefault('offset', 0)
                data.setdefault('items', {})
                return data
        except (OSError, ValueError):
            pass
        return {'offset': 0, 'items': {}}

    def save_state(self) -> None:
        tmp = self.cfg.state_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, indent=1))
        tmp.replace(self.cfg.state_file)

    def audit(self, **entry) -> None:
        entry = {
            'at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
            **entry,
        }
        with self.cfg.audit_file.open('a') as f:
            f.write(json.dumps(entry) + '\n')

    def stamp(self) -> str:
        return dt.datetime.fromtimestamp(self.now(), self.cfg.tz).strftime('%-I:%M %p')

    def approver(self, user: Any) -> Approver | None:
        return next(
            (a for a in self.cfg.approvers if str(a.chat_id) == str(user)), None
        )

    # -- sync with Paperclip
    def sync(self) -> None:
        current = collect(self.pc, self.cfg.reader_key, self.cfg.public_url)
        known = self.state['items']
        for key, item in current.items():
            # A task back in review after it left is a new hand-off; anything else posts once.
            if key in known and not (key.startswith('r:') and known[key].get('done')):
                continue
            allowed = [a for a in self.cfg.approvers if self.may_see(a, item)]
            if self.cfg.label:
                item.title = f'{self.cfg.label} · {item.title}'
            try:
                sent = self.chat.post(item, allowed) if allowed else []
            except ApiError as exc:
                log(f'could not post {key}: {exc}')
                continue  # not recorded, so the next sync tries again
            known[key] = {
                'kind': item.kind,
                'issue_id': item.issue_id,
                'item': item.__dict__,
                'messages': sent,
                'done': False,
            }
        for key, rec in known.items():
            if not rec.get('done') and key not in current:
                self.close(
                    key,
                    'Out of review in Paperclip.' if key.startswith('r:') else 'Handled in Paperclip.',
                )
        # Forget finished items after a week so the state file stays small.
        cutoff = self.now() - 7 * 86400
        for key in [
            k
            for k, r in known.items()
            if r.get('done') and r.get('done_at', self.now()) < cutoff
        ]:
            del known[key]
        self.save_state()

    @staticmethod
    def may_see(a: Approver, item: Item) -> bool:
        if item.kind not in a.kinds:
            return False
        return item.kind != 'review' or not a.paperclip_user_id or a.paperclip_user_id == item.reviewer

    def close(self, key: str, outcome: str) -> None:
        rec = self.state['items'][key]
        item = Item(**rec['item'])
        for ref in rec.get('messages', []):
            try:
                self.chat.update(ref, item, outcome)
            except ApiError as exc:
                log(f'could not update a message about {key}: {exc}')
        rec.update(done=True, done_at=self.now(), outcome=outcome)

    def form_item(self, user: Any, key: str) -> tuple[Item | None, str]:
        """The open question card an approver may answer, or None and why not (refusals logged)."""
        rec = self.state['items'].get(key)
        if not key.startswith('q:') or rec is None:
            return None, 'That item is no longer tracked.'
        who = self.approver(user)
        if who is None or rec['kind'] not in who.kinds:
            self.audit(user=user, item=key, action='answer', result='refused: not an approver for this')
            return None, 'Only an approver can answer this.'
        if rec.get('done'):
            return None, 'Already answered.'
        return Item(**rec['item']), ''

    # -- presses
    def handle(self, update: dict) -> None:
        """One raw chat update (Telegram update or Slack interactive payload)."""
        pressed = self.chat.press(update)
        if pressed:
            self.decide_press(pressed)

    def decide_press(self, p: Press) -> None:
        choice, _, key = p.data.partition('|')
        who = self.approver(p.user)
        rec = self.state['items'].get(key)
        if choice == 's' and key.startswith('q:') and rec is not None:
            self.answer_press(p, key, rec, who)
            return
        if choice not in ('y', 'n') or rec is None or key.startswith(('q:', 'r:')):
            p.reply('That item is no longer tracked.')
            return
        if who is None or rec['kind'] not in who.kinds:
            self.audit(
                user=p.user,
                item=key,
                action=choice,
                result='refused: not an approver for this',
            )
            p.reply('Only an approver can decide this.')
            return
        approve = choice == 'y'
        if rec.get('done'):
            p.reply('Already decided.')
            return
        try:
            if not still_pending(self.pc, who.key, key, rec.get('issue_id', '')):
                self.close(key, 'Handled in Paperclip.')
                self.save_state()
                p.reply('Already decided in Paperclip.')
                return
            decide(self.pc, who.key, key, rec.get('issue_id', ''), approve)
        except ApiError as exc:
            self.audit(
                approver=who.name,
                item=key,
                action='approve' if approve else 'reject',
                result=f'error: {exc}',
            )
            p.reply(
                'Paperclip refused or was unreachable; nothing changed. Try Paperclip.'
            )
            log(f'decision on {key} by {who.name} failed: {exc}')
            return
        kind, _, ident = key.partition(':')
        if kind == 'a':
            try:
                self.pc.dismiss_inbox(who.key, ident)
            except ApiError as exc:
                log(f'could not clear {key} from the Paperclip inbox: {exc}')
        outcome = (
            f'✅ Approved by {who.name} · {self.stamp()}'
            if approve
            else f'Not now · {who.name} · {self.stamp()}'
        )
        self.close(key, outcome)
        self.save_state()
        self.audit(
            approver=who.name,
            item=key,
            action='approve' if approve else 'reject',
            result='recorded',
        )
        p.reply('Approved.' if approve else 'Marked not now.')

    def answer_press(self, p: Press, key: str, rec: dict, who: Approver | None) -> None:
        """Submitted answers to a question card: same checks as a decision, then /respond."""
        if who is None or rec['kind'] not in who.kinds:
            self.audit(user=p.user, item=key, action='answer', result='refused: not an approver for this')
            p.reply('Only an approver can answer this.')
            return
        if rec.get('done'):
            p.reply('Already answered.')
            return
        try:
            if not still_pending(self.pc, who.key, key, rec.get('issue_id', '')):
                self.close(key, 'Handled in Paperclip.')
                self.save_state()
                p.reply('Already answered in Paperclip.')
                return
            respond(self.pc, who.key, key, rec.get('issue_id', ''), p.answers)
        except ApiError as exc:
            self.audit(approver=who.name, item=key, action='answer', result=f'error: {exc}')
            p.reply('Paperclip refused or was unreachable; nothing changed. Try Paperclip.')
            log(f'answer to {key} by {who.name} failed: {exc}')
            return
        summary = answer_summary(rec['item'].get('questions') or [], p.answers)
        self.close(key, f'✅ Answered by {who.name} · {self.stamp()}' + (f': {summary}' if summary else ''))
        self.save_state()
        self.audit(approver=who.name, item=key, action='answer', result='recorded')
        p.reply('Answers sent.')

    def run(self, once: bool = False) -> None:
        last_sync = 0.0
        while True:
            if self.now() - last_sync >= POLL_SECONDS:
                try:
                    self.sync()
                except ApiError as exc:
                    log(f'sync failed: {exc}')
                last_sync = self.now()
            if once:
                return
            try:
                for pressed in self.chat.events(self.state, POLL_SECONDS):
                    try:
                        self.decide_press(pressed)
                    except ApiError as exc:
                        log(f'press failed: {exc}')
                self.save_state()
            except ApiError as exc:
                log(f'chat updates failed: {exc}')
                time.sleep(5)


def log(message: str) -> None:
    print(
        f'{dt.datetime.now().isoformat(timespec="seconds")} {message}',
        file=sys.stderr,
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Paperclip approvals in chat')
    parser.add_argument('--once', action='store_true', help='one sync pass, then exit')
    parser.add_argument(
        '--env-file',
        action='append',
        default=[],
        help='KEY=value file to load first (repeatable; values never printed)',
    )
    args = parser.parse_args(argv)
    try:
        for path in args.env_file:
            load_env_file(path)
        bot = Bot(load_config())
    except (BotError, OSError) as exc:
        log(f'not starting: {exc}')
        return 2
    log(
        f'approvals bot up ({bot.cfg.chat}): {len(bot.cfg.approvers)} approver(s), '
        f'company {bot.cfg.company[:8]}'
    )
    bot.run(once=args.once)
    return 0


if __name__ == '__main__':
    sys.exit(main())
