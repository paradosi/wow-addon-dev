# Contributing to wow-addon-dev

Thanks for looking. This is a small project maintained in spare time, so short,
concrete reports and focused pull requests get handled fastest.

## Ways to help

- **Report a wrong API answer.** The index says a function exists on a client and
  it doesn't, or the reverse. Include the name, the client and its build
  (`/dump GetBuildInfo()` in game), and what actually happened.
- **Report a scan false positive or false negative.** The scan read a guard wrong:
  it flagged a call that can never run on that client, or passed one that errors
  in game. A few lines of Lua that reproduce it are worth more than a description.
- **Suggest an idea.** A check you wish the gate ran, a release step it could
  automate, an MCP tool that would help. Open an issue with the *Idea* template.
- **Send a fix.** See below.

## Development

```sh
git clone https://github.com/paradosi/wow-addon-dev
cd wow-addon-dev
just setup     # deps, pinned tools, upstream data, index (~1 min, ~400 MB)
just test      # pytest; index-backed tests skip if no index is built
just lint      # ruff check + format check
just fmt       # autofix
```

`just` is the entry point for everything; see `just --list`. Python is managed
with [uv](https://docs.astral.sh/uv/).

## Pull requests

- Keep each PR to one change, and say what it fixes and how you checked it.
- **Add a test for scanner changes.** `tests/test_core.py` has small Lua snippets
  run through `analyse()`; a guard bug should get one that fails before your fix.
  The scan is the part where a quiet mistake ships broken addons, so it is held
  to that.
- `just test` and `just lint` must pass.
- Update the README when behaviour, a command or a configuration variable changes.

## Data sources

The index is built from public repositories, which are downloaded locally and
never committed here: Gethe/wow-ui-source (Blizzard's UI code),
Ketho/BlizzardInterfaceResources, Ketho/vscode-wow-api, NumyAddon/FramexmlAnnotations,
and wago.tools DB2 tables. Please don't commit any of that data, or any game files.

## License

By contributing you agree that your contribution is licensed under the
[MIT License](LICENSE), the same as the rest of the project.
