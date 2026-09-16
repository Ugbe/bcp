import random
import unittest

from scripts_evaluation.calibration import rms_calibration_error

try:
    from scripts_evaluation.evaluate_with_openai import calculate_calibration_error
except Exception:  # pragma: no cover - the reference needs the OpenAI client
    calculate_calibration_error = None


class CalibrationTest(unittest.TestCase):
    def test_rejects_mismatched_lengths(self):
        with self.assertRaises(ValueError):
            rms_calibration_error([90.0], [True, False])

    def test_rejects_empty_input(self):
        with self.assertRaises(ValueError):
            rms_calibration_error([], [])

    def test_zero_below_one_bin(self):
        self.assertEqual(rms_calibration_error([90.0] * 99, [True] * 99), 0.0)

    def test_perfectly_calibrated_pair_of_bins(self):
        confidences = [0.0] * 100 + [100.0] * 100
        correctness = [False] * 100 + [True] * 100
        self.assertAlmostEqual(rms_calibration_error(confidences, correctness), 0.0)

    def test_fully_overconfident_first_bin(self):
        # Two bins of 100; upstream excludes the last bin, so the first bin's
        # full error is spread over all 200 answers: sqrt(100/200 * 1) = 0.7071.
        confidences = [100.0] * 200
        correctness = [False] * 200
        self.assertAlmostEqual(rms_calibration_error(confidences, correctness), 70.710678, places=5)

    @unittest.skipIf(calculate_calibration_error is None, "reference implementation unavailable")
    def test_matches_reference_implementation_without_ties(self):
        # numpy's argsort is not stable, so the implementations agree exactly
        # only when no two answers share a confidence value.
        rng = random.Random(17)
        for size in (100, 250, 830):
            confidences = [value / 100 for value in rng.sample(range(1, 10001), size)]
            correctness = [rng.random() < 0.6 for _ in range(size)]
            self.assertAlmostEqual(
                rms_calibration_error(confidences, correctness),
                calculate_calibration_error(confidences, correctness),
                places=9,
            )

    def test_tied_confidences_are_binned_in_stable_input_order(self):
        confidences = [90.0] * 300
        correctness = [index < 150 for index in range(300)]
        self.assertAlmostEqual(
            rms_calibration_error(confidences, correctness),
            rms_calibration_error(confidences, list(correctness)),
        )


if __name__ == "__main__":
    unittest.main()
