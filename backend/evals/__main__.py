"""Print an evaluation report.

python -m evals
"""

import sys

from evals.runner import EvalRunner, load_grounding_cases, load_investigation_cases


def main() -> int:
    grounding_cases = load_grounding_cases()
    report = EvalRunner().run(load_investigation_cases(), grounding_cases)

    print(report.summary())

    false_rejections = report.false_rejections(grounding_cases)
    if false_rejections:
        print(
            "\nFALSE REJECTIONS — sound diagnoses that grounding discarded. "
            "This silently disables the model path:"
        )
        print("\n".join(f"  - {case_id}" for case_id in false_rejections))

    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
