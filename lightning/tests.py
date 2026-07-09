import datetime as dt

from django.test import SimpleTestCase

from lightning.city_stats import city_aggregate_rows, city_for_strike


class CityStatsTests(SimpleTestCase):
    def test_city_for_strike_matches_top_city_within_radius(self):
        city = city_for_strike(48.8566, 2.3522, "FR")

        self.assertIsNotNone(city)
        self.assertEqual(city["name"], "Paris")

    def test_city_for_strike_ignores_points_outside_radius(self):
        city = city_for_strike(47.0, 2.0, "FR")

        self.assertIsNone(city)

    def test_city_aggregate_rows_builds_day_month_year_counts(self):
        rows = city_aggregate_rows([
            {
                "lat": 48.8566,
                "lon": 2.3522,
                "country": "FR",
                "timestamp": dt.datetime(2026, 7, 9, 12, 30, tzinfo=dt.timezone.utc),
                "quality": "good",
            },
            {
                "lat": 48.86,
                "lon": 2.35,
                "country": "FR",
                "timestamp": dt.datetime(2026, 7, 10, 1, 5, tzinfo=dt.timezone.utc),
                "quality": "bad",
            },
        ])

        by_period = {(row["period_kind"], row["period_start"]): row for row in rows}

        self.assertEqual(by_period[("day", dt.date(2026, 7, 9))]["count"], 1)
        self.assertEqual(by_period[("day", dt.date(2026, 7, 10))]["count"], 1)
        self.assertEqual(by_period[("month", dt.date(2026, 7, 1))]["count"], 2)
        self.assertEqual(by_period[("year", dt.date(2026, 1, 1))]["count"], 2)
