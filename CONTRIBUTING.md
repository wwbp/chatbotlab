# Contributing

Thanks for working on ChatbotLab. This covers the conventions the repo actually
follows; see the [README](README.md) for setup and architecture.

## Getting set up

Follow the [Quick start](README.md#quick-start). If any step there is wrong or
incomplete, fixing it is a genuinely useful first contribution.

## Workflow

1. Branch from `main` — `feat/`, `fix/`, `ci/`, `refactor/`, or `docs-` prefix.

   > Not `docs/`. A branch named `docs` already exists (it deploys the
   > documentation site), and git cannot have both a `docs` ref and a `docs/`
   > directory of refs — the push is rejected with `directory file conflict`.
   > Use `docs-` instead.

2. Make the change, with tests.
3. Run `make lint` and `make test` locally.
4. Open a pull request against `staging`. Merging deploys it to
   dev.bot.wwbp.org — check it there.
5. Release with a pull request from `staging` into `main`. Merging deploys it
   to production (bot.wwbp.org).

`main` and `staging` deploy on push, so nothing lands on either directly, and
`main` is always what production runs — branch from it. Production hosts live
studies, which is why every change goes through staging first.

## Tests

New behaviour needs a test. Bug fixes need a test that fails before the fix —
if it passes without your change, it is not testing the bug.

```bash
make test-api      # backend
make test-web      # frontend
docker exec chatbotlab-backend-1 pytest chatbot/tests/test_chatbot_view.py -k moderation -v
```

Backend tests run against a real MariaDB. Async tests need explicit
`@pytest.mark.asyncio` — `api/pytest.ini` sets no `asyncio_mode`. Tests that
make live API calls are marked `integration` and are deselected by default;
everything else must pass with no network access and no API keys.

Shared fixtures live in `api/chatbot/tests/conftest.py` (`make_bot`,
`make_conversation`, `mock_llm`). Prefer them over building objects by hand.

## Style

`make lint` writes fixes; CI checks the same rules and will fail on anything
left over.

- **Python** — ruff (lint + format, 88 cols) and isort with the black profile.
  Migrations are excluded from linting.
- **JavaScript** — eslint and prettier.

Match the surrounding code. Comments should explain *why* something is the way
it is, particularly where the reason is not obvious from the code — this
codebase has real constraints (async ORM behaviour, connection recycling,
provider quirks) that are easy to "clean up" and thereby break.

## Database migrations

Generate them, never hand-write schema changes:

```bash
docker exec chatbotlab-backend-1 python manage.py makemigrations chatbot
make migrate
```

Commit the generated file, and confirm it reverses cleanly before opening a PR:

```bash
docker exec chatbotlab-backend-1 python manage.py migrate chatbot <previous_number>
make migrate
```

Keep schema and data migrations in separate numbered files. A data migration
needs a working reverse function, and a test — put it in
`api/chatbot/tests/` and call the migration's functions directly.

Adding a field to a model is usually not the whole change. Check whether it
also belongs in the Django admin (`list_display`, `list_filter`, a fieldset) and
in the schema table in `docs/data/schema.rst`. A field missing from the admin is
invisible to the researchers who need it.

## Pull requests

Explain **why**, not just what. Reviewers can read the diff; what they cannot
recover is the reasoning, the alternatives you rejected, and the risks.

Call out explicitly:

- **Behaviour changes** — anything altering what participants see or what gets
  stored. This is a research platform: a silent change to bot behaviour can
  invalidate a running study.
- **Migrations**, and whether they are reversible.
- **Dependency changes**, especially anything relocking `api/Pipfile.lock`.

Keep unrelated changes in separate PRs.

## Reporting bugs

Include what you expected, what happened, and how to reproduce it. For anything
involving chat behaviour, say whether `MOCK_LLM` was `true` or `false` — it
changes both LLM replies and moderation, and it is behind a good share of
"this does not work locally" reports.
