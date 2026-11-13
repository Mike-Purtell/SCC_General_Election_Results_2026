import unittest
from unittest.mock import patch

import app


class ContourAxisRangeTests(unittest.TestCase):
    def test_contour_zooms_without_removing_outliers(self):
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
            figure = app.build_partisanship_contour("page", "Choice", app.BECERRA)

        self.assertAlmostEqual(figure.layout.xaxis.range[0], 4.1)
        self.assertAlmostEqual(figure.layout.xaxis.range[1], 95.9)
        self.assertAlmostEqual(figure.layout.yaxis.range[0], 4.1)
        self.assertAlmostEqual(figure.layout.yaxis.range[1], 95.9)
        self.assertEqual(list(figure.data[0].x), x_values)
        self.assertEqual(list(figure.data[0].y), y_values)

    def test_empty_and_constant_ranges(self):
        self.assertEqual(app.contour_axis_range([]), [0, 100])
        self.assertEqual(app.contour_axis_range([10, 10, 10]), [9, 11])


if __name__ == "__main__":
    unittest.main()
