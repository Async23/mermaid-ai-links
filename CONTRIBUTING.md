# Contributing

Thanks for helping improve `mermaid-ai-links`.

## Development setup

The project targets macOS and Python 3.11 or newer. Install
[uv](https://docs.astral.sh/uv/), then run:

```zsh
git clone https://github.com/Async23/mermaid-ai-links.git
cd mermaid-ai-links
uv sync --locked --all-groups
```

## Before opening a pull request

```zsh
uv run ruff check src tests
uv run ruff format --check src tests
uv run -m unittest discover -s tests -p 'test_*.py'
uv build
```

Keep pull requests focused and explain the user-visible behavior being changed.
Add or update tests for behavior changes.

## Repository privacy rule

Never commit a generated `http://127.0.0.1:38473/v1/open/...` link. Its signed
token contains an encoded absolute local path. Public architecture documents
therefore contain Mermaid blocks without generated links; use an ignored
`docs/*.local.md` copy for local click testing.

Do not commit real Mermaid Chart project URLs, credentials, cookies, HMAC
secrets, browser profiles, logs, or local configuration.

## End-to-end testing

Real E2E tests need a dedicated Chrome profile, a logged-in Mermaid Chart
session, and a local configuration. They are intentionally not run by GitHub
Actions:

```zsh
mermaid-ai-links start
uv run tests/e2e_links.py
```

The test creates background browser targets and stages public unlinked
documentation in a temporary directory. It must not focus the user's browser.

## Mermaid source conventions

- Use ASCII letters, digits, and underscores for node IDs.
- Quote labels containing punctuation or Chinese text.
- Use `<br/>` for line breaks inside node labels.
- Keep diagrams compatible with the Mermaid versions embedded by common
  Markdown editors unless the documentation explicitly says otherwise.

