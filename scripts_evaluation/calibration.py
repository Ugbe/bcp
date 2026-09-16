"""RMS calibration error, with no third-party dependencies.

The shipped evaluators compute this through ``evaluate_with_openai``, which
imports the OpenAI and vLLM clients at module load, so a machine that only needs
the metric cannot use them. This module reimplements the same upstream binning
(Hendrycks' ``calib_err`` with p=2 and a target bin size of 100) in the standard
library.

It agrees with the evaluator exactly when confidences are distinct. The upstream
code sorts with ``numpy.argsort``, whose default quicksort is not stable, so
answers sharing a confidence value are binned in an arbitrary order and the two
implementations can differ by a point or so on real data. Prefer the value an
evaluator already wrote into ``evaluation_summary.json``; use this module when
no such summary exists.
"""

from __future__ import annotations

from math import sqrt

DEFAULT_BIN_SIZE = 100


def rms_calibration_error(
    confidences: list[float], correctness: list[bool], beta: int = DEFAULT_BIN_SIZE
) -> float:
    """Return the RMS calibration error as a percentage.

    ``confidences`` are percentages in 0-100. Pairs are sorted by confidence and
    cut into bins of ``beta``; the last bin absorbs the remainder. As in the
    upstream implementation, the final bin is excluded from the sum, and the
    sort does not break ties, so the result depends on the input order when
    several answers share a confidence value.
    """

    if len(confidences) != len(correctness):
        raise ValueError("confidences and correctness must have equal length")
    if not confidences:
        raise ValueError("at least one confidence is required")

    pairs = sorted(
        zip((value / 100.0 for value in confidences), (float(value) for value in correctness)),
        key=lambda pair: pair[0],
    )
    total = len(pairs)
    bin_count = total // beta
    bounds = [[index * beta, (index + 1) * beta] for index in range(bin_count)]
    if not bounds:
        return 0.0
    bounds[-1] = [bounds[-1][0], total]

    error = 0.0
    for start, end in bounds[:-1]:
        window = pairs[start:end]
        if not window:
            continue
        mean_confidence = sum(pair[0] for pair in window) / len(window)
        mean_correct = sum(pair[1] for pair in window) / len(window)
        error += len(window) / total * (mean_confidence - mean_correct) ** 2
    return sqrt(error) * 100
