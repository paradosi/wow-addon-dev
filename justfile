# wow-addon-dev — the WoW addon development environment

# List available recipes
default:
    @just --list

# Install deps, pinned tools (LuaLS, packager), fetch upstream, build the index
setup:
    uv sync
    uv run wowdev setup

# Run the test suite
test:
    uv run pytest -q

# Lint and check formatting
lint:
    uv run ruff check .
    uv run ruff format --check .

# Autofix lint and format
fmt:
    uv run ruff check --fix .
    uv run ruff format .

# Update upstream data and rebuild the index
refresh:
    uv run wowdev refresh

# Rebuild the index from upstream data already on disk
build:
    uv run wowdev refresh --offline

# The full static gate on addons (default all); add --types / --package
check *ARGS:
    uv run wowdev check {{ARGS}}

# Any wowdev subcommand: just wowdev api UnitAura
wowdev *ARGS:
    uv run wowdev {{ARGS}}

# Run the MCP server on stdio (what Claude Code launches)
run:
    uv run wowdev mcp

# Install the weekly index refresh (Tue 06:00, after patch day) as a system timer
install-timer:
    sudo install -m 644 systemd/wowdev-refresh.service systemd/wowdev-refresh.timer /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now wowdev-refresh.timer
    systemctl list-timers wowdev-refresh.timer --no-pager
