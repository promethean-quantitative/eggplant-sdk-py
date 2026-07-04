# Contributing

Thanks for helping out. Real money moves through this code, so the bar is:
every behavior change comes with tests, and CI must be green before anything
merges or ships.

## Development setup

Requires Python ≥ 3.11.

```bash
git clone https://github.com/promethean-quantitative/eggplant-sdk-py.git
cd eggplant-sdk-py
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

```bash
pytest
```

`asyncio_mode = "auto"` is set in `pyproject.toml`, so async tests need no
decorator — just write `async def test_*`. Fixtures are venue-shaped JSON:
when adding one, copy a real API response rather than hand-writing a minimal
one, so parsers are tested against what the venue actually sends.

## Lint and format

```bash
ruff check eggplant_sdk tests
ruff format eggplant_sdk tests
```

CI runs `ruff format --check`, so format before pushing. Line length is 100;
the lint rule set and its deliberate ignores live in `pyproject.toml`.

## Project conventions

Two rules carry the SDK's design and are not negotiable:

- **Money math is `Decimal`.** No `float` anywhere on an order-affecting
  path — prices, sizes, balances, fees. Parse wire strings straight into
  `Decimal`.
- **Wire parsers are lenient.** The venue adds fields, enum values, and tick
  sizes without notice. Unknown values degrade gracefully (e.g. to an
  `UNKNOWN` variant), and one malformed item is skipped — it never poisons
  the rest of the batch. A parser that raises on novel venue data is a bug.

## Pull requests

Branch from `main` and keep diffs focused — one concern per PR. CI runs on
every PR: ruff lint + format check, the test suite on Python 3.11/3.12/3.13,
and a package build. All of it must pass.

## Releasing (maintainers)

Publishing is automated: a `v*` tag pushed to GitHub triggers the `publish`
job in `.github/workflows/ci.yml`, which builds the sdist and wheel and
uploads them to [PyPI](https://pypi.org/project/eggplant-sdk/) via trusted
publishing (OIDC — no tokens involved). Publish only runs after lint, tests,
and build pass on the same run.

Release when users should get new code. Docs-only changes (README,
CONTRIBUTING, examples) just merge to `main` — no tag, no version bump.
The one side effect: the PyPI project page renders the README as it was at
the last release, so it catches up on the next one. Never push a tag without
bumping the version first — PyPI rejects the reused version number and the
publish job fails.

1. Bump the version in **both** places, keeping them identical:
   - `pyproject.toml` → `[project] version`
   - `eggplant_sdk/__init__.py` → `__version__`

   PyPI permanently rejects reusing a version number, so every release needs
   a fresh one. While the project is 0.x, breaking changes bump the minor
   version.
2. Commit, push to `main`, and let CI go green.
3. Tag that commit and push the tag:

   ```bash
   git tag -a v0.2.0 -m "eggplant-sdk 0.2.0"
   git push origin v0.2.0
   ```

4. Watch the run in the Actions tab; a couple of minutes after the publish
   job finishes, `pip install -U eggplant-sdk` serves the new version.
5. Optional: create GitHub release notes with
   `gh release create v0.2.0 --generate-notes`.

### If the publish job fails

Nothing is half-published — PyPI either accepted the release or it didn't.
Fix the cause and re-run just the failed job from the Actions tab; no new
tag needed. The usual suspect is the trusted-publisher config: PyPI's
publisher for this project is registered against exactly the repo
`promethean-quantitative/eggplant-sdk-py`, workflow `ci.yml`, environment
`pypi`. Renaming the workflow file or the environment breaks publishing
until the config on PyPI (project → Settings → Publishing) is updated to
match.
