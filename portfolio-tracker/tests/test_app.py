import base64
import io
import os
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import app as portfolio_app
from app import compute_recommendation


class PortfolioTrackerTests(unittest.TestCase):
    def test_compute_recommendation_handles_missing_close_column(self):
        info = {}
        hist = pd.DataFrame({"Open": [100 + i for i in range(25)]})

        result = compute_recommendation(info, hist)

        self.assertEqual(result["recommendation"], "N/A")
        self.assertEqual(result["n_signals"], 0)

    def test_add_route_persists_manually_entered_watch_ticker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "portfolio.db")
            with patch.object(portfolio_app, "DB_PATH", db_path):
                portfolio_app._sqlite_init()
                with patch.object(portfolio_app, "fetch_market_data", return_value=None):
                    with portfolio_app.app.test_client() as client:
                        with client.session_transaction() as session:
                            session["ok"] = True
                        response = client.post(
                            "/add",
                            data={"ticker": "AAPL", "position_type": "watch"},
                        )

                self.assertEqual(response.status_code, 302)
                positions = portfolio_app.get_positions()
                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]["ticker"], "AAPL")

    def test_migration_export_can_be_imported_into_empty_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = os.path.join(temp_dir, "source.db")
            target_path = os.path.join(temp_dir, "target.db")
            auth = "Basic " + base64.b64encode(
                b"portfolio-migration:test-migration-token"
            ).decode("ascii")
            with (
                patch.dict(os.environ, {"PORTFOLIO_MIGRATION_TOKEN": "test-migration-token"}),
                patch.object(portfolio_app, "DB_PATH", source_path),
            ):
                portfolio_app._sqlite_init()
                portfolio_app.add_position_doc({
                    "ticker": "AAPL",
                    "name": "Apple",
                    "position_type": "own",
                    "shares": 2.5,
                    "buy_price": 150,
                    "buy_currency": "USD",
                    "currency": "USD",
                    "quote_type": "EQUITY",
                })

                with portfolio_app.app.test_client() as client:
                    exported = client.get(
                        "/migration/export",
                        headers={"Authorization": auth},
                    )
                    self.assertEqual(exported.status_code, 200)

                    with patch.object(portfolio_app, "DB_PATH", target_path):
                        portfolio_app._sqlite_init()
                        imported = client.post(
                            "/migration/import",
                            headers={"Authorization": auth},
                            data={"file": (io.BytesIO(exported.data), "portfolio-export.json")},
                        )
                        self.assertEqual(imported.status_code, 200)
                        self.assertEqual(imported.json["imported"], 1)
                        self.assertEqual(portfolio_app.get_positions()[0]["ticker"], "AAPL")

                        rejected = client.post(
                            "/migration/import",
                            headers={"Authorization": auth},
                            data={"file": (io.BytesIO(exported.data), "portfolio-export.json")},
                        )
                        self.assertEqual(rejected.status_code, 409)
                        self.assertEqual(len(portfolio_app.get_positions()), 1)


if __name__ == "__main__":
    unittest.main()
