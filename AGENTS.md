# Repository Guidelines

## Project Structure & Module Organization
- Core entrypoint is `src/main.py`, which loads config, initializes Mongo via `src/db.py`, attaches logging from `src/logger.py`, and wires events/cogs.
- Commands live in `src/cogs/` (`admin.py`, `general.py`, `music.py`, `notifications.py`); keep new features modular by adding a cog per domain.
- Event listeners live in `src/events/` (join, ready, voice state). Prefer adding new listeners here instead of in cogs.
- Runtime config comes from `.env` (see `.env.example`) or `src/config.json`; avoid committing secrets. Helper assets/logs include `src/discord_bot.log` and the legacy `src/discord_bot.db`.
- Utility scripts sit in `scripts/` (e.g., `migrate_to_mongo.py` for data migration).

## Build, Test, and Development Commands
- Create and activate a virtualenv, then install deps: `python -m venv .venv && .\\.venv\\Scripts\\activate && pip install -r requirements.txt`.
- Run the bot locally (requires `DISCORD_TOKEN`, `MONGODB_URI`, `MONGODB_DATABASE`): `python src/main.py`.
- Docker workflow: `docker build -t discord-bot .` then `docker run -d --name discord-bot -v %cd%\\src:/app/src discord-bot`.
- Data migration: `python scripts/migrate_to_mongo.py` (run after setting Mongo env vars).

## Coding Style & Naming Conventions
- Python 3.12, PEP 8 defaults: 4-space indents, snake_case for functions/vars, CapWords for classes, lowercase command names.
- Use async/await consistently with `discord.py`; avoid blocking calls inside cogs/events.
- Route all diagnostics through `logger = get_logger()` instead of `print`; redact tokens/URIs in logs.
- Keep cogs focused on one concern and register with clear, user-facing command help strings.

## Testing Guidelines
- No formal test suite yet; sanity-check changes by running `python src/main.py` in a test guild and exercising affected commands/events (e.g., `!ping`, `!play`, join/leave voice).
- For Mongo changes, verify collections initialize without errors on startup and that updates persist between runs.
- When adding features, include a short manual test plan in the PR (commands run, expected responses, screenshots if UI-facing in Discord).

## Commit & Pull Request Guidelines
- Prefer Conventional Commits (`feat(scope): message`, `fix(scope): message`); scopes like `music`, `notifications`, `bot`, or `database` match current structure (`git log` shows `feat(music): ...` and maintenance summaries).
- Keep commits small and descriptive; note config or migration impacts explicitly.
- PRs should summarize intent, list testing performed, call out config/env requirements, and include screenshots/log excerpts for user-facing changes. Link issues when applicable and request review when secrets/config changes are needed.

## Security & Configuration Tips
- Never commit `.env`, tokens, or Mongo credentials. Use `.env.example` for new settings and prefer environment variables over `src/config.json` in production.
- Restrict Discord bot permissions to what the feature needs; avoid logging full member identifiers when not required.
