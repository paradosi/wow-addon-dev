# wow-api-mcp

# List available recipes
default:
    @just --list

# Create the venv and install deps (uv manages .venv automatically)
setup:
    uv sync

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

# Run the MCP server on stdio (what Claude Code launches)
run *ARGS:
    uv run python -m wow_api_mcp {{ARGS}}

# Fetch upstream (wow-ui-source, BlizzardInterfaceResources, Ketho annotations) and rebuild the index
refresh:
    uv run wow-api-build

# Rebuild the index from upstream data already on disk
build:
    uv run wow-api-build --offline

# Cross-flavor API check of one or more addon directories (args: paths, --flavor X, --json)
scan +ARGS:
    uv run wow-api-scan {{ARGS}}

# Scan every addon in ~/projects/wowaddons
scan-all:
    uv run wow-api-scan ~/projects/wowaddons/*/
