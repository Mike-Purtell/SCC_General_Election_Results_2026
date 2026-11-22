import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import app
import polars as pl
from shapely.geometry import Polygon
from shapely.wkb import dumps as dump_wkb


class SelectionTests(unittest.TestCase):
    def test_candidate_controls_precede_contest_and_are_stacked(self):
        controls = app.app.layout.children[2].children
        candidate_column, contest_column = controls[:2]
        self.assertEqual(candidate_column.children[1].id, "map-choice")
        response_container = candidate_column.children[2]
        self.assertEqual(response_container.id, "contest-response-container")
        self.assertEqual(response_container.children[1].id, "contest-response")
        self.assertEqual(contest_column.children[1].id, "contest-selector")

    def test_only_duplicate_names_include_the_contest(self):
        contests = {"1": "First (Vote For 1)", "2": "Second (Vote For 1)"}
        choices = {"1": (("Alice", "Carol"), ()), "2": (("Bob", "Carol"), ())}
        with patch.object(app, "CONTESTS", contests), patch.object(
            app, "load_contest", side_effect=choices.get
        ):
            options = app.all_choice_options()
        self.assertEqual(
            {option["label"]: json.loads(option["value"]) for option in options},
            {
                "Alice": ["1", "Alice"],
                "Bob": ["2", "Bob"],
                "Carol — First": ["1", "Carol"],
                "Carol — Second": ["2", "Carol"],
            },
        )

    def test_options_cover_named_candidates_only(self):
        options = app.choice_options
        values = [option["value"] for option in options]
        expected = {
            app.choice_value(page, choice)
            for page in app.CONTESTS
            for choice in app.load_contest(page)[0]
            if app.is_candidate(choice)
        }
        self.assertEqual(set(values), expected)
        self.assertEqual(len(values), len(expected))
        for choice in ("Yes", "No", "Write-in", "Voter Turnout"):
            self.assertFalse(app.is_candidate(choice))
            self.assertNotIn(choice, [json.loads(value)[1] for value in values])

    def test_selecting_candidate_and_contest_updates_the_other_selector(self):
        page = next(
            page for page in app.CONTESTS
            if page != app.default_page
            and len([c for c in app.load_contest(page)[0] if app.is_candidate(c)]) > 1
        )
        choices = app.load_contest(page)[0]
        selected = app.choice_value(page, next(c for c in choices[1:] if app.is_candidate(c)))
        initial = app.choice_value(app.default_page, app.default_choices[0])
        client = app.app.server.test_client()
        outputs = [
            {"id": "contest-selector", "property": "value"},
            {"id": "map-choice", "property": "value"},
            {"id": "contest-response", "property": "options"},
            {"id": "contest-response", "property": "value"},
            {"id": "contest-response-container", "property": "style"},
        ]
        callback = ".." + "...".join(f"{o['id']}.{o['property']}" for o in outputs) + ".."

        def update(contest, candidate, response_value, changed):
            response = client.post(
                "/_dash-update-component",
                json={
                    "output": callback,
                    "outputs": outputs,
                    "inputs": [
                        {"id": "contest-selector", "property": "value", "value": contest},
                        {"id": "map-choice", "property": "value", "value": candidate},
                        {"id": "contest-response", "property": "value", "value": response_value},
                    ],
                    "state": [],
                    "changedPropIds": [changed],
                },
            )
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            return response.get_json()["response"]

        result = update(app.default_page, selected, None, "map-choice.value")
        self.assertEqual(result["contest-selector"]["value"], page)
        self.assertEqual(result["contest-response"]["value"], json.loads(selected)[1])
        self.assertEqual(
            [option["value"] for option in result["contest-response"]["options"]],
            list(choices),
        )
        result = update(page, initial, None, "contest-selector.value")
        self.assertEqual(result["map-choice"]["value"], app.choice_value(page, choices[0]))
        self.assertEqual(result["contest-response"]["value"], choices[0])
        result = update(page, selected, choices[0], "contest-response.value")
        self.assertEqual(result["map-choice"]["value"], app.choice_value(page, choices[0]))

        measure = next(
            page for page in app.CONTESTS if app.load_contest(page)[0] == ("Yes", "No")
        )
        result = update(measure, initial, None, "contest-selector.value")
        self.assertIsNone(result["map-choice"]["value"])
        self.assertEqual(result["contest-response"]["value"], "Yes")
        self.assertEqual(
            result["contest-response"]["options"],
            [{"label": "Yes", "value": "Yes"}, {"label": "No", "value": "No"}],
        )
        result = update(measure, None, "No", "contest-response.value")
        self.assertIsNone(result["map-choice"]["value"])
        mayor_page = next(
            page for page in app.CONTESTS
            if "Kathy Watanabe" in app.load_contest(page)[0]
        )
        result = update(
            app.default_page,
            app.choice_value(mayor_page, "Kathy Watanabe"),
            None,
            "map-choice.value",
        )
        self.assertEqual(result["contest-selector"]["value"], mayor_page)
        self.assertEqual(result["contest-response"]["value"], "Kathy Watanabe")
        choices = {option["value"] for option in result["contest-response"]["options"]}
        self.assertTrue(
            {"Kathy Watanabe", "Karen Hardy", "David Kertes", "Kevin Park"} <= choices
        )

    def test_dashboard_receives_selected_choice_name(self):
        page = app.default_page
        choice = app.default_choices[-1]
        with patch.object(app, "build_dashboard", return_value="result") as build:
            result = app.update_dashboard(page, app.choice_value(page, choice), None, "white-bg", None)
        self.assertEqual(result, "result")
        build.assert_called_once_with(page, choice, "white-bg", None)
        with patch.object(app, "build_dashboard", return_value="result") as build:
            app.update_dashboard(page, None, "No", "white-bg", None)
        build.assert_called_once_with(page, "No", "white-bg", None)


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


class PrecinctGeometryCacheTests(unittest.TestCase):
    def test_exact_cache_is_reused_and_invalidated_when_source_changes(self):
        cached_geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": "0002001",
                    "properties": {"Precinct": "0002001"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[1, 2], [3, 2], [3, 4], [1, 2]]],
                    },
                }
            ],
        }
        with TemporaryDirectory() as directory:
            geodatabase = Path(directory) / "precincts.gdb"
            geodatabase.mkdir()
            source_file = geodatabase / "layer.gdbtable"
            source_file.write_text("source", encoding="utf-8")
            cache_path = Path(directory) / "precincts.json"
            with patch.object(app, "PRECINCT_GEODATABASE", geodatabase), patch.object(
                app, "PRECINCT_GEOJSON_CACHE", cache_path
            ):
                cache_path.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "source_signature": app.precinct_geodatabase_signature(),
                            "geojson": cached_geojson,
                            "bounds": {"0002001": [1, 2, 3, 4]},
                        }
                    ),
                    encoding="utf-8",
                )
                with patch.object(
                    app,
                    "read_geodatabase_layer",
                    side_effect=AssertionError("cache should be used"),
                ):
                    geojson, bounds = app.load_precinct_geojson()
                self.assertEqual(geojson, cached_geojson)
                self.assertEqual(bounds, {"0002001": (1, 2, 3, 4)})

                source_file.write_text("updated source", encoding="utf-8")
                polygon = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
                with patch.object(
                    app,
                    "read_geodatabase_layer",
                    return_value=(
                        {"crs": "EPSG:4326"},
                        None,
                        [dump_wkb(polygon)],
                        [[2001]],
                    ),
                ) as read_layer:
                    rebuilt_geojson, rebuilt_bounds = app.load_precinct_geojson()
                read_layer.assert_called_once()
                self.assertNotEqual(rebuilt_geojson, cached_geojson)
                self.assertEqual(rebuilt_bounds["0002001"], (0.0, 0.0, 1.0, 1.0))


class WeightedRegressionTests(unittest.TestCase):
    def test_weighted_fit_matches_vote_expansion(self):
        slope, intercept, correlation = app.weighted_linear_regression(
            (1, 3, 7),
            (2, 5, 4),
            (2, 3, 1),
        )
        self.assertAlmostEqual(slope, 1 / 3)
        self.assertAlmostEqual(intercept, 17 / 6)
        self.assertAlmostEqual(correlation, 8 / (260 ** 0.5))

    def test_constant_y_has_zero_correlation(self):
        slope, intercept, correlation = app.weighted_linear_regression(
            (1, 3),
            (2, 2),
            (1, 4),
        )
        self.assertEqual(slope, 0)
        self.assertEqual(intercept, 2)
        self.assertEqual(correlation, 0)


class PartisanshipCacheTests(unittest.TestCase):
    def test_scatter_and_heatmap_share_partisanship_data(self):
        page = "example"
        governor_page = "governor"
        contest_rows = (
            {
                "Precinct": "0002001",
                "Contest Total Votes": 10,
                "Choice": 4,
                "Ballots Cast": 5,
                "Turnout": 50,
            },
        )
        governor_rows = (
            {
                "Precinct": "0002001",
                "Contest Total Votes": 10,
                app.BECERRA: 5,
            },
        )

        def contest_data(requested_page, include_zero_votes=False):
            if requested_page == governor_page:
                return (app.BECERRA,), governor_rows
            return ("Choice",), contest_rows

        app.load_partisanship_data.cache_clear()
        try:
            with patch.object(
                app, "CONTESTS", {page: "Example", governor_page: app.GOVERNOR_CONTEST}
            ), patch.object(
                app, "CONTEST_PAGES", {app.GOVERNOR_CONTEST: governor_page}
            ), patch.object(
                app, "TURNOUT_BY_PRECINCT", {"0002001": {"Ballots Cast": 5}}
            ), patch.object(app, "load_contest", side_effect=contest_data) as load:
                first = app.load_partisanship_data(page, "Choice", app.BECERRA)
                second = app.load_partisanship_data(page, "Choice", app.BECERRA)
            self.assertEqual(first, second)
            self.assertEqual(load.call_count, 2)
        finally:
            app.load_partisanship_data.cache_clear()


class DashboardTests(unittest.TestCase):
    def test_same_contest_map_update_omits_static_geometry(self):
        selected_choice = app.default_choices[-1]
        full_figure, _, _, _ = app.build_dashboard(
            app.default_page,
            selected_choice,
            map_style="white-bg",
        )
        figure, _, _, _ = app.build_dashboard(
            app.default_page,
            selected_choice,
            map_style="white-bg",
            current_figure={"layout": {"uirevision": app.default_page}},
        )
        operations = figure.to_plotly_json()["operations"]
        paths = {tuple(operation["location"]) for operation in operations}
        updates = {
            tuple(operation["location"]): operation["params"]["value"]
            for operation in operations
        }
        self.assertIn(("data", 0, "z"), paths)
        self.assertIn(("data", 0, "customdata"), paths)
        self.assertIn(("layout", "map", "style"), paths)
        self.assertNotIn(("data", 0, "geojson"), paths)
        self.assertEqual(updates[("data", 0, "z")], list(full_figure.data[0].z))
        self.assertEqual(
            updates[("data", 0, "customdata")],
            [list(row) for row in full_figure.data[0].customdata],
        )
        self.assertEqual(
            updates[("layout", "title", "text")],
            full_figure.layout.title.text,
        )
        self.assertEqual(
            updates[("layout", "map", "style")],
            full_figure.layout.map.style,
        )

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
