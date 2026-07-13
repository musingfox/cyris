# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] — 2026-07-14

Initial public release.

- Fetch from Miniflux RSS + newsletters, tier-based LLM filtering/summarization,
  Obsidian markdown digest output.
- LLM providers: Anthropic Claude (default) and Google Gemini, with graceful
  degradation on LLM failure.
- Swipe-based triage web UI; preference learning from digest feedback.
- Docker Compose stack (Miniflux + Postgres + cyris) and macOS launchd scheduling.
- Optional Cloudflare Workers for email-newsletter ingestion and promote/HTML publish.

[0.1.0]: https://github.com/musingfox/cyris/releases/tag/v0.1.0
