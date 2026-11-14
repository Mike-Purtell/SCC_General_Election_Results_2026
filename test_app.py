import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import app
import polars as pl


class PrecinctCrossReferenceTests(unittest.TestCase):
    def test_parquet_cache_is_created_and_reused(self):
        frame = pl.DataFrame(
            {"Regular Precinct": ["2014"], "Voting Precinct": ["2001"]}
        )
        with TemporaryDirectory() as directory:
            parquet_path = Path(directory) / "cross_reference.parquet"
            with patch.object(app, "read_precinct_cross_reference", return_value=frame) as read_pdf:
                first = app.load_precinct_cross_reference("source.pdf", parquet_path)
                self.assertEqual(first.to_dicts(), frame.to_dicts())
                read_pdf.assert_called_once_with("source.pdf")
                self.assertTrue(parquet_path.is_file())
                read_pdf.reset_mock()
                cached = app.load_precinct_cross_reference("source.pdf", parquet_path)
                read_pdf.assert_not_called()
                self.assertEqual(cached.to_dicts(), frame.to_dicts())
                self.assertEqual(cached.schema, frame.schema)

    def test_cross_reference_tracks_contest_precincts(self):
        frame = pl.DataFrame(
            {
                "Regular Precinct": ["2014", "2001", "2002"],
                "Voting Precinct": ["2001", "2001", "2002"],
            }
        )
        contests = {
            "first": ((), ({"Precinct": "0002001"},)),
            "second": ((), ({"Precinct": "0002002"},)),
            "empty": ((), ()),
        }
        with patch.object(app, "precinct_cross_reference", frame), patch.object(
            app, "load_contest", side_effect=contests.get
        ):
            self.assertEqual(
                app.update_precinct_cross_reference("first"),
                [
                    {"Regular Precinct": "2001", "Voting Precinct": "2001"},
                    {"Regular Precinct": "2014", "Voting Precinct": "2001"},
                ],
            )
            self.assertEqual(
                app.update_precinct_cross_reference("second"),
                [{"Regular Precinct": "2002", "Voting Precinct": "2002"}],
            )
            self.assertEqual(app.update_precinct_cross_reference("empty"), [])

    def test_pdf_cross_reference_is_complete(self):
        frame = app.read_precinct_cross_reference(app.PRECINCT_CROSS_REFERENCE)
        self.assertEqual(frame.columns, ["Regular Precinct", "Voting Precinct"])
        self.assertEqual(frame.height, 2560)
        self.assertEqual(frame["Regular Precinct"].n_unique(), 2560)
        self.assertEqual(frame["Voting Precinct"].n_unique(), 708)
        mappings = dict(frame.iter_rows())
        self.assertEqual(mappings["2014"], "2001")
        self.assertEqual(mappings["2095"], "2048")
        self.assertEqual(mappings["9272"], "9268")

    def test_precinct_group_can_continue_on_next_page(self):
        pages = [
            SimpleNamespace(extract_text=lambda: " 10002001 - PCT 2001  0002001 PCT 2001"),
            SimpleNamespace(extract_text=lambda: " 1 0002014 PCT 2014\nRegular Pcts: 2 Total Voting Pcts: 1"),
        ]
        with patch.object(app, "PdfReader", return_value=SimpleNamespace(pages=pages)):
            frame = app.read_precinct_cross_reference("example.pdf")
        self.assertEqual(frame.rows(), [("2001", "2001"), ("2014", "2001")])

    def test_rejects_malformed_rows_and_incomplete_totals(self):
        for text in (
            " 1 0002014 PCT 2014\nRegular Pcts: 1 Total Voting Pcts: 1",
            " 10002001 - PCT 2001  0002001 PCT 9999\nRegular Pcts: 1 Total Voting Pcts: 1",
            " 10002001 - PCT 2001  0002001 PCT 2001\nRegular Pcts: 2 Total Voting Pcts: 1",
        ):
            with self.subTest(text=text):
                pages = [SimpleNamespace(extract_text=lambda: text)]
                with patch.object(app, "PdfReader", return_value=SimpleNamespace(pages=pages)):
                    with self.assertRaises(ValueError):
                        app.read_precinct_cross_reference("example.pdf")


class DashboardTests(unittest.TestCase):
    def test_results_columns_are_equal_and_80_percent_width(self):
        _, _, column_defs, _ = app.build_dashboard(
            app.default_page, app.default_choices[0]
        )
        self.assertTrue(all(definition["width"] == 200 for definition in column_defs))
        results_grid = next(
            component
            for component in app.app.layout._traverse_ids()
            if component.id == "results-grid"
        )
        self.assertEqual(results_grid.style["width"], "80%")
        self.assertLess(results_grid.defaultColDef["minWidth"], 120)

    def test_long_column_headers_enable_wrapping(self):
        _, _, column_defs, _ = app.build_dashboard(
            app.default_page, app.default_choices[0]
        )
        definitions = {definition["field"]: definition for definition in column_defs}
        expected_headers = {
            "Precinct": "Consolidated Voting Precinct",
            "Registered Voters": "Registered Voters",
            "Contest Total Votes": "Contest Total Votes",
            "Ballots Cast": "Ballots Cast",
        }
        for column, header_name in expected_headers.items():
            self.assertEqual(definitions[column]["headerName"], header_name)
            self.assertTrue(definitions[column]["wrapHeaderText"])
            self.assertTrue(definitions[column]["autoHeaderHeight"])


class PrecinctDensityTests(unittest.TestCase):
    def test_density_scale_starts_at_zero_and_spans_bin_counts(self):
        data = ("Example", "Choice", [10, 10, 20], [1, 1, 2], [], [], [])
        with patch.object(app, "load_partisanship_data", return_value=data):
            figure = app.build_partisanship_heatmap("page", "Choice", app.BECERRA)
        heatmap = figure.data[0]
        self.assertEqual(heatmap.type, "histogram2d")
        self.assertEqual(heatmap.zmin, 0)
        self.assertEqual(heatmap.zmax, 2)
        self.assertEqual(heatmap.colorscale[0], (0, "#ffffff"))
        self.assertEqual(heatmap.colorscale[-1], (1, "#008000"))

    def test_heatmap_zooms_without_removing_outliers(self):
        x_values = [-1000, *range(101), 1000]
        y_values = [-2000, *range(101), 2000]
        data = (
            "Example",
            "Choice",
            x_values,
            y_values,
            [],
            [],
            [],
        )
        with patch.object(app, "load_partisanship_data", return_value=data):
            figure = app.build_partisanship_heatmap("page", "Choice", app.BECERRA)

        self.assertAlmostEqual(figure.layout.xaxis.range[0], 4.1)
        self.assertAlmostEqual(figure.layout.xaxis.range[1], 95.9)
        self.assertAlmostEqual(figure.layout.yaxis.range[0], 4.1)
        self.assertAlmostEqual(figure.layout.yaxis.range[1], 95.9)
        self.assertEqual(list(figure.data[0].x), x_values)
        self.assertEqual(list(figure.data[0].y), y_values)

    def test_empty_and_constant_ranges(self):
        self.assertEqual(app.density_axis_range([]), [0, 100])
        self.assertEqual(app.density_axis_range([10, 10, 10]), [9, 11])


class PrecinctDistributionTests(unittest.TestCase):
    def test_vote_share_includes_zero_and_uses_weighted_county_share(self):
        rows = (
            {"Contest Total Votes": 100, "Choice": 50},
            {"Contest Total Votes": 900, "Choice": 0},
        )
        with patch.object(app, "load_contest", return_value=(("Choice",), rows)):
            figure = app.build_precinct_distribution(app.default_page, "stale selection")
        self.assertEqual(list(figure.data[0].x), [50, 0])
        self.assertEqual(figure.data[0].xbins.size, 5)
        self.assertEqual(figure.layout.bargap, 0)
        self.assertEqual(figure.data[0].marker.line.color, "#808080")
        self.assertEqual(figure.data[0].marker.line.width, 1)
        self.assertEqual(figure.layout.shapes[0].x0, 5)
        self.assertIn("Choice", figure.layout.xaxis.title.text)

    def test_turnout_excludes_undefined_and_keeps_above_100(self):
        page = next(page for page, contest in app.CONTESTS.items() if contest == app.REGISTERED_VOTERS)
        rows = (
            {"Registered Voters": 10, "Turnout": 200.0},
            {"Registered Voters": 0, "Turnout": 0.0},
        )
        with patch.object(app, "load_contest", return_value=(("Voter Turnout",), rows)), \
                patch.object(app, "COUNTY_REGISTERED", 100), \
                patch.object(app, "COUNTY_TURNOUT", 57.5):
            figure = app.build_precinct_distribution(page, "Voter Turnout")
        self.assertEqual(list(figure.data[0].x), [200.0])
        self.assertEqual(figure.layout.shapes[0].x0, 57.5)

    def test_empty_contest_shows_message_without_reference_line(self):
        with patch.object(app, "load_contest", return_value=(("Choice",), ())):
            figure = app.build_precinct_distribution(app.default_page, "Choice")
        self.assertEqual(len(figure.data), 0)
        self.assertEqual(len(figure.layout.shapes), 0)
        self.assertIn("No precinct data", figure.layout.annotations[0].text)


if __name__ == "__main__":
    unittest.main()
