# Balance Forecast BETA v0.8

> **Unofficial tool — not affiliated with or endorsed by Monarch Money, Inc. Use at your own risk.**

A self-hosted personal finance dashboard that pulls your **Monarch Money** account data and builds a rolling balance forecast. Everything runs on your own computer. No data ever leaves your device except the direct connection to Monarch's own servers and, optionally, your chosen AI provider.

---

## What this tool does

Balance Forecast reads your transaction history and recurring payment schedule from Monarch Money and projects your checking account balance forward in time — by default 45 days, configurable to any horizon you like. It highlights upcoming low-balance windows, lets you model one-off transfers or expenses, and can optionally generate AI-powered narrative insights about spending patterns and upcoming bills.

Key capabilities:

- **Forecast chart** — rolling balance projection based on your Monarch recurring schedule
- **Variable Payments** — override the learned amount for credit cards or irregular bills
- **Scenario Modeling** — add hypothetical transfers or expenses to see their impact
- **AI Insights** (optional) — seasonal patterns, predicted expenses, transfer recommendations
- **Calendar Integration** (optional) — overlay external Calendar events on the forecast
- **Dark mode**

---

## Disclaimer & legal notice

**This is an unofficial, community-built tool. It is not affiliated with, endorsed by, sponsored by, or in any way connected to Monarch Money, Inc.**

This tool automates a real Chromium browser session using your own Monarch credentials via [Playwright](https://playwright.dev/). It intercepts the same GraphQL network calls that the Monarch web application makes — it makes no additional or undocumented API requests beyond what your browser would make normally.

**This may violate Monarch's Terms of Service.** Use it for personal use only, at your own discretion, and at your own risk. The author makes no guarantees about continued compatibility. If Monarch updates their web app, this tool may stop working without notice.

**Support for this tool comes from the individual who created it, not from Monarch Money.** Do not contact Monarch Money's support team with questions about this tool.

---

## Architecture & privacy

**All your data stays on your computer. Nothing is uploaded to any cloud service operated by this tool.**

Here is exactly where data flows and where it is stored:

### Authentication & Monarch connection

1. You enter your Monarch email and password into a chrome window just to capture the session data — they are never transmitted to any server other than Monarch's own login endpoint.
2. When you click **Connect to Monarch**, a Chromium browser window opens on your machine. You log in directly to `app.monarchmoney.com` — your credentials go from your keyboard to Monarch's servers, nowhere else.
3. After a successful login, Playwright saves your session cookies to `browser_state.json` in the app folder. Subsequent data fetches use this saved session (headless, no visible window) to avoid repeated logins.
4. All future data fetches go directly from your computer to `api.monarch.com` — the same GraphQL endpoint your browser uses when you visit Monarch normally.

### Data storage (all files are local, in the app folder)

| File | What it contains | Leaves your device? |
|---|---|---|
| `.env` | AI API key |
| `browser_state.json` | Monarch session cookies | **Never** |
| `config.yaml` | App preferences (account ID, forecast horizon, etc.) | **Never** |
| `insights.json` | AI analysis output | Only if you configure an AI provider |
| `payment_overrides.json` | Your variable payment amounts | **Never** |
| `scenarios.json` | Scenario modeling events | **Never** |
| `monarch_accounts_cache.json` | Account names and IDs from Monarch | **Never** |
| `user_context.md` | Corrections you feed to the AI | Only if you configure an AI provider |

The app runs a local web server at `http://localhost:5002` by default. This server is only accessible from your own computer — it does not listen on a network interface accessible to other devices.

### AI provider data handling (optional feature)

If you enable AI Insights, the app sends a summary of your recent transactions and recurring payments to the AI provider you choose (Anthropic, OpenAI, or Google). **Before enabling this feature, review the privacy policy of your chosen provider:**

- [Anthropic Privacy Policy](https://www.anthropic.com/privacy)
- [OpenAI Privacy Policy](https://openai.com/policies/privacy-policy)
- [Google Privacy Policy](https://policies.google.com/privacy)

Your AI API key is stored only in `.env` on your device. It is sent only to your chosen provider's API endpoint to authenticate requests — it is never transmitted to any server operated by this tool or its author.

---

## What you'll need before starting

- A **Mac** (macOS) or **Linux** computer — Windows not supported in this version
- A **[Monarch Money](https://www.monarchmoney.com/)** account
- **Python 3.11 or later** installed on your computer
  - Mac: download from [python.org](https://www.python.org/downloads/) or install via [Homebrew](https://brew.sh): `brew install python@3.12`
  - Linux: `sudo apt install python3` (Debian/Ubuntu) or `sudo dnf install python3` (Fedora)

**Optional — for AI insights:**
An API key from one of these providers (pick one):
- [Anthropic (Claude)](https://console.anthropic.com/) — recommended, tested & verified
- [OpenAI (GPT)](https://platform.openai.com/) — not tested
- [Google (Gemini)](https://aistudio.google.com/) — not tested

---

## Getting started

### Step 1 — Download the app

Click the **Code** button on this page and choose **Download ZIP**. Unzip it somewhere you'll remember, like your Documents folder. (Or if you're already fluent in git just fork the repo and have at it.)

### Step 2 — Launch it

**On Mac:**
Double-click **`Start Balance Forecast.command`** in the folder. On first run it will take a couple of minutes to set up and open the Settings page in a browser - be patient and watch your terminal window for any dependency errors!

> The first time you open it, macOS may warn you it's from an unidentified developer. Right-click the file → **Open** → **Open** to proceed. You only need to do this once.

**On Linux:**
Right-click **`run.sh`** in your file manager → **Run as Program** (the exact wording depends on your desktop). Or open a Terminal, navigate to the folder, and enter `./run.sh`.

The launcher will automatically:
- Set up a Python environment
- Install all required packages
- Open your browser to the Settings page

### Step 3 — Connect to Monarch

On first launch your browser will open directly to the **Settings** page. The **Monarch Connection** section (highlighted with a blue border) is the only required step:

1. Click **Connect to Monarch** — a Chrome browser window will open automatically. Log in to Monarch when prompted; the window closes on its own after a successful login (~30–60 seconds).
2. Wait for your eligible Monarch accounts to populate, then select your **primary bill pay account** from the dropdown that appears. Click **Save Monarch Settings** to complete the setup.

Once these two steps are complete, the **Go to Dashboard →** button at the top of the page will turn green. Click it to pull transactions and open your forecast - again, this may take 30 seconds or so. Patience, grasshopper.

> Down further on the Settings page are **AI Insights** and **Forecast Settings**. These are both optional on first run and can always be customized later. The forecast defaults work just fine to start.

### Step 4 — View & customize your first forecast

The first time you open the dashboard after setup the app fetches your transaction history from Monarch. Your forecast chart will appear at the top of the dashboard when it's done. After the first run, your data stays cached so the dashboard loads instantly on future visits.

---

## Day-to-day use

**Starting the app:** Double-click **`Start Balance Forecast.command`** (Mac) or run `./run.sh` (Linux). Your browser will open automatically. I recommend bookmarking `http://localhost:5002` as well.

**Refresh Forecast** — click the button in the upper right whenever you want updated transaction data from Monarch. Takes 1–2 minutes.

**Run AI Analysis** — generates fresh AI insights. Run this once a day or whenever you want an updated analysis. Requires an AI API key in Settings → AI Insights.

**Settings** — everything is configurable from the Settings page. Key options:

| Setting | Description |
|---|---|
| Primary Account | Select the account used for bill payments - refresh the list if you don't see it |
| AI Insights | Enable AI Insights (off by default); set your preferred provider (Anthropic, OpenAI, or Google), choose your preferred model and add your API key; set the default number of transaction months to retrieve for analysis;  customize how long before the analysis is labeled as stale. You can also kick off an AI Analysis right from Settings and see it run in a status window |
| Forecast Horizon | How many days to project forward (default: 45) |
| Buffer Threshold | Get a warning when your balance drops below this dollar amount |
| User Context & AI Corrections | Hand-edit your AI corrections in free text (also stored in user_context.md) |
| Calendar Integration | Overlay custom calendar transaction events onto the forecast chart with Google Calendar, iCloud, or a custom ICS feed. |
| App Settings | Change the default port, turn on debug mode, restart the server, or reset to factory defaults. |

---

## Features

- **Forecast balance chart** with recurring payments projected forward, configurable to any number of days
- **AI insights** (optional) — seasonal spending patterns, predicted upcoming expenses, transfer recommendations to maintain a balance above your configured buffer amount
- **Variable Payments** — override Monarch's learned amount for variable monthly payments like credit cards; enter $0 to suppress a payment from the forecast entirely for a month
- **Scenario Modeling** — temporarily model one-time transfers or expenses to see how they affect your balance forecast
- **Corrections & Context** — feed the AI specific facts about your finances to improve its accuracy
- **Dark mode** — toggle in Settings or with the moon icon in the header

---

## Troubleshooting

**"Setup needed" error on the dashboard** — click **→ Open Settings** in the error box and complete the Monarch Connection section (email, password, and primary account selection).

**Forecast is slow** — this is normal on first run. The app opens a browser, logs into Monarch, and fetches months of transaction history. Subsequent loads use a cached session and are much faster.

**Monarch login fails** — a Chrome window opens for you to log in. Complete any two-factor authentication steps there; the window closes automatically once you're logged in.

**Account list looks incomplete after connecting** — click **Refresh Accounts** in Settings → Monarch Connection.

**"API key is not configured"** — go to Settings → AI Insights, select your AI provider, and paste in your key.

**Browser doesn't open automatically (Linux)** — navigate to `http://localhost:5002` in your browser manually.

**The app was working and suddenly stopped** — Monarch occasionally updates their web app, which can break the data-fetching layer. Check the [project page on GitHub](https://github.com/vendaface/balance-forecast) for updates.

---

## Power user reference

### Running from Terminal

```bash
# Start (foreground — closes when you close the Terminal window)
./run.sh

# Start as a background daemon
./server.sh start

# Other daemon controls
./server.sh stop       # stop the server
./server.sh restart    # restart after config changes
./server.sh status     # check if running
./server.sh logs       # tail the server log
```

### File overview

| File | Purpose |
|---|---|
| `config.yaml` | Main configuration (auto-created on first launch) |
| `.env` | Credentials — email, password, API keys (auto-created on first launch) |
| `browser_state.json` | Saved Monarch login session |
| `insights.json` | Latest AI analysis output |
| `payment_overrides.json` | Variable payment amounts you've set |
| `scenarios.json` | Scenario modeling events |
| `monarch_accounts_cache.json` | Cached account list from Monarch |
| `user_context.md` | Corrections and facts injected into AI prompts |

### Resetting to a clean state

```bash
./reset-for-testing.sh
```

Kills the running server, deletes all cached and generated files, and removes the virtual environment. You will be asked to confirm before anything is deleted.

---

## Known limitations

- No official Monarch API exists — this tool intercepts the same network calls the Monarch web app makes. Changes to Monarch's web app may break it without warning.
- Designed for desktop use; mobile layout is not optimized.
- Windows is not supported in this version.

---

## License

MIT — see `LICENSE` file.

This software is provided "as is", without warranty of any kind. The author is not responsible for any account issues, data loss, or Terms of Service consequences that may arise from its use.
