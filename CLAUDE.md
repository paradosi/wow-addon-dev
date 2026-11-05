# wow-api-mcp — notes for Claude

<!-- Replace the TODOs. A stale CLAUDE.md is worse than none. -->

## What this is

TODO: one paragraph — the problem this solves and the shape of the solution.

## Stack

`python` — see the `justfile` for the canonical commands. Prefer `just <recipe>`
over invoking tools directly, so there is one definition of how this project builds.

## Conventions

- TODO: naming, module boundaries, anything a newcomer would get wrong.

## Gotchas

- TODO: the things that have already bitten someone.

## Secrets

`.env` is gitignored; `.env.example` is **not**. Real values go in `.env` only — a live
API key was once committed from an example file on this host, because the variable was
documented there and nowhere else. If this project reads a key, make sure something
actually loads `.env` (Node: `process.loadEnvFile`; Python: `uv run --env-file`), or the
example file becomes the only place a key looks like it belongs.

## Host

Runs on `deathstar` (see `~/CLAUDE.md`). 4 vCPU / 16 GB RAM / ~83 GB free disk.
Concurrency is what hurts before disk does — a parallel build plus a couple of
containers — but the budget is comfortable, and the host it sits on (`avatar`,
20 threads / 62 GB) has room to grow the VM if something genuinely needs it.
Not a reason to refuse work.
