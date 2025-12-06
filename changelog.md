# Changelog

## Unreleased
- Notifications overhaul:
  - YouTube: `!notifications youtube add` accepts channel URLs/@handles/IDs, stores channel titles, and lists entries with human-readable names and target channels. Duplicate-video suppression via `notified_videos` kept.
  - Twitch: `!notifications twitch add` accepts profile URLs/usernames, stores display names, and lists entries with display + username and target channels. Uses client-credentials auth only.
  - Notification channel per subscription or default via `!notifications channel`.
- DB layer refactor: service class with typed helpers for servers, links, custom messages, music limits, notification channels, YouTube/Twitch subscriptions, and stream status; env-first Mongo config.
- Music command gating: per-guild channel limits via `!music_channels` (list/add/remove/clear) persisted in Mongo with cog caching.

## e8a20c8 — docs: add repository guidelines
- Added contributor guide `AGENTS.md` with structure, commands, and security practices.

## ebb37b0 — Simplify Dockerfile and update dependencies
- Streamlined Docker build; refreshed Python dependencies.

## bbaae38 — Basic notification addition with .env as main token handling method.
- Introduced notification features using environment-based token/config handling.

## 3a16df2 — feat(music): Enhance audio extraction and playback handling with improved error management and user feedback
- Improved music playback reliability, error reporting, and extraction logic.

## 7a011fc — Remove mongo migrations md file.
- Cleaned obsolete Mongo migration documentation.

## a92bdcf — Update .gitignore
- Expanded ignore rules for generated and local files.

## 738c3d3 — Remove jsons
- Removed unused JSON artifacts from the repo.

## 63c27f0 — feat(bot): Added seeking command, and minor fixes to playback.
- Added music seek command and playback stability fixes.

## 7520357 — feat(bot): Update music bot to better handle playback.
- General playback improvements and bug fixes in music commands.

## bac4517 — Merge pull request #1 from Leos-Khai/mongo-migration
- Integrated Mongo migration work from feature branch.

## 49ba4e9 — refactor(database): Refactor code to use mongodb
- Moved persistence to MongoDB; introduced new database layer.

## 4568536 — feat(migration): add MongoDB migration guide and implementation details
- Added migration guide and supporting implementation for MongoDB transition.

## b7c5681 — chore: remove duplicate file
- Removed redundant file to tidy the codebase.

## 59eae59 — fix(music): improve volume management and file structure
- Adjusted volume handling and reorganized music code layout.

## e1b8bd2 — docs(bot): Updated readme
- Refreshed README with latest bot usage details.

## 997403a — fix: prevent multiple 'no permission' messages
- Debounced permission error responses to avoid spam.

## 89e7184 — feat: update workspace with recent changes
- Synced workspace with various improvements and fixes.

## 5b03a91 — Remove unused dependencies from requirements.txt
- Pruned unused Python dependencies.

## 7a4d3c3 — Add SSH setup guide for Windows and WSL
- Added `ssh-setup.md` for developer onboarding.

## f90d7e0 — Merge branch 'main' of github.com:Leos-Khai/discord-bot
- Merge sync.

## b1b0422 — new requirements
- Updated dependency pins.

## 9f5cb12 — Refactor Dockerfile for multi-stage build, add music commands cog, and update README with Docker instructions
- Introduced multi-stage Docker build and structured music commands into a cog; updated Docker docs.

## 0aa592b — New stuff
- General feature and maintenance updates.

## b2938e2 — Various updates: added link/update/role commands and db updates
- Added `!list_links`, `!update_channel`, `!update_role`, `!remove_role`; expanded DB helpers.

## b9f12bc — Clone all voice channel shit.
- Added voice channel utilities and related changes.

## 7a63813 — Update note
- Documentation note update.

## f01d435 — Initial Commit / 05255a8 — Initial commit
- Initial project scaffolding and first tracked files.
