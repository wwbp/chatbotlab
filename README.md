# ChatbotLab

[![CI](https://github.com/wwbp/chatbotlab/actions/workflows/ci.yml/badge.svg)](https://github.com/wwbp/chatbotlab/actions/workflows/ci.yml)
[![Docs](https://github.com/wwbp/chatbotlab/actions/workflows/docs.yml/badge.svg)](https://wwbp.github.io/chatbotlab/)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Django 5](https://img.shields.io/badge/django-5.2-092E20.svg)](https://www.djangoproject.com/)

**An open-source platform for running LLM chat experiments inside research studies.**

Researchers configure bots — model, prompt, persona, typing delays, moderation thresholds — through a Django admin panel, with no code. Participants reach the chat UI embedded in a survey (Qualtrics, REDCap, LimeSurvey), recruited through Prolific or MTurk. Every message, along with the prompt and history that produced it, is stored for later analysis.

📖 **[Full documentation](https://wwbp.github.io/chatbotlab/)** — study design, survey integration, deployment, and data analysis.

---

## Who this README is for

| You are | Start here |
|---|---|
| **A researcher** running a study | [Documentation site](https://wwbp.github.io/chatbotlab/) — you do not need this repo |
| **An engineer** contributing code | [Quick start](#quick-start) → [Development](#development) |
| **An operator** deploying it | [Quick start](#quick-start) → [Deployment](#deployment) |

---

## Quick start

**Prerequisites:** Docker with Compose, Make, and Git. Nothing else — Python 3.12 and Node 18 run inside the containers.

```bash
git clone https://github.com/wwbp/chatbotlab.git
cd chatbotlab

cp api/.env.example api/.env    # then edit — see below
cp web/.env.example web/.env    # defaults are fine for local work

make up                         # build and start everything (first run: several minutes)
make superuser                  # create the admin login
```

Before `make up`, edit `api/.env` and set at minimum:

| Variable | Why |
|---|---|
| `SECRET_KEY` | Django will not start without it |
| `MYSQL_USER` / `MYSQL_PASSWORD` / `MYSQL_ROOT_PASSWORD` | Any values you like — they create the local database user |
| `DATABASE_USER` / `DATABASE_PASSWORD` | **Must match** the `MYSQL_*` pair above |
| `DJANGO_SUPERUSER_USERNAME` / `_PASSWORD` | What `make superuser` will create |
| `OPENAI_API_KEY` | Needed for real replies **and for moderation** |

> The database user is created on the **first** `make up` only. If you change the `MYSQL_*` values later, run `make reset` to rebuild the volume, or the app will fail to connect.

Then:

| Service | URL |
|---|---|
| Chat UI | http://localhost:3000 |
| Django admin | http://localhost:8000/api/admin/ |

Log into the admin, create a **Bot** (name, prompt, model), and open the chat UI to talk to it.

Migrations run automatically when the backend container starts, so there is no migrate step here. `make superuser` **is** required — the development image does not create one for you.

### Working without API keys or cost

A fresh checkout makes **real, billed API calls** — `api/.env.example` sets `MOCK_LLM=false`. To develop without keys or spend, set `MOCK_LLM=true` in `api/.env` and restart the backend:

```bash
docker compose -f .devcontainer/docker-compose.yml up -d --force-recreate backend
```

Every chat then returns a canned string instantly. Note what else that switches off:

- Replies are not real model output
- **Moderation never blocks anything** — [`moderate_message`](api/chatbot/services/moderation.py) returns before it calls the API

So mock mode is right for UI and plumbing work, and wrong for anything touching model output or moderation. Switch it back when testing those.

---

## Architecture

```
Survey platform (Qualtrics / REDCap / LimeSurvey)
        │  embeds
        ▼
┌──────────────────┐     /api/chatbot/      ┌──────────────────┐
│  React chat UI   │ ─────────────────────► │  Django (ASGI)   │
│  (Vite, :3000)   │ ◄───────────────────── │  (:8000)         │
└──────────────────┘                        └────────┬─────────┘
                                                     │
                         ┌───────────────────────────┼──────────────────────────┐
                         ▼                           ▼                          ▼
                 ┌───────────────┐         ┌──────────────────┐        ┌────────────────┐
                 │ OpenAI        │         │  MariaDB         │        │  Redis         │
                 │ moderation    │         │  bots, convos,   │        │  conversation  │
                 │ + LLM engines │         │  utterances      │        │  history cache │
                 └───────────────┘         └──────────────────┘        └────────────────┘
```

**What happens on each message** — `POST /api/chatbot/` → [`run_chat_round`](api/chatbot/services/runchat.py):

1. **Moderate first.** The message goes to OpenAI's moderation endpoint. If any category exceeds the bot's threshold, the round stops here: no LLM call, a fixed warning is returned, and both rows are written to the database tagged with the category.
2. **Load history** from the Redis cache, falling back to the database, trimmed to the bot's transcript limit.
3. **Build the system prompt** from the bot's prompt plus the conversation's randomly assigned persona.
4. **Call the LLM** through [Kani](https://github.com/zhudotexe/kani), which abstracts OpenAI, Anthropic, and AWS Bedrock behind one interface.
5. **Persist and cache** the reply, together with the exact prompt and history that produced it — that provenance is what makes the data analysable later.

Human-like touches (typing delays, message chunking, idle follow-ups) are computed per bot and returned alongside the reply for the frontend to play back.

### Repo layout

```
api/                Django backend
  chatbot/            models, views, admin, services (chat, moderation, voice, follow-up)
  server/             LLM engine factory (OpenAI / Anthropic / Bedrock)
  generic_chatbot/    settings, ASGI entrypoint, root URLs
web/                React + Vite chat UI
docs/               Sphinx documentation (published to GitHub Pages)
infra/terraform/    Terraform for the AWS environment
.devcontainer/      Docker Compose stack for local development
conversation_data/  Synthetic sample transcripts for analysis examples
```

---

## Development

```bash
make up          # build and start all services
make down        # stop services
make reset       # stop and delete volumes (fresh database)
make migrate     # apply new migrations without restarting
make shell       # Django shell
make superuser   # create admin user from DJANGO_SUPERUSER_* in api/.env
make test        # backend + frontend tests
make coverage    # backend tests with an HTML coverage report
make lint        # ruff, isort, eslint, prettier — writes fixes
```

Most targets require the stack to be running.

### Tests

```bash
make test-api        # backend (pytest)
make test-web        # frontend (vitest)

# a single test, or anything else pytest accepts
docker exec chatbotlab-backend-1 pytest chatbot/tests/test_chatbot_view.py -k moderation -v
```

Backend tests live in `api/chatbot/tests/` and run against a real MariaDB, created as `test_<DATABASE_NAME>`. Tests marked `integration` make live API calls and are **deselected by default** (see `api/pytest.ini`); run them with `-m integration` and real keys.

### Database changes

Edit `api/chatbot/models.py`, then:

```bash
docker exec chatbotlab-backend-1 python manage.py makemigrations chatbot
make migrate
```

Commit the generated migration file. Check it reverses cleanly before opening a PR:

```bash
docker exec chatbotlab-backend-1 python manage.py migrate chatbot <previous_number>
make migrate
```

### CI

Every push runs [`ci.yml`](.github/workflows/ci.yml): backend lint (`ruff check`, `ruff format --check`, `isort --check`), backend tests against MariaDB and Redis, frontend lint, and frontend tests. All four must pass before a deploy workflow will run.

---

## Configuration

All configuration is environment variables, loaded from `api/.env` and `web/.env` locally. Neither file is committed — copy the `.env.example` alongside each.

### Backend (`api/.env`)

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Django secret key. **Required.** |
| `DEBUG` | `true` locally, never in production |
| `DATABASE_*` | Connection Django uses |
| `MYSQL_*` | Credentials the MariaDB container is created with — must agree with `DATABASE_*` |
| `REDIS_URL` | Conversation history cache |
| `OPENAI_API_KEY` | LLM replies **and moderation** — moderation silently no-ops without it |
| `ANTHROPIC_API_KEY` | Optional, for Anthropic-backed bots |
| `AWS_*` | S3 for avatars and voice recordings; required in production |
| `MOCK_LLM` | `true` skips all LLM **and moderation** calls and returns canned replies |
| `DJANGO_SUPERUSER_*` | Used by `make superuser`; on Elastic Beanstalk the entrypoint creates the user on first boot |
| `GUNICORN_WORKERS` | Production worker count (default 4) |

### Frontend (`web/.env`)

| Variable | Purpose |
|---|---|
| `VITE_API_URL` | Optional backend base URL, e.g. `http://localhost:8000/api`. Unset uses a relative `/api`, which the dev server and CloudFront both proxy to the backend. |

Everything about a study — prompts, personas, model choice, typing delays, moderation thresholds — is configured per bot in the admin, not in environment variables.

---

## Deployment

Deploys are triggered by pushing to a branch. CI must pass first.

| Branch | Deploys to | Workflow |
|---|---|---|
| `staging` | `dev.bot.wwbp.org` | [`staging.yml`](.github/workflows/staging.yml) |
| `main` | `bot.wwbp.org` | [`production.yml`](.github/workflows/production.yml) |
| `docs` | [GitHub Pages](https://wwbp.github.io/chatbotlab/) | [`docs.yml`](.github/workflows/docs.yml) |

Each deploy builds the React app to S3 behind CloudFront, and the Django app to Elastic Beanstalk. On boot the production container applies migrations, collects static files, loads bots, and creates the superuser if `DJANGO_SUPERUSER_PASSWORD` is set — see [`api/entrypoint.sh`](api/entrypoint.sh).

Production runtime configuration lives in Elastic Beanstalk environment variables, not in a `.env` file. `SECRET_KEY`, database credentials, and LLM API keys must be set there before the first deploy.

### Infrastructure

AWS resources are defined in [`infra/terraform/`](infra/terraform/) and managed by [`deploy-infrastructure.yml`](.github/workflows/deploy-infrastructure.yml) (with a matching destroy workflow). Researchers standing up their own instance should follow the [deployment guide](https://wwbp.github.io/chatbotlab/deployment/) rather than running Terraform by hand.

---

## Troubleshooting

**`make up` fails with a container name conflict**
The Compose file claims the fixed names `db`, `redis`, and `react_app`. Another project using those names will collide — remove or rename the stale containers (`docker rm <name>`; named volumes are not affected).

**Tests fail with `Access denied ... to database 'test_chatbot_db'`**
The database user lacks rights to create Django's test database. A fresh volume grants them automatically; an older one may not. Fix in place:

```bash
docker exec db mariadb -uroot -p"$MYSQL_ROOT_PASSWORD" \
  -e "GRANT ALL PRIVILEGES ON \`test\_%\`.* TO '<DATABASE_USER>'@'%'; FLUSH PRIVILEGES;"
```

**Admin login rejects correct credentials**
The user probably does not exist — the development image does not create one. Run `make superuser`.

**Chat replies are always "This is a mock response for load testing"**
`MOCK_LLM=true`. See [Working without API keys or cost](#working-without-api-keys-or-cost).

**Moderation never blocks anything**
Either `MOCK_LLM=true`, or `OPENAI_API_KEY` is unset — both make `moderate_message` return before it calls the API. Global moderation can also be switched off in the admin under *Global Moderation Settings*.

**Database connection errors after changing `.env`**
The MariaDB user is created only on first initialisation. Run `make reset` to rebuild the volume — this deletes local data.

---

## Contributing

Branch from `main`, keep CI green, and open a pull request. See [CONTRIBUTING.md](CONTRIBUTING.md) for the working conventions.

## License

No license file is currently present, so default copyright applies and reuse rights are not granted. Maintainers: adding a `LICENSE` would let others use and contribute to this work.
