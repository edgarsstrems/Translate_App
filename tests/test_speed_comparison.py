import unittest
from scripts.compare_speed_modes import run_benchmark


class SpeedModeComparisonTests(unittest.TestCase):
    def test_benchmark_metrics_and_accuracy(self):
        report = run_benchmark()
        balanced = report["balanced"]
        low_latency = report["low_latency"]
        comp = report["comparison"]

        # Accuracy checks
        self.assertEqual(balanced["missing_words_count"], 0)
        self.assertEqual(low_latency["missing_words_count"], 0)
        self.assertEqual(balanced["repeated_words_count"], 0)
        self.assertEqual(low_latency["repeated_words_count"], 0)
        self.assertEqual(balanced["wer"], 0.0)
        self.assertEqual(low_latency["wer"], 0.0)
        self.assertEqual(balanced["biblical_accuracy_pct"], 100.0)
        self.assertEqual(low_latency["biblical_accuracy_pct"], 100.0)

        # Latency checks
        self.assertLess(low_latency["avg_latency"], balanced["avg_latency"])
        self.assertLess(low_latency["p95_latency"], balanced["p95_latency"])
        self.assertGreater(comp["latency_improvement_pct"], 15.0)

        # Request and cost safety
        self.assertGreater(low_latency["stt_requests"], 0)
        self.assertEqual(low_latency["gemini_requests"], balanced["gemini_requests"])
        self.assertAlmostEqual(
            balanced["whisper_estimated_cost_usd"],
            low_latency["whisper_estimated_cost_usd"],
            places=3,
        )


if __name__ == "__main__":
    unittest.main()
