# Security

## What the bot holds

- **A reader key** (a Paperclip board key) used only to list pending approvals and cards.
- **One Paperclip key per approver.** Each decision is made with the presser's own key, so it is
  recorded as them and limited to what Paperclip already lets them do.
- **Chat tokens:** the Slack bot and app-level tokens, or the Telegram bot token.

Anyone who can read the bot's environment files can act as every approver in Paperclip. Treat the
machine running it like the approvers' own sessions:

- keep `secrets.env` at mode 600, owned by the user that runs the bot, outside any synced or
  backed-up-to-cloud folder you don't control;
- run the bot as its own unprivileged user;
- use keys with no more reach than the approver needs, and revoke an approver's key in Paperclip
  when they leave (then remove them from `approvers.json`).

## What it trusts

- **Chat user IDs**, as reported by Slack or Telegram, to identify approvers. Anyone who controls
  an approver's Slack or Telegram account can approve as them. Use your chat app's own protections
  (SSO, 2FA) accordingly.
- **Paperclip**, as the source of truth: every press is re-checked against Paperclip before acting.
- **Agent-made files**, only when `APPROVALS_REVIEW_FILES=on`: the bot downloads a review task's
  attachments with the reader key and uploads them, unopened, to your Slack channel. It never runs or
  parses them, and caps their number and size. Their names are cut to a plain base name. Anyone in
  the channel can download them, so leave the option off if task files shouldn't live in Slack.

It does not trust message text. There's no AI in the bot and no command parsing beyond Telegram's
`/start`.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting ("Report a vulnerability" under the Security
tab) rather than a public issue. There's no bug bounty and no guaranteed response time, but reports
are read.
