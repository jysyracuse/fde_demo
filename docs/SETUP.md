# Manual setup (step 1b)

Things only you can do in a browser. Each section ends with the `.env` values it produces.

## 1. Salesforce: External Client App (client credentials)

Connected Apps can no longer be created by default (Spring '26), so use an **External Client App**.

1. Setup → Quick Find **Users** → your user → confirm the profile has **API Enabled**
   (System Administrator has it in Developer Edition).
   - Optional, least privilege: create a dedicated **integration user** with a
     permission set that only grants *Read* on Account, Contact, Opportunity, Case, Task.
     For the demo the data loader needs Create/Delete too — use your admin user for loading and
     the read-only user as the Run As user for the app itself.
2. Setup → Quick Find **External Client App Manager** → **New External Client App**
   - Name `AR Followup Demo`, contact email, Distribution State **Local**.
   - **Enable OAuth**; callback URL `https://login.salesforce.com/services/oauth2/success`
     (required by the form, unused by this flow).
   - Scopes: **Manage user data via APIs (api)**. No refresh-token scope needed.
   - Flow Enablement: **Enable Client Credentials Flow** → accept the warning → Create.
3. Open the app → **Policies** → Edit → OAuth Policies → **Client Credentials Flow → Run As**:
   pick the user from step 1 → Save.
4. **Settings** tab → OAuth Settings → **Consumer Key and Secret** (needs email verification).
5. Your **My Domain** host: Setup → My Domain, e.g. `yourname-dev-ed.develop.my.salesforce.com`.
   `login.salesforce.com` does **not** work for client credentials.

```
SF_DOMAIN=yourname-dev-ed.develop.my.salesforce.com
SF_CLIENT_ID=...
SF_CLIENT_SECRET=...
```

Test: `uv run python scripts/smoke_salesforce.py` → prints API version, user and an Account count.

Common errors: `invalid_grant / no client credentials user enabled` → Run As not set;
`request not supported on this domain` → using login.salesforce.com instead of My Domain.

## 2. Slack app (Socket Mode)

1. Create a free workspace (or use a sandbox one) and a channel `#ar-followup`.
2. https://api.slack.com/apps → **Create New App → From a manifest** → paste `docs/slack-manifest.yml`.
3. **Basic Information → App-Level Tokens** → generate one with scope `connections:write` → `SLACK_APP_TOKEN` (`xapp-…`).
4. **Install to Workspace** → copy **Bot User OAuth Token** → `SLACK_BOT_TOKEN` (`xoxb-…`).
5. In Slack: `/invite @AR Followup` in `#ar-followup`; copy the channel ID (channel details, bottom).

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_CHANNEL_ID=C0...
```

Test: `uv run python scripts/smoke_slack.py` → a message appears in the channel.

## 3. Billing database

Nothing to set up: SQLite file `data/out/billing.db`, built by `uv run python scripts/load_db.py`.

## 4. LLM API

platform.deepseek.com → API key → `DEEPSEEK_API_KEY`. Use an API key rather than a subscription login for
automated calls (open question in the handoff resolved this way by default).
