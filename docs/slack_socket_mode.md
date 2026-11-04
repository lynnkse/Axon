# Private Slack Socket Mode bridge

This adapter is deliberately an Axon-side Slack integration only. It uses no
OpenAI API, no OpenAI API key, no public listener, and no shell capability.
The Slack app opens an outbound Socket Mode connection and forwards messages
from one configured private channel to the existing localhost Axon gateway.

## Slack app configuration

Create or use a Slack app only in `lynnkse-core`, enable Socket Mode, generate
an app-level token with `connections:write`, and install the app to that
workspace. Give its bot the minimum scopes needed to read the private bridge
channel and write replies: `groups:history` and `chat:write`. Subscribe to
the `message.groups` bot event, then add the bot only to `#axon-bridge`.
The ready-to-import manifest is `docs/slack_app_manifest.yaml`.

Obtain the private channel ID (not its name) and the exact sender identity used
by ChatGPT Work. Depending on Slack's posting mode this will be your member ID,
the ChatGPT bot ID, or its app ID. The adapter requires at least one explicit
sender allowlist and always rejects its own bot identity. Never place any token
in a repository, shell history, dashboard, or Slack message.

## Local launch

Run the existing gateway with a source that is explicitly discarded by the
Telegram subscriber:

```bash
export AXON_GATEWAY_SOURCE=slack
export AXON_GATEWAY_CALLER_ID=slack-socket-mode
export AXON_GATEWAY_DB=/tmp/axon/slack_gateway.sqlite3
python3 axon_gateway.py --port 8767
```

In a separately permissioned local service environment, provide
`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_AXON_TEAM_ID`,
`SLACK_AXON_CHANNEL_ID`, and `AXON_GATEWAY_TOKEN`. The team ID binds the bot
token to the intended workspace at startup. Also configure at least one of
`SLACK_AXON_ALLOWED_USER_IDS`, `SLACK_AXON_ALLOWED_BOT_IDS`, or
`SLACK_AXON_ALLOWED_APP_IDS`, then run:

```bash
python3 axon_slack_adapter.py
```

The adapter immediately acknowledges the durable Axon task ID, then posts the
final result in the originating Slack thread. It ignores other channels, empty
text, edits/deletions, its own replies, and every human, bot, or app identity
that is not explicitly authorized.
