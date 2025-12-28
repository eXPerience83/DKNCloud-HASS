# Contributing

## Development requirements

- **Python 3.14** is required to run this repository’s development tooling locally
  (Ruff/Black/pytest).
- **Runtime compatibility:** Home Assistant currently runs on **Python >= 3.13.2**.
  The integration runtime code is expected to remain compatible with this baseline.
  Please avoid using Python 3.14-only language features in runtime code.

## Local development

1) Create a virtual environment (Python 3.14):

   python3.14 -m venv .venv
   . .venv/bin/activate

2) Install dependencies:

   pip install -U pip
   pip install -r requirements-dev.txt

   (If there is no requirements-dev.txt, install the project’s dev deps as used by CI.)

3) Run checks:

   ruff check .
   black --check .
   pytest -q
