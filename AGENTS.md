# Repository Guidelines

## Project Structure & Module Organization
- Core entry points: `PixivUtil2.py` (CLI downloader) and `PixivDBManager.py` (DB maintenance).
- Shared utilities/auth/config live in `common/`; data models in `model/`; download and workflow logic in `handler/`.
- Tests under `test/` with fixtures in `test_data/`; HTML templates (`template.html`, `novel_template.html`) and packaging files (`setup.py`, `pyproject.toml`) sit at repo root.
- Keep personal runtime files (`config.ini`, cookies, downloads) out of git; `requirements.txt` is the authoritative dependency list.

## Build, Test, and Development Commands
- `python -m pip install -r requirements.txt` (or `pip install -e .` if iterating) installs deps; Python 3.10+ required (archive mode needs 3.13+).
- `python PixivUtil2.py` runs the app; it will prompt for configuration when no `config.ini` is present.
- `python PixivDBManager.py` inspects or repairs the local SQLite database.
- `pytest -v ./test_*` runs the test suite offline using bundled fixtures.
- Docker: `docker build -t pixivutil2 .` then run with the README command to bundle ffmpeg and run in a clean container.

## Coding Style & Naming Conventions
- Python with 4-space indents; follow existing PEP8 layout while tolerating long lines and inline disables noted in `.pep8`/`.pylintrc`.
- Prefer snake_case for functions/variables and CamelCase for classes; keep helpers in `common/PixivHelper.py`, models data-focused, handlers orchestrating flow.
- Avoid wide refactors purely for style; leave lint disables intact unless the logic is touched.

## Testing Guidelines
- Add new cases in `test/test_<feature>.py`; mirror existing naming like `test_PixivHelper.py`.
- Reuse JSON/HTML fixtures in `test_data/`; keep tests deterministic and without external network/auth.
- Run `pytest -v ./test_*` before pushing; add targeted regression coverage when fixing bugs.

## Commit & Pull Request Guidelines
- Commits use short, imperative subjects with optional scope (e.g., `Fix oauth refresh retry`, `Add fanbox post parsing test`); keep changes cohesive.
- PRs include a clear summary, linked issues, config/data migrations, and test results (`pytest -v`). Add screenshots when touching HTML templates or user-facing logs/output.
- Do not commit credentials, cookies, or downloaded assets; ensure any new configs have sane defaults.
