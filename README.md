# ERP Stock Assistant

Ask an Oracle ERP about stock in plain English and get real figures back, with the SQL that
produced them shown under every answer.

Built for a live Oracle 19c ERP at a construction group (3.5M-row stock ledger, 128k items,
500 locations). A Claude agent writes the SQL, a hardened read-only layer runs it, and a
single-page web UI streams the query, the result grid and the answer as they arrive.

> The company-specific schema notes (`knowledge.py`), conversation logs and the internal
> hand-over are deliberately not in this repository. `knowledge_example.py` shows the format,
> so the same code runs against any Oracle schema once you describe it.

## What it does

- **Natural language to Oracle SQL** with a tool-using Claude agent (`run_sql`,
  `describe_table`, `search_schema`, `sample_rows`). The model must query before it states a number.
- **Every query is visible**: syntax-highlighted SQL, row count, elapsed time, a sortable result
  grid and a CSV download under each answer.
- **Multi-turn**: follow-up questions keep context ("and only for the main company?").
- **Streaming UI**: answers render while the model is still working; Esc stops a question and
  the half-finished exchange is discarded server-side so the history never breaks.
- **Refresh-safe**: conversations are rebuilt from the server on reload.
- **Several conversations per sign-in**: the sidebar holds only your conversations (start,
  switch, delete); each keeps its own context and cost.
- **Tunnel-proof streaming**: the server sends a heartbeat every few seconds while the model
  works, so proxies and tunnels never drop a long answer, and the page shows a live timer.
- **Cost controls**: the schema notes are a cached system prompt, per-question cost is shown,
  spend is logged, and a daily budget hard-stops the API.
- **Vanilla front end, zero external resources**: question library in the empty state, a profile
  page with status, spend, preferences, account and the admin entry, light and dark themes.

## Users, roles and the admin panel

Sign-in is required. Two kinds of account:

- **App accounts** in `data/users.json` (salted PBKDF2-SHA256 hashes), created with
  `python manage.py create <name> <admin|user>` or from the admin panel. An app account can
  be **linked to an ERP username** so its access follows that person's ERP roles.
- **ERP credentials** (optional, on by default): anyone with an active ERP account signs in
  with their ERP username and password. The typed password is hashed and compared with the
  ERP's stored hash in constant time; the hash never leaves the server.

Roles and responsibilities come from the ERP. At sign-in the person's active ERP groups are
read live and mapped to **modules** (Stock, Purchasing, HR, Finance...). Each module carries
the tables it may query and a plain-language scope. **Admin is a fixed list of ERP usernames**
in the private knowledge file, never a user-type flag.

| role | can |
|---|---|
| **admin** | ask anything the schema holds (except credential tables), see the SQL, browse the schema, manage users and the group-to-module mapping, choose the model, set budgets, and **teach the assistant** |
| **user** | ask only within their modules. Anything else gets a fixed refusal ("You don't have enough privileges to access this information. Please contact the admin."). SQL is hidden unless an admin allows it |

The restriction is not a prompt trick: every SQL statement from a `user` session passes the
server-side table allow-list of their modules, and the Oracle data dictionary is blocked for
them. Credential tables and password columns are unreadable for every role, admins included.

The admin panel (Profile > Admin panel) shows users, model choice with a live cost
table computed from your own usage, budgets, learned knowledge, feedback, security posture and
the audit log.

## Learning loop

The assistant improves with use, without anyone editing code:

- **Admin sessions are learning sessions.** When it lacks a table, a join or a business rule
  it asks the admin a precise question instead of guessing, then saves the confirmed rule with
  its `save_knowledge` tool. Saved rules go into every future session's instructions.
- **Thumbs up / down** under every answer. An admin's thumbs-up stores the question and the
  SQL as a verified example; an admin's thumbs-down with a correction stores the correction.
  Users' feedback is queued for the admin to review.
- Admins can also type facts directly into the knowledge card, and delete anything wrong.

## Security design

The database account is read-only, and the application does not trust that alone:

1. **Authentication**: username/password, PBKDF2-SHA256 with 600k iterations and a per-user
   salt; generic failure message; 5 failures lock the account for 15 minutes; failed attempts
   are audited.
2. **Sessions**: random 256-bit token in an HttpOnly, Secure, SameSite=Strict cookie; 12 h
   lifetime, 90 min idle timeout; all sessions of a user are revoked when their password or
   role changes.
3. **Authorisation**: admin/user roles; a per-role table allow-list is enforced on every SQL
   statement server-side; admin routes return 403 to users and the attempt is audited.
4. **SQL**: only one `SELECT` / `WITH` statement passes the validator (comments and literals
   stripped before scanning, so `select 'delete' from dual` passes while DML, DDL, PL/SQL,
   `FOR UPDATE` and multi-statement input are rejected before reaching Oracle); the session runs
   `set transaction read only`; results are capped and time-boxed.
5. **Web**: Content-Security-Policy, `X-Frame-Options: DENY`, nosniff, no-referrer,
   Permissions-Policy; POST requests must carry a custom header (CSRF); bodies capped at 64 KB;
   errors never leak stack traces; model output is sanitised with DOMPurify before rendering.
6. **Spend**: global and per-user daily budgets; every question's cost is logged per user.
7. **Audit**: sign-ins, failures, questions, every executed SQL, denied queries, admin actions
   and knowledge changes go to `logs/audit.jsonl`.
8. **Network**: binds to `127.0.0.1`; remote access only through an HTTPS tunnel, with the same
   sign-in.

## How it works

```
browser (static/index.html)
   |  POST /api/chat  --->  app.py   stdlib ThreadingHTTPServer, streams "data: {json}" events
   |                          |
   |                       agent.py  Claude tool loop, history trimming, cost accounting
   |                          |
   |                        db.py    validator -> oracledb (READ ONLY) -> capped result
   <---  tool / result / text_delta / final events
```

`knowledge.py` is the whole competence of the assistant: table and column meanings, join keys,
business rules ("stock on hand = received - issued, cancelled rows excluded, opening stock is
already in the ledger"), data-quality traps and ready-made query shapes. It is a plain string,
kept free of dates so the prompt cache stays warm.

## Quick start

```
pip install -r requirements.txt
copy .env.example .env                    (add ANTHROPIC_API_KEY, set the Oracle profile)
copy knowledge_example.py knowledge.py    (describe your schema here)
python manage.py create admin admin       (first admin user; password asked interactively)
python app.py                             (opens http://127.0.0.1:8765 - sign in)
python chat.py "current stock of diesel by store"   (terminal, runs as admin)
```

Oracle credentials are read from environment variables (`ORA_USER`, `ORA_PWD`, `ORA_DSN`,
`ORA_SCHEMA`; on Windows also from `HKCU\Environment`, so `setx` values work without a
restart). Nothing secret lives in the repository.

## Configuration (`.env`)

| key | default | meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | | required |
| `CHAT_MODEL` | `claude-opus-5` | `claude-haiku-4-5` is about 5x cheaper, weaker on hard SQL |
| `CHAT_EFFORT` | `low` | `low` / `medium` / `high` |
| `DB_PROFILE` | `ORA_` | prefix of the credential variables, e.g. `ORA3_` for a second DB |
| `ORACLE_CLIENT_DIR` | | Oracle client directory for thick mode; empty = thin mode |
| `MAX_ROWS` | `200` | rows returned per query |
| `SQL_TIMEOUT_SEC` | `60` | per-query time limit |
| `DAILY_BUDGET_USD` | `2.00` | hard stop on API spend per day |
| `PORT` | `8765` | web UI port |

Model, effort, budgets and row cap can also be changed at runtime in the admin panel
(stored in `data/settings.json`, which overrides `.env`).

### What it costs

Measured on the production schema with the schema notes cached (about 16.5k cached tokens,
3k fresh input tokens and 600 output tokens per question):

| model | per question | 10 questions/day | 50 questions/day |
|---|---|---|---|
| Claude Opus 5 (low effort) | about $0.04 | about $12/month | about $55/month |
| Claude Sonnet 5 | about $0.017 | about $5/month | about $21/month |
| Claude Haiku 4.5 | about $0.008 | about $2.5/month | about $11/month |

The admin panel recomputes this table from your own usage log. Daily budgets (global and per
user) stop the AI service when reached.

## Running fully local (no data leaves the machine)

The answering model is a setting, not a dependency. Two providers ship:

| provider | where the model runs | cost | what leaves the machine |
|---|---|---|---|
| `anthropic` | Anthropic's API | cents per question | the question, the schema notes and the query results, over TLS |
| `ollama` | [Ollama](https://ollama.com) on this PC or a server you control | $0 | nothing |

Switch in the admin panel (Model & cost card) or in `data/settings.json`. The browser page loads no
external resources either: scripts, styles and fonts are served by the app, so with the local
provider the only network traffic is browser, this server, and the Oracle database.

Measured on an office PC with no GPU (Intel i5, 16 GB RAM, `qwen3:8b`): first answer about
5 minutes, follow-ups about 90 seconds, and small-model mistakes on flags and joins. A 30B-class
model on a 24 GB GPU is the realistic setup for daily use. Roles, allow-lists, learning and the
audit log work identically with either provider, because they are enforced by the server, not by
the model.

```bash
ollama pull qwen3:8b       # once; then pick "Local model via Ollama" in the admin panel
```

## Backups

Everything the assistant knows lives in files that are deliberately not in this repository:
`knowledge.py`, `data/` (users, settings, learned notes, group mapping), `logs/` and `.env`.
`backup.cmd` copies them to a dated folder on another drive; run it by hand or schedule it with
Windows Task Scheduler (daily, "Start a program", pointing at `backup.cmd`).

## Public access

The app has to run where it can reach the database, so "hosting" means a tunnel from that
machine. With cloudflared installed, `tunnel.cmd` exposes the running app on a
`https://....trycloudflare.com` URL for as long as the window stays open. The same sign-in
applies to remote visitors.

## Project layout

| file | role |
|---|---|
| `app.py` | HTTP server: sign-in, sessions, streaming chat, CSV, feedback, admin API, security headers |
| `auth.py` | users file, password hashing, sessions, lockout |
| `policy.py` | role definitions, per-role table allow-list, role prompts |
| `store.py` | runtime settings, learned knowledge, feedback and audit stores |
| `agent.py` | Claude tool loop, role-aware tools, cost accounting, history trimming, learning |
| `db.py` | connection, SELECT validator, capped execution, schema helpers |
| `static/index.html`, `app.js`, `app.css` | the front end (sign-in, conversations, profile page, admin panel) |
| `knowledge_example.py` | template for the schema notes (copy to `knowledge.py`) |
| `manage.py` | command-line user management |
| `config.py` | `.env` loader |
| `chat.py` | terminal client (admin role) |
| `run.cmd` / `tunnel.cmd` | Windows launchers |

## License

MIT
