# oTree → LLM Questionnaire Parser

> Turn an [oTree](https://www.otree.org/) behavioural experiment into a sequential, **LLM-ready questionnaire** — so you can *pilot* the experiment on large language models before fielding it on human participants.

**Status: research prototype / work in progress.** The core pipeline runs end-to-end on standard oTree projects, but the tool is still evolving — it is not yet packaged or covered by automated tests, and the output format may change. See [Project status & roadmap](#project-status--roadmap).

## Why

Running behavioural and survey experiments on people is slow and expensive. A growing line of computational-social-science work uses LLMs as **synthetic respondents** to pilot a design first — to pressure-test wording, spot fragile manipulations, and see where an instrument is likely to break — *before* any human data is collected.

oTree is one of the most widely used frameworks for building such experiments, but an `.otreezip` export is a full oTree/Django application (models, pages, HTML templates, randomisation logic) — not something you can hand to a model. This tool bridges that gap: it reads the experiment and reconstructs, **per experimental condition**, the exact sequence of instructions and questions a participant would see, formatted as ready-to-send prompts for an LLM API.

It is the engineering companion to my research on using LLMs to pilot survey and vignette experiments.

## What it does

- Unpacks an `.otreezip` export (ZIP or TAR.GZ) and locates the project root.
- Inspects each app's `Player` / `Group` / `Subsession` models and extracts fields, labels, answer choices, and help text.
- Inspects pages and templates and reconstructs the **visible page sequence per treatment**, evaluating `is_displayed()` where possible.
- Detects experimental conditions (treatments) — e.g. values randomly assigned in `creating_session` — and builds a **separate questionnaire for every treatment combination**.
- Emits the questionnaire as (a) human-readable Markdown, (b) structured dictionaries, or (c) a list of sequential prompt strings ready for an LLM API.
- Works **without oTree/Django installed**: if dynamic import fails, it falls back to static AST parsing of the source code.

## How it works

```
.otreezip  →  unpack  →  inspect models  →  inspect pages/templates
           →  resolve visible pages per treatment
           →  enumerate treatment combinations
           →  assemble sequential questionnaire (prompts)
```

## Quickstart

```bash
pip install -r requirements.txt   # only beautifulsoup4 (optional); the core is stdlib-only
```

```python
from otree_parser import parse_otreezip, build_questionnaire_for_api

experiment = parse_otreezip("examples/data/your_experiment.otreezip")

# One LLM-ready questionnaire per treatment combination:
questionnaires = build_questionnaire_for_api(
    experiment,
    include_all_treatment_variants=True,
    format_for_llm=True,
)

for q in questionnaires:        # each q is a list of prompt strings
    for element in q:
        ...                     # send `element` to your LLM API and collect the answer
```

A fuller walkthrough is in [`testing_parser.ipynb`](testing_parser.ipynb); a minimal runnable script is in [`examples/quickstart.py`](examples/quickstart.py).

### Example output

For a one-factor consumer-trust experiment (a vignette that varies how a company's charitable giving is framed), the parser produces one questionnaire per framing condition, each a sequence of prompts like:

```
ELEMENT 1  ИНФОРМАЦИЯ О TREATMENT: donation_text: ... жертвует 5% от прибыли ...
ELEMENT 2  ИНФОРМАЦИЯ (страница: Demographics): Добрый день! Приглашаем Вас принять участие ...
ELEMENT 3  ВОПРОС 1: Укажите свой возраст
ELEMENT 4  ВОПРОС 2: Укажите Ваш пол — Варианты: Мужской, Женский
...
ELEMENT 8  ВОПРОС 5: Насколько положительной Вам кажется репутация этой компании? (шкала 1–7)
```

Instructions and questions are preserved **verbatim** in the experiment's own language.

## Public API

| Function | Returns |
|---|---|
| `parse_otreezip(path)` | `Experiment` — structured description (treatments, pages, models) |
| `build_questionnaire_for_api(exp, ...)` | sequential prompt strings (or dicts) — one questionnaire per treatment combination |
| `format_questionnaire_for_llm(exp, ...)` | a single human-readable Markdown questionnaire |
| `extract_questionnaire_texts(exp, ...)` | flat list of question texts |
| `get_all_questions_with_options(exp, ...)` | list of `{question_text, answer_options, field_name, ...}` dicts |

## Repository structure

```
otree_parser.py            # the parser (single module)
structure_otree_parser.md  # detailed architecture notes
testing_parser.ipynb       # end-to-end walkthrough
examples/quickstart.py     # minimal runnable example
requirements.txt
LICENSE
```

## Project status & roadmap

**Works today**
- End-to-end parsing of standard single- and multi-app oTree projects.
- AST fallback, so projects parse even without an installed oTree runtime.
- Per-treatment questionnaire assembly with answer options and page instructions.

**Known limitations**
- Extraction is heuristic: unusual project layouts, complex `is_displayed()` logic, and fully dynamic page sequences may be missed.
- Treatment detection currently recognises specific assignment patterns (e.g. `random.choice` over a list stored in `participant.vars`); other randomisation schemes are not covered yet.
- Template-text extraction relies on BeautifulSoup plus heuristics for Jinja2 / oTree tags.
- No automated test suite or packaging yet; the output format may still change.

**Planned**
- A pip-installable package and a fixture-based test suite.
- Broader coverage of treatment assignment and conditional display.
- An optional runner that sends questionnaires to an LLM API and parses the responses back into tidy data.
- Configurable system prompts / persona conditioning for synthetic respondents.

## Background

This tool grew out of coursework on using large language models as instruments for **piloting survey and vignette experiments** in the social sciences — generating synthetic respondents to stress-test an experimental design before it is fielded on people. The parser operationalises that idea: it converts existing oTree experiments into a format that can be replayed on a model.

## License

MIT — see [LICENSE](LICENSE).
