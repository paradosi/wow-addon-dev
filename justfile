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

# Run the package entrypoint
run *ARGS:
    uv run python -m wow_api_mcp {{ARGS}}
