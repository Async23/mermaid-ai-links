## Outcome

Describe the user-visible result and the entry point affected.

## Verification

- [ ] `uv run ruff check src tests`
- [ ] `uv run ruff format --check src tests`
- [ ] `uv run -m unittest discover -s tests -p 'test_*.py'`
- [ ] `uv build`
- [ ] Tests were added or updated for behavior changes
- [ ] No credentials, signed local links, personal paths, private config, or private Mermaid source were committed

## Notes

Document compatibility, security, or migration considerations.
