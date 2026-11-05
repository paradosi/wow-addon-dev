# wow-api-mcp

<!-- One or two sentences: what this does and who it is for. Lead with the problem it
     solves, not the technology. This paragraph is what GitHub shows in search results
     and what a reader decides on in about five seconds. -->

TODO: what this project does and why it exists.

---

## Table of contents

- [Overview](#overview)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Development](#development)
- [Project structure](#project-structure)
- [How it works](#how-it-works)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Overview

<!-- Expand the summary into a paragraph or two. What problem prompted this? What does
     it replace or improve on? What is explicitly out of scope? -->

TODO

### Features

- **TODO** — what it does and why that matters
- **TODO**
- **TODO**

### Status

Scaffolded 2026-11-05. Stack: `python`.

> [!NOTE]
> State plainly whether this is experimental, in use, or abandoned. A reader cannot tell
> from the code, and a stale status line is worse than none.

---

## Requirements

| Requirement | Version | Notes |
|---|---|---|
| TODO | | |

---

## Installation

```sh
git clone <remote> wow-api-mcp
cd wow-api-mcp
just setup
```

---

## Usage

<!-- Show the shortest thing that produces a visible result, before any options. -->

```sh
TODO
```

### Examples

<!-- Two or three worked examples covering the common cases. Include the output. -->

```sh
TODO
```

---

## Configuration

Copy `.env.example` to `.env` and fill in the values.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `LOG_LEVEL` | no | `info` | Logging verbosity |
| TODO | | | |

> [!WARNING]
> `.env` is gitignored. Never commit real secrets — document **where** a value comes from
> and **how** to rotate it, never the value itself.

---

## Development

This project uses [`just`](https://github.com/casey/just) as its task runner. Every
project on this host exposes the same interface, whatever the stack:

| Recipe | Does |
|---|---|
| `just` | List all recipes |
| `just setup` | Install dependencies |
| `just test` | Run the test suite |
| `just lint` | Lint and check formatting |

<!-- Add stack-specific recipes here: run, build, typecheck, up, down, dump, etc. -->

Prefer `just <recipe>` over calling tools directly, so there is one definition of how this
project builds.

---

## Project structure

```
wow-api-mcp/
├── README.md        this file
├── CLAUDE.md        context for Claude Code
├── justfile         task definitions
├── .env.example     configuration template
└── TODO             describe the real layout here
```

---

## How it works

<!-- The section most projects skip and most readers need. Describe the actual mechanism:
     data flow, key abstractions, why it is built this way. Diagrams welcome. -->

TODO

### Design decisions

<!-- Record choices whose reasoning is not obvious from the code, and what you rejected.
     This is what stops someone "fixing" a deliberate decision six months from now. -->

- **TODO** — what was chosen, what was rejected, and why

---

## Troubleshooting

> [!TIP]
> Add failures here as you hit them. The ones that cost you an hour are exactly the ones
> you will hit again and not remember.

**TODO: symptom**
Cause, and the fix.

---

## License

TODO
