"""Tests for the approvals bot. Run: python -m pytest -q"""

import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import approvals as ap  # noqa: E402

ME, SAM, HELPER = 100000001, 555, 'helper-agent'
NOW = float(1790991660)  # 2026-10-02 21:41 Eastern


class FakePaperclip:
    """Pending approvals and cards; records decisions with the key that made them."""

    def __init__(self):
        self.approvals = {}
        self.issues = {}
        self.card_map = {}  # issue id -> list
        self.decisions = []
        self.bodies = []
        self.dismissed = []
        self.fail = False
        self.fail_dismiss = False

    def call(self, key, method, path, body=None):
        if self.fail:
            raise ap.ApiError('down')
        if method == 'GET' and path.startswith('/approvals/'):
            return self.approvals[path.split('/')[2]]
        if method == 'POST':
            self.decisions.append((key, path))
            self.bodies.append(body)
            parts = path.strip('/').split('/')
            if parts[0] == 'approvals':
                self.approvals[parts[1]]['status'] = (
                    'approved' if parts[2] == 'approve' else 'rejected'
                )
            else:
                done = {'accept': 'accepted', 'reject': 'rejected', 'respond': 'answered'}
                for c in self.card_map[parts[1]]:
                    if c['id'] == parts[3]:
                        c['status'] = done[parts[4]]
            return {}
        raise AssertionError(path)

    def dismiss_inbox(self, key, approval_id):
        if self.fail_dismiss:
            raise ap.ApiError('dismiss refused')
        self.dismissed.append((key, approval_id))

    def pending_approvals(self, key):
        return [a for a in self.approvals.values() if a['status'] == 'pending']

    def open_issues(self, key):
        return list(self.issues.values())

    def cards(self, key, issue_id):
        return self.card_map.get(issue_id, [])

    def agents(self, key):
        return {HELPER: 'Helper · Chief of Staff', 'lead': 'Lee'}

    def prefix(self, key):
        return 'ACME'


class FakeTelegram:
    def __init__(self):
        self.sent, self.edits, self.answers = [], [], []
        self.next_id = 100

    def send(self, chat_id, text, buttons):
        self.next_id += 1
        self.sent.append((chat_id, text, buttons))
        return self.next_id

    def edit(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))

    def answer(self, callback_id, text):
        self.answers.append(text)


@pytest.fixture
def bot(tmp_path):
    cfg = ap.Config(
        token='t',
        api_url='https://p.example',
        public_url='https://p.example',
        company='co',
        reader_key='reader',
        approvers=[
            ap.Approver('Alex', ME, 'key-alex', {'board', 'card'}),
            ap.Approver('Sam P', SAM, 'key-sam', {'card'}),
        ],
        state_file=tmp_path / 'state.json',
        audit_file=tmp_path / 'audit.jsonl',
        tz=ZoneInfo('America/New_York'),
    )
    pc, tg = FakePaperclip(), FakeTelegram()
    pc.issues['i38'] = {'id': 'i38', 'identifier': 'ACME-38', 'status': 'in_review'}
    pc.card_map['i38'] = [
        {
            'id': 'card1',
            'kind': 'request_confirmation',
            'status': 'pending',
            'createdByAgentId': HELPER,
            'payload': {
                'prompt': "OK to send tonight's hello?",
                'detailsMarkdown': '**Once approved, Helper will:** send the draft as-is.',
            },
        }
    ]
    pc.approvals['ap1'] = {
        'id': 'ap1',
        'type': 'hire_agent',
        'status': 'pending',
        'requestedByAgentId': 'lead',
        'payload': {
            'name': 'Robin',
            'title': 'Landing Page Designer',
            'reportsTo': 'lead',
            'adapterConfig': {'model': 'claude-sonnet-5'},
            'budgetMonthlyCents': 4000,
        },
    }
    return ap.Bot(cfg, pc, tg, now=lambda: NOW), pc, tg


def press(bot, who, data, cid='cb1'):
    bot.handle(
        {
            'update_id': 1,
            'callback_query': {'id': cid, 'from': {'id': who}, 'data': data},
        }
    )


def audit(bot):
    return [json.loads(line) for line in bot.cfg.audit_file.read_text().splitlines()]


def test_posts_each_item_once_to_the_approvers_allowed_that_kind(bot):
    b, pc, tg = bot
    b.sync()
    hire = [s for s in tg.sent if 'Hire: Robin' in s[1]]
    card = [s for s in tg.sent if 'hello' in s[1]]
    assert [s[0] for s in hire] == [ME]  # board items: Alex only
    assert sorted(s[0] for s in card) == [SAM, ME]  # cards: both approvers
    assert (
        'Lee asked · reports to Lee · claude-sonnet-5' in hire[0][1]
        and 'Budget: $40/mo' in hire[0][1]
    )
    assert (
        'ACME-38, Helper · Chief of Staff' in card[0][1]
        and 'Once approved, Helper will: send' in card[0][1]
    )
    assert (
        'https://p.example/ACME/issues/ACME-38' in card[0][1]
        and 'open the card in Paperclip' in card[0][1]
    )
    assert card[0][2][0][0]['callback_data'] == 'y|c:card1'
    b.sync()
    assert len(tg.sent) == 3  # nothing posted twice


def test_approve_is_recorded_with_the_pressers_own_key(bot):
    b, pc, tg = bot
    b.sync()
    press(b, ME, 'y|c:card1')
    assert pc.decisions == [('key-alex', '/issues/i38/interactions/card1/accept')]
    assert (
        all('✅ Approved by Alex · 9:41 PM' in e[2] for e in tg.edits)
        and len(tg.edits) == 2
    )
    assert tg.answers == ['Approved.']
    assert (
        audit(b)[-1]['approver'] == 'Alex'
        and audit(b)[-1]['result'] == 'recorded'
    )


def test_not_now_rejects_a_board_approval(bot):
    b, pc, tg = bot
    b.sync()
    press(b, ME, 'n|a:ap1')
    assert pc.decisions == [('key-alex', '/approvals/ap1/reject')]
    assert 'Not now · Alex' in tg.edits[-1][2]


def test_a_decided_approval_is_cleared_from_the_approvers_inbox(bot):
    # decided approvals piled up unread in Paperclip's inbox.
    b, pc, tg = bot
    b.sync()
    press(b, ME, 'y|a:ap1')
    press(b, ME, 'y|c:card1', cid='cb2')
    assert pc.dismissed == [('key-alex', 'ap1')]  # approvals only, with the presser's key


def test_a_failed_inbox_clear_keeps_the_decision(bot):
    b, pc, tg = bot
    b.sync()
    pc.fail_dismiss = True
    press(b, ME, 'n|a:ap1')
    assert pc.decisions == [('key-alex', '/approvals/ap1/reject')]
    assert tg.answers == ['Marked not now.'] and audit(b)[-1]['result'] == 'recorded'


def test_not_now_on_a_card_sends_a_decline_reason(bot):
    # cards with rejectRequiresReason answer 422 to an empty reject body.
    b, pc, tg = bot
    b.sync()
    press(b, ME, 'n|c:card1')
    assert pc.decisions == [('key-alex', '/issues/i38/interactions/card1/reject')]
    assert pc.bodies == [{'reason': ap.NOT_NOW_REASON}]
    assert 'Not now · Alex' in tg.edits[-1][2]


def test_strangers_and_wrong_kinds_are_refused_and_logged(bot):
    b, pc, tg = bot
    b.sync()
    press(b, 999, 'y|c:card1')
    press(b, SAM, 'y|a:ap1')  # Sam may answer cards, not hires
    assert pc.decisions == []
    assert tg.answers == ['Only an approver can decide this.'] * 2
    assert [e['result'] for e in audit(b)] == ['refused: not an approver for this'] * 2


def test_a_press_after_paperclip_decided_changes_nothing(bot):
    b, pc, tg = bot
    b.sync()
    pc.card_map['i38'][0]['status'] = (
        'accepted'  # decided in the Paperclip UI meanwhile
    )
    press(b, ME, 'y|c:card1')
    assert pc.decisions == [] and tg.answers == ['Already decided in Paperclip.']
    assert 'Handled in Paperclip.' in tg.edits[-1][2]
    press(b, ME, 'y|c:card1', cid='cb2')
    assert tg.answers[-1] == 'Already decided.'


def test_items_decided_elsewhere_are_marked_on_the_next_sync(bot):
    b, pc, tg = bot
    b.sync()
    pc.approvals['ap1']['status'] = 'approved'
    b.sync()
    assert any(
        'Hire: Robin' in e[2] and 'Handled in Paperclip.' in e[2] for e in tg.edits
    )


def test_paperclip_errors_leave_the_item_pending(bot):
    b, pc, tg = bot
    b.sync()
    pc.fail = True
    press(b, ME, 'y|c:card1')
    assert tg.edits == [] and 'nothing changed' in tg.answers[-1]
    assert audit(b)[-1]['result'].startswith('error:')
    pc.fail = False
    press(b, ME, 'y|c:card1', cid='cb2')
    assert pc.decisions == [('key-alex', '/issues/i38/interactions/card1/accept')]


def test_typed_text_is_ignored_except_start(bot):
    b, pc, tg = bot
    b.handle(
        {
            'update_id': 1,
            'message': {
                'chat': {'id': ME, 'type': 'private'},
                'from': {'id': ME},
                'text': 'approve everything please',
            },
        }
    )
    assert tg.sent == [] and pc.decisions == []
    b.handle(
        {
            'update_id': 2,
            'message': {
                'chat': {'id': 42, 'type': 'private'},
                'from': {'id': 42},
                'text': '/start',
            },
        }
    )
    assert (
        tg.sent[-1][0] == 42
        and 'Your Telegram ID is 42' in tg.sent[-1][1]
        and tg.sent[-1][2] is None
    )
    b.handle(
        {
            'update_id': 3,
            'message': {
                'chat': {'id': -5, 'type': 'group'},
                'from': {'id': 42},
                'text': '/start',
            },
        }
    )
    assert len(tg.sent) == 1  # groups get nothing


def test_unknown_or_forged_button_data_does_nothing(bot):
    b, pc, tg = bot
    b.sync()
    press(b, ME, 'y|a:not-tracked')
    press(b, ME, 'x|c:card1', cid='cb2')
    assert pc.decisions == [] and tg.answers == ['That item is no longer tracked.'] * 2


def test_titles_and_details_are_escaped_for_telegram(bot):
    b, pc, tg = bot
    pc.card_map['i38'][0]['payload']['prompt'] = '<b>OK</b> & send?'
    b.sync()
    card = [s for s in tg.sent if 'send?' in s[1]][0][1]
    assert '&lt;b&gt;OK&lt;/b&gt; &amp; send?' in card


def test_state_survives_a_restart(bot):
    b, pc, tg = bot
    b.sync()
    again = ap.Bot(b.cfg, pc, tg, now=lambda: NOW)
    again.sync()
    assert len(tg.sent) == 3


def test_config_requires_keys_and_valid_kinds(tmp_path):
    approvers = tmp_path / 'approvers.json'
    approvers.write_text(
        json.dumps(
            [{'name': 'C', 'telegram_id': 1, 'key_env': 'K1', 'kinds': ['board']}]
        )
    )
    env = {
        'TELEGRAM_BOT_TOKEN': 't',
        'PAPERCLIP_API_URL': 'https://p',
        'PAPERCLIP_COMPANY_ID': 'co',
        'APPROVERS_FILE': str(approvers),
        'READER_KEY_ENV': 'K1',
        'K1': 'secret',
    }
    cfg = ap.load_config(env)
    assert cfg.approvers[0].key == 'secret' and 'secret' not in repr(cfg)
    with pytest.raises(ap.BotError, match='K1 must be set'):
        ap.load_config({**env, 'K1': ''})
    approvers.write_text(
        json.dumps(
            [{'name': 'C', 'telegram_id': 1, 'key_env': 'K1', 'kinds': ['everything']}]
        )
    )
    with pytest.raises(ap.BotError, match='kinds must be'):
        ap.load_config(env)


def test_bot_token_never_reaches_logs():
    assert (
        ap.redact('https://api.telegram.org/bot123:ABC/sendMessage')
        == 'https://api.telegram.org/bot<token>/sendMessage'
    )


# --- Slack ------------------------------------------------------------------------------
SLACK_ME, SLACK_OTHER = 'U0APPROVER1', 'U0STRANGER'


class FakeSlackApi:
    def __init__(self):
        self.calls = []

    def call(self, method, body):
        self.calls.append((method, body))
        if method == 'chat.postMessage':
            return {'ok': True, 'channel': body['channel'], 'ts': f'17.{len(self.calls)}'}
        return {'ok': True}

    def of(self, method):
        return [b for m, b in self.calls if m == method]


@pytest.fixture
def slack_bot(tmp_path):
    cfg = ap.Config(
        token='xoxb', api_url='https://p.example', public_url='https://p.example', company='co',
        reader_key='reader',
        approvers=[ap.Approver('Alex', SLACK_ME, 'key-alex', {'board', 'card'})],
        state_file=tmp_path / 'state.json', audit_file=tmp_path / 'audit.jsonl',
        tz=ZoneInfo('America/New_York'), chat='slack', slack_channel='C0APPROVALS',
    )
    pc, api = FakePaperclip(), FakeSlackApi()
    pc.issues['i38'] = {'id': 'i38', 'identifier': 'ACME-38', 'status': 'in_review'}
    pc.card_map['i38'] = [{'id': 'card1', 'kind': 'request_confirmation', 'status': 'pending',
                           'createdByAgentId': HELPER, 'payload': {'prompt': 'OK to send <the hello>?'}}]
    chat = ap.SlackChat(api, 'C0APPROVALS', socket=object())  # no live socket in tests
    return ap.Bot(cfg, pc, now=lambda: NOW, chat=chat), pc, api


def slack_press(bot, user, value):
    bot.handle({'type': 'block_actions', 'user': {'id': user}, 'channel': {'id': 'C0APPROVALS'},
                'actions': [{'action_id': 'approve', 'value': value}]})


def test_slack_posts_one_message_that_mentions_the_approver(slack_bot):
    b, pc, api = slack_bot
    b.sync()
    [post] = api.of('chat.postMessage')
    assert post['channel'] == 'C0APPROVALS' and post['unfurl_links'] is False
    text = post['blocks'][0]['text']['text']
    assert text.startswith('<@U0APPROVER1> 🟡 *OK to send &lt;the hello&gt;?*')
    assert '<https://p.example/ACME/issues/ACME-38|open the card in Paperclip>' in text
    assert [e['value'] for e in post['blocks'][1]['elements']] == ['y|c:card1', 'n|c:card1']
    b.sync()
    assert len(api.of('chat.postMessage')) == 1


def test_slack_press_records_as_the_presser_and_removes_the_buttons(slack_bot):
    b, pc, api = slack_bot
    b.sync()
    slack_press(b, SLACK_ME, 'y|c:card1')
    assert pc.decisions == [('key-alex', '/issues/i38/interactions/card1/accept')]
    [upd] = api.of('chat.update')
    assert upd['ts'] == '17.1' and all(blk['type'] != 'actions' for blk in upd['blocks'])
    assert '✅ Approved by Alex · 9:41 PM' in upd['blocks'][1]['elements'][0]['text']
    assert '<@' not in upd['blocks'][0]['text']['text']          # no second buzz on the edit
    assert api.of('chat.postEphemeral')[-1] == {'channel': 'C0APPROVALS', 'user': SLACK_ME, 'text': 'Approved.'}


def test_slack_stranger_press_is_refused_privately(slack_bot):
    b, pc, api = slack_bot
    b.sync()
    slack_press(b, SLACK_OTHER, 'y|c:card1')
    assert pc.decisions == [] and api.of('chat.update') == []
    assert api.of('chat.postEphemeral')[-1]['user'] == SLACK_OTHER
    assert 'Only an approver' in api.of('chat.postEphemeral')[-1]['text']


def test_slack_ignores_anything_but_button_presses(slack_bot):
    b, pc, api = slack_bot
    b.sync()
    b.handle({'type': 'message_action', 'user': {'id': SLACK_ME}, 'message': {'text': 'approve all'}})
    b.handle({'type': 'block_actions', 'user': {'id': SLACK_ME}, 'actions': []})
    assert pc.decisions == [] and api.of('chat.postEphemeral') == []


def test_slack_post_failure_retries_on_the_next_sync(slack_bot):
    b, pc, api = slack_bot
    real = api.call

    def flaky(method, body):
        if method == 'chat.postMessage' and not getattr(flaky, 'done', False):
            flaky.done = True
            raise ap.ApiError('Slack chat.postMessage failed: not_in_channel')
        return real(method, body)

    api.call = flaky
    b.sync()
    assert b.state['items'] == {}
    b.sync()
    assert 'c:card1' in b.state['items']


def test_slack_config_needs_its_tokens_channel_and_user_ids(tmp_path):
    approvers = tmp_path / 'approvers.json'
    approvers.write_text(json.dumps([{'name': 'C', 'slack_user_id': SLACK_ME, 'key_env': 'K1', 'kinds': ['card']}]))
    env = {'APPROVALS_CHAT': 'slack', 'SLACK_BOT_TOKEN': 'xoxb', 'SLACK_APP_TOKEN': 'xapp',
           'SLACK_APPROVALS_CHANNEL': 'C0APPROVALS', 'PAPERCLIP_API_URL': 'https://p',
           'PAPERCLIP_COMPANY_ID': 'co', 'APPROVERS_FILE': str(approvers), 'READER_KEY_ENV': 'K1', 'K1': 's'}
    cfg = ap.load_config(env)
    assert cfg.chat == 'slack' and cfg.approvers[0].chat_id == SLACK_ME and 'xapp' not in repr(cfg)
    with pytest.raises(ap.BotError, match='SLACK_APP_TOKEN must be set'):
        ap.load_config({**env, 'SLACK_APP_TOKEN': ''})
    approvers.write_text(json.dumps([{'name': 'C', 'telegram_id': 1, 'key_env': 'K1', 'kinds': ['card']}]))
    with pytest.raises(ap.BotError, match='slack_user_id is needed'):
        ap.load_config(env)
    with pytest.raises(ap.BotError, match='APPROVALS_CHAT must be'):
        ap.load_config({**env, 'APPROVALS_CHAT': 'imessage'})



def test_a_board_request_says_the_board_asked(bot):
    b, pc, tg = bot
    pc.approvals['ap2'] = {'id': 'ap2', 'type': 'hire_agent', 'status': 'pending', 'requestedByUserId': 'u1',
                           'payload': {'name': 'Lab · Strategist'}}
    b.sync()
    assert any('The board asked' in s[1] for s in tg.sent if 'Lab · Strategist' in s[1])


def test_a_label_names_the_company_on_every_item(slack_bot):
    # Several boards share one #approvals channel; the label says whose item it is.
    b, pc, api = slack_bot
    b.cfg.label = 'Acme'
    b.sync()
    [post] = api.of('chat.postMessage')
    assert post['text'] == 'Approval needed: Acme · OK to send &lt;the hello&gt;?'
    assert '*Acme · OK to send &lt;the hello&gt;?*' in post['blocks'][0]['text']['text']


def test_items_no_approver_may_see_are_never_posted(slack_bot):
    # An approver allowed only board items never sees task content in Slack.
    b, pc, api = slack_bot
    b.cfg.approvers[0].kinds = {'board'}
    b.sync()
    assert api.of('chat.postMessage') == []



# --- question cards ---------------------------------------------------------------------
QUESTION_CARD = {
    'id': 'qcard1', 'kind': 'ask_user_questions', 'status': 'pending', 'createdByAgentId': 'lead',
    'title': 'Two things before we start',
    'payload': {'questions': [
        {'id': 'audience', 'prompt': 'Who is this deck for?', 'selectionMode': 'single',
         'required': True, 'allowOther': True,
         'options': [{'id': 'acme', 'label': 'Acme Corp'}, {'id': 'internal', 'label': 'Internal / unbranded'}]},
        {'id': 'territory', 'prompt': 'Keep the territory example real?', 'selectionMode': 'single',
         'required': True, 'options': [{'id': 'reuse', 'label': 'Reuse as-is'}, {'id': 'generic', 'label': 'Genericize it'}]},
        {'id': 'extras', 'prompt': 'Anything to add?', 'selectionMode': 'multi', 'required': False,
         'options': [{'id': 'faq', 'label': 'FAQ slide'}, {'id': 'sheet', 'label': 'Cheat sheet'}]},
    ]},
}


def add_question(pc):
    pc.issues['i80'] = {'id': 'i80', 'identifier': 'ACME-80', 'status': 'blocked'}
    pc.card_map['i80'] = [json.loads(json.dumps(QUESTION_CARD))]


def form_values(**picks):
    """Slack view state: q0='0' (radio), q2=['0','1'] (checkboxes), o0='text'."""
    values = {}
    for block, pick in picks.items():
        if block.startswith('o'):
            values[block] = {'other': {'type': 'plain_text_input', 'value': pick}}
        elif isinstance(pick, list):
            values[block] = {'choice': {'type': 'checkboxes', 'selected_options': [{'value': v} for v in pick]}}
        else:
            values[block] = {'choice': {'type': 'radio_buttons', 'selected_option': {'value': pick}}}
    return values


def submit(b, user, values, key='q:qcard1'):
    return b.chat.on_interactive({
        'type': 'view_submission', 'user': {'id': user},
        'view': {'callback_id': ap.ANSWER_FORM, 'private_metadata': json.dumps({'key': key, 'channel': 'C0APPROVALS'}),
                 'blocks': [{'type': 'input', 'block_id': 'q0'}], 'state': {'values': values}}})


def drain(b):
    while not b.chat.inbox.empty():
        b.handle(b.chat.inbox.get())


def test_question_cards_are_posted_in_full_with_an_answer_button(slack_bot):
    b, pc, api = slack_bot
    add_question(pc)
    b.sync()
    post = [p for p in api.of('chat.postMessage') if p['text'].startswith('Question for you')][0]
    text = post['blocks'][0]['text']['text']
    assert '*Two things before we start*' in text and 'ACME-80, Lee asks:' in text
    assert '1. Who is this deck for? (Acme Corp; Internal / unbranded; or your own answer)' in text
    assert '2. Keep the territory example real? (Reuse as-is; Genericize it)' in text
    assert '<https://p.example/ACME/issues/ACME-80|answer in Paperclip>' in text
    [button] = post['blocks'][1]['elements']
    assert button['action_id'] == 'answer' and button['value'] == 'a|q:qcard1'
    assert b.state['items']['q:qcard1']['kind'] == 'card'


def test_answer_press_opens_a_form_for_an_approver_only(slack_bot):
    b, pc, api = slack_bot
    add_question(pc)
    b.sync()
    press = {'type': 'block_actions', 'user': {'id': SLACK_ME}, 'trigger_id': 'trig', 'channel': {'id': 'C0APPROVALS'},
             'actions': [{'action_id': 'answer', 'value': 'a|q:qcard1'}]}
    assert b.chat.on_interactive(press) is None
    [opened] = api.of('views.open')
    view = opened['view']
    assert opened['trigger_id'] == 'trig' and view['callback_id'] == ap.ANSWER_FORM
    inputs = {blk['block_id']: blk for blk in view['blocks'] if blk['type'] == 'input'}
    assert inputs['q0']['element']['type'] == 'radio_buttons' and inputs['q0']['optional'] is True  # own answer allowed
    assert inputs['q1']['optional'] is False and 'o1' not in inputs
    assert inputs['q2']['element']['type'] == 'checkboxes'
    assert inputs['o0']['element']['type'] == 'plain_text_input'
    assert b.chat.inbox.empty()  # handled on arrival, nothing for the main loop
    b.chat.on_interactive({**press, 'user': {'id': SLACK_OTHER}})
    assert len(api.of('views.open')) == 1
    assert api.of('chat.postEphemeral')[-1] == {'channel': 'C0APPROVALS', 'user': SLACK_OTHER,
                                                'text': 'Only an approver can answer this.'}
    assert audit(b)[-1]['result'] == 'refused: not an approver for this'


def test_long_option_lists_use_a_menu_and_labels_fit_slack():
    item = ap.Item(key='q:x', kind='card', id='x', title='t', lines=[], url='u', questions=[
        {'id': 'pick', 'prompt': 'Which?', 'selectionMode': 'multi', 'required': True,
         'options': [{'id': f'o{i}', 'label': 'L' * 200} for i in range(12)]}])
    [section, choice] = ap.answer_form(item, 'C1')['blocks']
    assert choice['element']['type'] == 'multi_static_select' and len(choice['element']['options']) == 12
    assert len(choice['element']['options'][0]['text']['text']) == ap.SLACK_OPTION_TEXT
    assert choice['optional'] is False


def test_submitted_answers_go_to_paperclip_as_the_approver(slack_bot):
    b, pc, api = slack_bot
    add_question(pc)
    b.sync()
    assert submit(b, SLACK_ME, form_values(q0='0', q1='1', q2=['0', '1'], o0='  and the sales team  ')) is None
    drain(b)
    assert pc.decisions == [('key-alex', '/issues/i80/interactions/qcard1/respond')]
    assert pc.bodies[-1] == {'answers': [
        {'questionId': 'audience', 'optionIds': ['acme'], 'otherText': 'and the sales team'},
        {'questionId': 'territory', 'optionIds': ['generic']},
        {'questionId': 'extras', 'optionIds': ['faq', 'sheet']}]}
    upd = [u for u in api.of('chat.update') if 'Answered' in u['text']][0]
    outcome = upd['blocks'][1]['elements'][0]['text']
    assert outcome.startswith('✅ Answered by Alex · 9:41 PM: Acme Corp, "and the sales team"; Genericize it')
    assert all(blk['type'] != 'actions' for blk in upd['blocks'])
    assert api.of('chat.postEphemeral')[-1]['text'] == 'Answers sent.'
    assert audit(b)[-1] == {**audit(b)[-1], 'approver': 'Alex', 'action': 'answer', 'result': 'recorded'}


def test_a_form_missing_a_required_answer_stays_open_with_errors(slack_bot):
    b, pc, api = slack_bot
    add_question(pc)
    b.sync()
    reply = submit(b, SLACK_ME, form_values(q0='0'))
    assert reply == {'response_action': 'errors', 'errors': {'q1': 'This one needs an answer.'}}
    assert b.chat.inbox.empty() and pc.decisions == []
    # Free text alone answers a question that allows it.
    assert submit(b, SLACK_ME, form_values(o0='Someone else', q1='0')) is None


def test_a_form_from_a_stranger_or_for_an_answered_card_is_refused(slack_bot):
    b, pc, api = slack_bot
    add_question(pc)
    b.sync()
    reply = submit(b, SLACK_OTHER, form_values(q0='0', q1='0'))
    assert reply['response_action'] == 'errors' and 'Only an approver' in reply['errors']['q0']
    pc.card_map['i80'][0]['status'] = 'answered'  # answered in the Paperclip UI meanwhile
    assert submit(b, SLACK_ME, form_values(q0='0', q1='0')) is None
    drain(b)
    assert pc.decisions == [] and api.of('chat.postEphemeral')[-1]['text'] == 'Already answered in Paperclip.'
    assert 'Handled in Paperclip.' in api.of('chat.update')[-1]['text']


def test_paperclip_refusing_an_answer_leaves_the_card_open(slack_bot):
    b, pc, api = slack_bot
    add_question(pc)
    b.sync()
    submit(b, SLACK_ME, form_values(q0='0', q1='0'))
    pc.fail = True
    drain(b)
    assert not b.state['items']['q:qcard1']['done'] and 'nothing changed' in api.of('chat.postEphemeral')[-1]['text']
    assert audit(b)[-1]['result'].startswith('error:')


def test_question_answers_cannot_be_forged_as_approve_presses(slack_bot):
    b, pc, api = slack_bot
    add_question(pc)
    b.sync()
    slack_press(b, SLACK_ME, 'y|q:qcard1')
    assert pc.decisions == [] and api.of('chat.postEphemeral')[-1]['text'] == 'That item is no longer tracked.'


def test_telegram_shows_question_cards_with_a_paperclip_link_and_no_buttons(bot):
    b, pc, tg = bot
    add_question(pc)
    b.sync()
    sent = [s for s in tg.sent if 'Two things' in s[1]]
    assert sorted(s[0] for s in sent) == [SAM, ME]  # cards go to every card approver
    assert all(s[2] == [] for s in sent) and 'answer in Paperclip' in sent[0][1]
    assert '1. Who is this deck for?' in sent[0][1]


# --- hardening after a security review --------------------------------------------------------------
def test_slack_notification_text_escapes_agent_markup(slack_bot):
    # The notification text is parsed as mrkdwn too: an agent-written title can't ping the channel
    # or fake a link there.
    b, pc, api = slack_bot
    pc.card_map['i38'][0]['payload']['prompt'] = '<!channel> <https://evil.example|Re-login> & go'
    b.sync()
    [post] = api.of('chat.postMessage')
    assert '<!channel>' not in post['text'] and '<https://evil' not in post['text']
    assert '&lt;!channel&gt; &lt;https://evil.example|Re-login&gt; &amp; go' in post['text']
    slack_press(b, SLACK_ME, 'y|c:card1')
    [upd] = api.of('chat.update')
    assert '<!channel>' not in upd['text'] and '&lt;!channel&gt;' in upd['text']


def test_plain_stays_fast_and_bounded_on_huge_agent_text():
    import time
    start = time.monotonic()
    for evil in (' ' * 300_000, '[' * 300_000, '[a](' * 100_000, ('x ' * 50_000 + '\n') * 3):
        out = ap.plain(evil, 700)
        assert len(out) <= 700
    assert time.monotonic() - start < 2.0


def test_plain_keeps_its_formatting_rules():
    assert ap.plain('**Bold** and `code`\n\n  next [link](https://x.example/a b) line', 200) == (
        'Bold and code next [link](https://x.example/a b) line')
    assert ap.plain('see [the doc](https://x.example/doc)\nthen  done', 200) == 'see the doc then  done'
    assert ap.plain('x' * 50, 10) == 'x' * 9 + '…'
    assert ap.plain(None, 10) == ''


# --- review hand-offs -----------------------------------------------------------------------------
PC_ME = 'user-alex'
REVIEW_TASK = {'id': 'i86', 'identifier': 'ACME-86', 'status': 'in_review', 'assigneeUserId': PC_ME,
               'createdByAgentId': 'lead', 'title': 'Create the launch deck (PPTX)'}


def with_review(b, pc):
    b.cfg.approvers[0].kinds = {'board', 'card', 'review'}
    pc.issues['i86'] = dict(REVIEW_TASK)


def test_a_task_in_review_for_a_person_is_posted_with_a_link_and_no_buttons(slack_bot):
    b, pc, api = slack_bot
    with_review(b, pc)
    b.sync()
    [post] = [p for p in api.of('chat.postMessage') if 'launch deck' in p['text']]
    assert post['text'] == 'Ready for your review: Create the launch deck (PPTX)'
    text = post['blocks'][0]['text']['text']
    assert text.startswith('<@U0APPROVER1> 🟡 *Create the launch deck')
    assert 'ACME-86, Lee put it in review for you' in text
    assert '<https://p.example/ACME/issues/ACME-86|open the task in Paperclip>' in text
    assert all(blk['type'] != 'actions' for blk in post['blocks'])
    b.sync()
    assert len([p for p in api.of('chat.postMessage') if 'launch deck' in p['text']]) == 1


def test_review_items_need_the_review_kind(slack_bot):
    b, pc, api = slack_bot
    pc.issues['i86'] = dict(REVIEW_TASK)  # Alex has board + card only
    b.sync()
    assert not any('launch deck' in p['text'] for p in api.of('chat.postMessage'))


def test_tasks_in_review_for_an_agent_are_not_posted(slack_bot):
    # The fixture's ACME-38 is in review with no person assigned; only its card is posted.
    b, pc, api = slack_bot
    b.cfg.approvers[0].kinds = {'board', 'card', 'review'}
    b.sync()
    assert [p['text'] for p in api.of('chat.postMessage')] == ['Approval needed: OK to send &lt;the hello&gt;?']


def test_a_review_closes_when_the_task_leaves_review_and_reposts_if_it_returns(slack_bot):
    b, pc, api = slack_bot
    with_review(b, pc)
    b.sync()
    pc.issues['i86']['status'] = 'done'
    b.sync()
    [upd] = api.of('chat.update')
    assert 'Out of review in Paperclip.' in upd['blocks'][1]['elements'][0]['text']
    pc.issues['i86']['status'] = 'in_review'
    b.sync()
    assert len([p for p in api.of('chat.postMessage') if 'launch deck' in p['text']]) == 2
    assert b.state['items']['r:i86']['done'] is False


def test_a_paperclip_user_id_limits_reviews_to_that_persons_own(slack_bot):
    b, pc, api = slack_bot
    with_review(b, pc)
    b.cfg.approvers[0].paperclip_user_id = 'someone-else'
    b.sync()
    assert not any('launch deck' in p['text'] for p in api.of('chat.postMessage'))
    b.state['items'].clear()
    b.cfg.approvers[0].paperclip_user_id = PC_ME
    b.sync()
    assert any('launch deck' in p['text'] for p in api.of('chat.postMessage'))


def test_review_items_cannot_be_approved_with_a_forged_press(slack_bot):
    b, pc, api = slack_bot
    with_review(b, pc)
    b.sync()
    slack_press(b, SLACK_ME, 'y|r:i86')
    assert pc.decisions == [] and b.state['items']['r:i86']['done'] is False


def test_telegram_shows_review_items_without_buttons(bot):
    b, pc, tg = bot
    with_review(b, pc)
    b.sync()
    [sent] = [s for s in tg.sent if 'launch deck' in s[1]]
    assert sent[0] == ME and not sent[2]
    assert 'open the task in Paperclip' in sent[1]


def test_config_reads_review_kind_and_paperclip_user_id(tmp_path):
    approvers = tmp_path / 'approvers.json'
    approvers.write_text(json.dumps([{'name': 'C', 'telegram_id': 1, 'key_env': 'K1',
                                      'kinds': ['card', 'review'], 'paperclip_user_id': PC_ME}]))
    cfg = ap.load_config({'TELEGRAM_BOT_TOKEN': 't', 'PAPERCLIP_API_URL': 'https://p', 'PAPERCLIP_COMPANY_ID': 'co',
                          'APPROVERS_FILE': str(approvers), 'READER_KEY_ENV': 'K1', 'K1': 's'})
    assert cfg.approvers[0].kinds == {'card', 'review'} and cfg.approvers[0].paperclip_user_id == PC_ME
