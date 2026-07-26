import unittest

import pandas as pd

from app import compute_recommendation


class PortfolioTrackerTests(unittest.TestCase):
    def test_compute_recommendation_handles_missing_close_column(self):
        info = {}
        hist = pd.DataFrame({"Open": [100 + i for i in range(25)]})

        result = compute_recommendation(info, hist)

        self.assertEqual(result["recommendation"], "N/A")
        self.assertEqual(result["n_signals"], 0)


if __name__ == "__main__":
    unittest.main()
