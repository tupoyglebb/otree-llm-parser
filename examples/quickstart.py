"""Minimal example: parse an oTree experiment and build LLM-ready questionnaires.

Run from the repository root:

    python examples/quickstart.py path/to/experiment.otreezip
"""

import os
import sys
import warnings

# Make the parser importable when running this script from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from otree_parser import (  # noqa: E402
    parse_otreezip,
    format_questionnaire_for_llm,
    build_questionnaire_for_api,
)

warnings.filterwarnings("ignore")  # silence optional-dependency / fallback warnings


def main(otreezip_path: str) -> None:
    experiment = parse_otreezip(otreezip_path)

    print(f"Project:    {experiment.project_name}")
    print(f"Treatments: {[t.name for t in experiment.treatments]}\n")

    # 1) Human-readable overview of the first treatment's questionnaire.
    print(format_questionnaire_for_llm(experiment))

    # 2) One LLM-ready questionnaire (list of prompt strings) per treatment combination.
    questionnaires = build_questionnaire_for_api(
        experiment,
        include_all_treatment_variants=True,
        format_for_llm=True,
    )

    print(
        f"\nBuilt {len(questionnaires)} questionnaire(s); "
        f"the first contains {len(questionnaires[0])} sequential prompt elements."
    )
    # Each `element` below is a ready-to-send prompt for your LLM API:
    #
    #   for questionnaire in questionnaires:
    #       for element in questionnaire:
    #           response = call_your_llm_api(element)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python examples/quickstart.py path/to/experiment.otreezip")
    main(sys.argv[1])
