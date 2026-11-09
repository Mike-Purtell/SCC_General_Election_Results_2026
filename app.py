from datetime import datetime
from functools import lru_cache
from math import asinh, atan, degrees, isfinite, log2, pi, radians, sinh, tan
from pathlib import Path
import re

import dash
import dash_ag_grid as dag
from dash import Input, Output, dcc, html
import plotly.express as px
import plotly.graph_objects as go
import polars as pl
import shapefile
from pyproj import Transformer
from scipy.stats import linregress
from shapely.geometry import shape


ROOT = Path(__file__).resolve().parent
SOURCE_DATA = ROOT / "detail_2026_11_08.xlsx"
PRECINCT_SHAPEFILE = ROOT / "Precinct_Boundaries" / "Precinct_Boundaries.shp"
REGISTERED_VOTERS = "Registered Voters"
GOVERNOR_CONTEST = "Governor (Vote For 1)"
BECERRA = "Xavier Becerra"
UNPROCESSED_BALLOTS = 186_550
UNPROCESSED_BALLOTS_UPDATED = "November 8, 2026 at 5:00 p.m."
UNPROCESSED_BALLOTS_URL = (
    "https://vote.santaclaracounty.gov/elections/unprocessed-ballot-estimates"
)
MAP_STYLES = (
    "open-street-map",
    "white-bg",
    "carto-voyager",
    "carto-voyager-nolabels",
    "carto-positron",
    "carto-positron-nolabels",
    "carto-darkmatter",
    "carto-darkmatter-nolabels",
    "satellite",
    "satellite-streets",
    "basic",
    "streets",
    "outdoors",
    "light",
    "dark",
)


def source_results_date(source_path):
    match = re.search(r"(?:^|_)(\d{4}_\d{2}_\d{2})$", source_path.stem)
    if match is None:
        raise ValueError(
            f"Source filename must end with a date in YYYY_MM_DD format: {source_path.name}"
        )
    return datetime.strptime(match.group(1), "%Y_%m_%d").date()


results_date = source_results_date(SOURCE_DATA)
results_date_label = f"{results_date:%B} {results_date.day}, {results_date.year}"


def precinct_id(value):
    return str(value).strip().zfill(7)


def display_precinct_id(value):
    return value[3:] if len(value) == 7 and value.startswith("000") else value


def display_contest_name(contest):
    contest = re.sub(
        r"\s*\(Vote\s+For\s+[01]\)\s*$", "", contest, flags=re.IGNORECASE
    )
    return re.sub(r"^State Proposition\b", "CA Prop", contest, flags=re.IGNORECASE)


def read_turnout():
    turnout = (
        pl.read_excel(
            SOURCE_DATA,
            sheet_name=REGISTERED_VOTERS,
            read_options={"header_row": 0},
        )
        .filter(pl.col("Precinct") != "Total:")
        .with_columns(pl.col("Precinct").str.pad_start(7, "0"))
    )
    turnout_rows = turnout.to_dicts()
    turnout_by_precinct = {
        row["Precinct"]: {
            "Registered Voters": row["Registered Voters"] or 0,
            "Ballots Cast": row["Ballots Cast"] or 0,
            "Turnout Percent": float(row["Voter Turnout"].rstrip("%")),
        }
        for row in turnout_rows
    }
    registered = sum(row["Registered Voters"] for row in turnout_rows)
    ballots = sum(row["Ballots Cast"] for row in turnout_rows)
    county_turnout = ballots / registered * 100 if registered else 0
    return turnout_by_precinct, registered, ballots, county_turnout


TURNOUT_BY_PRECINCT, COUNTY_REGISTERED, COUNTY_BALLOTS, COUNTY_TURNOUT = read_turnout()

contest_table = pl.read_excel(
    SOURCE_DATA,
    sheet_name="Table of Contents",
    read_options={"header_row": 3},
)
CONTESTS = {
    str(row["Page"]): row["Contest"]
    for row in contest_table.iter_rows(named=True)
}
CONTEST_PAGES = {
    contest: page for page, contest in CONTESTS.items()
}


def project_coordinates(coordinates, transformer):
    if (
        isinstance(coordinates, (list, tuple))
        and len(coordinates) >= 2
        and isinstance(coordinates[0], (int, float))
        and isinstance(coordinates[1], (int, float))
    ):
        longitude, latitude = transformer.transform(
            coordinates[0], coordinates[1]
        )
        return [longitude, latitude, *coordinates[2:]]
    return [project_coordinates(value, transformer) for value in coordinates]


def load_precinct_geojson():
    transformer = Transformer.from_crs("EPSG:2227", "EPSG:4326", always_xy=True)
    reader = shapefile.Reader(str(PRECINCT_SHAPEFILE), encoding="utf-8")
    field_names = [field[0] for field in reader.fields[1:]]
    features = []

    for shape_record in reader.iterShapeRecords():
        geometry = shape_record.shape.__geo_interface__
        precinct = precinct_id(
            dict(zip(field_names, shape_record.record))["Precinct"]
        )
        polygon = shape(geometry)
        if polygon.geom_type == "MultiPolygon":
            polygon = max(polygon.geoms, key=lambda part: part.area)
        point = polygon.representative_point()
        longitude, latitude = transformer.transform(point.x, point.y)
        features.append(
            {
                "type": "Feature",
                "id": precinct,
                "properties": {
                    "Precinct": precinct,
                    "label_lon": longitude,
                    "label_lat": latitude,
                },
                "geometry": {
                    "type": geometry["type"],
                    "coordinates": project_coordinates(
                        geometry["coordinates"], transformer
                    ),
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}


PRECINCT_GEOJSON = load_precinct_geojson()
PRECINCT_IDS = {
    feature["properties"]["Precinct"] for feature in PRECINCT_GEOJSON["features"]
}
PRECINCT_FEATURES = {
    feature["properties"]["Precinct"]: feature
    for feature in PRECINCT_GEOJSON["features"]
}
PRECINCT_LABELS = {
    feature["properties"]["Precinct"]: (
        feature["properties"]["label_lon"],
        feature["properties"]["label_lat"],
    )
    for feature in PRECINCT_GEOJSON["features"]
}
PRECINCT_BOUNDS = {
    feature["properties"]["Precinct"]: shape(feature["geometry"]).bounds
    for feature in PRECINCT_GEOJSON["features"]
}


def precinct_map_view(precincts):
    bounds = [PRECINCT_BOUNDS[precinct] for precinct in precincts if precinct in PRECINCT_BOUNDS]
    if not bounds:
        return {"lat": 37.25, "lon": -121.93}, 8
    west = min(bound[0] for bound in bounds)
    south = min(bound[1] for bound in bounds)
    east = max(bound[2] for bound in bounds)
    north = max(bound[3] for bound in bounds)
    south_mercator = asinh(tan(radians(south)))
    north_mercator = asinh(tan(radians(north)))
    center = {
        "lon": (west + east) / 2,
        "lat": degrees(atan(sinh((south_mercator + north_mercator) / 2))),
    }
    width_fraction = (east - west) / 360
    height_fraction = (north_mercator - south_mercator) / (2 * pi)
    zoom = min(
        log2(400 / (512 * width_fraction)) if width_fraction else 20,
        log2(480 / (512 * height_fraction)) if height_fraction else 20,
        20,
    )
    return center, max(0, zoom)


@lru_cache(maxsize=32)
def load_contest(page, include_zero_votes=False):
    contest = CONTESTS[page]
    sheet_name = REGISTERED_VOTERS if contest == REGISTERED_VOTERS else page
    header_row = 0 if contest == REGISTERED_VOTERS else 2
    frame = (
        pl.read_excel(
            SOURCE_DATA,
            sheet_name=sheet_name,
            read_options={"header_row": header_row},
        )
        .filter(pl.col("Precinct") != "Total:")
        .with_columns(pl.col("Precinct").str.pad_start(7, "0"))
    )

    if contest == REGISTERED_VOTERS:
        rows = []
        for row in frame.to_dicts():
            turnout = TURNOUT_BY_PRECINCT.get(row["Precinct"], {})
            rows.append(
                {
                    "Precinct": row["Precinct"],
                    "Registered Voters": row["Registered Voters"] or 0,
                    "Ballots Cast": row["Ballots Cast"] or 0,
                    "Turnout": turnout.get("Turnout Percent", 0),
                }
            )
        return ("Voter Turnout",), tuple(rows)

    if not include_zero_votes:
        frame = frame.filter(pl.col("Total") != 0)
    choice_headers = pl.read_excel(
        SOURCE_DATA,
        sheet_name=sheet_name,
        read_options={"header_row": header_row - 1},
    ).columns
    choice_names = {}
    for index, column in enumerate(frame.columns):
        if column == "Total Votes" or column.startswith("Total Votes_"):
            choice = choice_headers[index - 2]
            if choice.upper() in {"YES", "NO"}:
                choice = choice.title()
            choice_names[column] = choice

    frame = frame.drop(
        [
            column
            for column in frame.columns
            if column in {"In Person", "Vote By Mail"}
            or column.startswith(("In Person_", "Vote By Mail_"))
        ]
    ).rename(choice_names)
    choices = tuple(choice_names.values())
    rows = []
    for row in frame.to_dicts():
        turnout = TURNOUT_BY_PRECINCT.get(row["Precinct"], {})
        row["Contest Total Votes"] = row.pop("Total", 0) or 0
        row["Registered Voters"] = row.get("Registered Voters") or 0
        row["Ballots Cast"] = turnout.get("Ballots Cast", 0)
        row["Turnout"] = turnout.get("Turnout Percent", 0)
        rows.append(row)
    return choices, tuple(rows)


def format_count(value):
    return f"{value:,.0f}"


def metric_card(label, value):
    return html.Div(
        [
            html.P(label, style={"margin": "0 0 8px", "color": "#64748b"}),
            html.H3(value, style={"margin": 0, "color": "#0f172a"}),
        ],
        style={
            "background": "white",
            "border": "1px solid #e2e8f0",
            "borderRadius": "10px",
            "padding": "16px",
            "minWidth": "150px",
        },
    )


def build_dashboard(page, selected_choice, map_style="open-street-map"):
    if map_style not in MAP_STYLES:
        raise ValueError(f"Unsupported map style: {map_style}")
    contest = CONTESTS[page]
    choices, rows = load_contest(page)
    if selected_choice not in choices:
        selected_choice = choices[0]

    map_rows = []
    grid_rows = []
    if contest == REGISTERED_VOTERS:
        for row in rows:
            map_rows.append(
                {
                    "Precinct": row["Precinct"],
                    "Choice": "Voter Turnout",
                    "Choice Votes": row["Ballots Cast"],
                    "Vote Percentage": row["Turnout"],
                    "Contest Total Votes": row["Ballots Cast"],
                    "Turnout": row["Turnout"],
                    "Map Value": row["Turnout"],
                    "Registered Voters": row["Registered Voters"],
                }
            )
            grid_rows.append({**row, "Precinct": display_precinct_id(row["Precinct"])})
        cards = [
            metric_card("Registered voters", format_count(COUNTY_REGISTERED)),
            metric_card("Ballots cast", format_count(COUNTY_BALLOTS)),
            metric_card("Voter turnout", f"{COUNTY_TURNOUT:.2f}%"),
        ]
    else:
        choice_totals = {choice: 0 for choice in choices}
        contest_total = 0
        for row in rows:
            total_votes = row["Contest Total Votes"]
            contest_total += total_votes
            for choice in choices:
                votes = row.get(choice) or 0
                choice_totals[choice] += votes
            selected_votes = row.get(selected_choice) or 0
            share = selected_votes / total_votes * 100 if total_votes else 0
            map_rows.append(
                {
                    "Precinct": row["Precinct"],
                    "Choice": selected_choice,
                    "Choice Votes": selected_votes,
                    "Vote Percentage": share,
                    "Contest Total Votes": total_votes,
                    "Turnout": row["Turnout"],
                    "Map Value": share,
                    "Registered Voters": row["Registered Voters"],
                }
            )
            grid_rows.append({**row, "Precinct": display_precinct_id(row["Precinct"])})

        cards = [
            metric_card(
                choice,
                f"{format_count(total)} "
                f"({total / contest_total * 100:.2f}%)"
                if contest_total
                else f"{format_count(total)} (N/A)",
            )
            for choice, total in choice_totals.items()
        ]
        cards.extend(
            [
                metric_card("Contest votes", format_count(contest_total)),
                metric_card("Voter turnout", f"{COUNTY_TURNOUT:.2f}%"),
            ]
        )

    displayed_values = pl.Series(
        "Map Value",
        [
            row["Map Value"]
            for row in map_rows
            if row["Precinct"] in PRECINCT_IDS
        ],
        dtype=pl.Float64,
    )
    color_range = (
        (
            displayed_values.quantile(0.1, interpolation="linear"),
            displayed_values.quantile(0.9, interpolation="linear"),
        )
        if not displayed_values.is_empty()
        else None
    )
    map_frame = pl.DataFrame(
        map_rows,
        schema={
            "Precinct": pl.String,
            "Choice": pl.String,
            "Choice Votes": pl.Int64,
            "Vote Percentage": pl.Float64,
            "Contest Total Votes": pl.Int64,
            "Turnout": pl.Float64,
            "Map Value": pl.Float64,
            "Registered Voters": pl.Int64,
        },
    ).with_columns(
        pl.when(pl.col("Precinct").str.contains(r"^000\d{4}$"))
        .then(pl.col("Precinct").str.slice(3))
        .otherwise(pl.col("Precinct"))
        .alias("Display Precinct")
    )
    map_precincts = [
        row["Precinct"] for row in map_rows if row["Precinct"] in PRECINCT_FEATURES
    ]
    contest_geojson = {
        "type": "FeatureCollection",
        "features": [PRECINCT_FEATURES[precinct] for precinct in map_precincts],
    }
    center, zoom = precinct_map_view(map_precincts)
    figure = px.choropleth_map(
        map_frame,
        geojson=contest_geojson,
        locations="Precinct",
        featureidkey="properties.Precinct",
        color="Map Value",
        range_color=color_range,
        color_continuous_scale="BuGn",
        opacity=0.5,
        map_style=map_style,
        center=center,
        zoom=zoom,
        custom_data=[
            "Choice",
            "Choice Votes",
            "Vote Percentage",
            "Contest Total Votes",
            "Turnout",
            "Display Precinct",
            "Registered Voters",
        ],
    )
    figure.update_traces(
        hovertemplate=(
            "<b>Precinct %{customdata[5]}</b><br>"
            "Ballots Cast: %{customdata[1]:,.0f}<br>"
            "Registered Voters: %{customdata[6]:,.0f}<br>"
            "Turnout: %{customdata[4]:.1f}%<extra></extra>"
            if contest == REGISTERED_VOTERS
            else (
                "<b>Precinct %{customdata[5]}</b><br>"
                "%{customdata[0]}: %{customdata[1]:,.0f} votes<br>"
                "Precinct votes: %{customdata[3]:,.0f}<br>"
                "Vote percentage: %{customdata[2]:.1f}%<br>"
                "Precinct turnout: %{customdata[4]:.1f}%"
                "<extra></extra>"
            )
        )
    )
    label_ids = [
        row["Precinct"] for row in map_rows if row["Precinct"] in PRECINCT_LABELS
    ]
    figure.add_trace(go.Scattermap(
        lon=[PRECINCT_LABELS[precinct][0] for precinct in label_ids],
        lat=[PRECINCT_LABELS[precinct][1] for precinct in label_ids],
        text=[display_precinct_id(precinct) for precinct in label_ids],
        mode="text",
        textfont={"size": 10, "color": "#111827"},
        hoverinfo="skip",
        showlegend=False,
    ))
    figure.update_layout(
        title=f"{display_contest_name(contest)} — {selected_choice}",
        margin={"l": 0, "r": 0, "t": 48, "b": 0},
        height=620,
        coloraxis_colorbar={
            "title": selected_choice
            if contest == REGISTERED_VOTERS
            else f"{selected_choice} share",
            "ticksuffix": "%",
        },
        paper_bgcolor="white",
        dragmode="pan",
        uirevision=page,
    )

    grid_columns = list(grid_rows[0]) if grid_rows else []
    column_defs = []
    for column in grid_columns:
        definition = {
            "field": column,
            "filter": True,
            "sortable": True,
            "resizable": True,
        }
        if (
            column != "Precinct"
            and grid_rows
            and isinstance(grid_rows[0].get(column), (int, float))
        ):
            definition["type"] = "numericColumn"
        column_defs.append(definition)
    return figure, cards, column_defs, grid_rows


def scatter_axis_range(values):
    if not values:
        return [0, 100]
    minimum = min(values)
    maximum = max(values)
    margin = (maximum - minimum) * 0.1
    if margin == 0:
        margin = max(abs(minimum) * 0.1, 1)
    return [minimum - margin, maximum + margin]


def build_partisanship_scatter(page, selected_choice, governor_candidate, color):
    contest = CONTESTS[page]
    choices, rows = load_contest(page)
    if selected_choice not in choices:
        selected_choice = choices[0]

    governor_choices, governor_rows = load_contest(
        CONTEST_PAGES[GOVERNOR_CONTEST], include_zero_votes=True
    )
    for candidate in {governor_candidate, BECERRA}:
        if candidate not in governor_choices:
            raise ValueError(f"{candidate} is missing from the governor contest")
    governor_by_precinct = {row["Precinct"]: row for row in governor_rows}

    vote_percentages = []
    governor_shares = []
    ballot_counts = []
    choice_counts = []
    hover_data = []
    for row in rows:
        governor = governor_by_precinct.get(row["Precinct"])
        turnout = TURNOUT_BY_PRECINCT.get(row["Precinct"])
        choice_votes = (
            row["Ballots Cast"] if contest == REGISTERED_VOTERS
            else row[selected_choice]
        )
        if contest != REGISTERED_VOTERS and choice_votes == 0:
            continue
        if (
            governor is None
            or turnout is None
            or not turnout["Ballots Cast"]
            or not governor["Contest Total Votes"]
            or choice_votes is None
        ):
            continue
        governor_votes = governor[governor_candidate]
        if governor_votes is None:
            continue
        ballots = turnout["Ballots Cast"]
        percentage = (
            row["Turnout"]
            if contest == REGISTERED_VOTERS
            else choice_votes / row["Contest Total Votes"] * 100
        )
        vote_percentages.append(percentage)
        governor_shares.append(governor_votes / governor["Contest Total Votes"] * 100)
        ballot_counts.append(ballots)
        choice_counts.append(choice_votes)
        becerra_votes = governor[BECERRA]
        blue_share = (
            f"{becerra_votes / governor['Contest Total Votes'] * 100:.1f}%"
            if becerra_votes is not None
            else "Unavailable"
        )
        hover_data.append([
            choice_votes,
            ballots if contest == REGISTERED_VOTERS else row["Contest Total Votes"],
            blue_share,
            display_precinct_id(row["Precinct"]),
        ])

    x_title = (
        "Voter turnout"
        if contest == REGISTERED_VOTERS
        else f"{selected_choice} Precinct Vote Share"
    )
    hovertemplate = (
        "<b>Precinct %{customdata[3]}</b><br>"
        f"{x_title}: %{{x:.1f}}%<br>"
        f"{selected_choice} votes: %{{customdata[0]:,.0f}}<br>"
        "Contest votes cast: %{customdata[1]:,.0f}<br>"
        "Xavier Becerra precinct vote share: %{customdata[2]}<extra></extra>"
        if contest != REGISTERED_VOTERS
        else (
            "<b>Precinct %{customdata[3]}</b><br>"
            "Voter turnout: %{x:.1f}%<br>"
            "Ballots cast: %{customdata[0]:,.0f}<br>"
            "Xavier Becerra precinct vote share: %{customdata[2]}<extra></extra>"
        )
    )
    figure = go.Figure(go.Scatter(
        x=vote_percentages,
        y=governor_shares,
        customdata=hover_data,
        mode="markers",
        marker={
            "color": color,
            "opacity": 0.7,
            "size": ballot_counts,
            "sizemode": "area",
            "sizeref": 2 * max(ballot_counts, default=1) / 40**2,
        },
        hovertemplate=hovertemplate,
    ))
    if len(vote_percentages) >= 2 and min(vote_percentages) != max(vote_percentages):
        vote_data = (
            pl.DataFrame(
                {
                    "vote_percentage": vote_percentages,
                    "governor_share": governor_shares,
                    "votes": choice_counts,
                }
            )
            .select(
                pl.col("vote_percentage").repeat_by("votes"),
                pl.col("governor_share").repeat_by("votes"),
            )
            .explode("vote_percentage", "governor_share")
        )
        regression = linregress(
            vote_data["vote_percentage"].to_numpy(),
            vote_data["governor_share"].to_numpy(),
        )
        line_x = [min(vote_percentages), max(vote_percentages)]
        figure.add_trace(go.Scatter(
            x=line_x,
            y=[regression.slope * x + regression.intercept for x in line_x],
            mode="lines",
            name="Best fit",
            line={"color": color, "width": 2},
            hoverinfo="skip",
        ))
        figure.add_annotation(
            x=line_x[-1],
            y=regression.slope * line_x[-1] + regression.intercept,
            text="Best Fit",
            font={"color": color},
            showarrow=False,
            xanchor="right",
            yanchor="bottom",
        )
        correlation = (
            f"{regression.rvalue:.3f}" if isfinite(regression.rvalue) else "undefined"
        )
        subtitle = (
            f"Slope: {regression.slope:.3f} | "
            f"Y-intercept: {regression.intercept:.3f} | r: {correlation}"
        )
    else:
        subtitle = "Best fit unavailable: at least two distinct x values required"
    figure.update_layout(
        title={
            "text": f"{display_contest_name(contest)} — Correlation with X. Becerra",
            "subtitle": {
                "text": f"{selected_choice} vs. Xavier Becerra precinct vote share | {subtitle}"
            },
        },
        xaxis={
            "title": x_title,
            "range": scatter_axis_range(vote_percentages),
            "ticksuffix": "%",
        },
        yaxis={
            "title": "Xavier Becerra Precinct Vote Share",
            "range": scatter_axis_range(governor_shares),
            "ticksuffix": "%",
        },
        paper_bgcolor="white",
        plot_bgcolor="white",
        showlegend=False,
        margin={"l": 60, "r": 30, "t": 80, "b": 55},
    )
    return figure


@lru_cache(maxsize=32)
def load_vote_type_totals(page):
    choices, _ = load_contest(page)
    frame = (
        pl.read_excel(SOURCE_DATA, sheet_name=page, read_options={"header_row": 2})
        .filter(pl.col("Precinct") != "Total:")
    )
    total_columns = [
        column for column in frame.columns
        if column == "Total Votes" or column.startswith("Total Votes_")
    ]
    if len(total_columns) != len(choices):
        raise ValueError(f"Vote columns do not match choices for contest {page}")

    totals = []
    for total_column in total_columns:
        suffix = total_column[len("Total Votes"):]
        columns = [f"Vote By Mail{suffix}", f"In Person{suffix}", total_column]
        if any(column not in frame.columns for column in columns):
            raise ValueError(f"Missing vote-type columns for {total_column} in contest {page}")
        totals.append(tuple(frame[column].sum() or 0 for column in columns))
    return choices, tuple(totals)


def build_vote_type_chart(page):
    contest = CONTESTS[page]
    figure = go.Figure()
    if contest == REGISTERED_VOTERS:
        figure.add_annotation(
            text="Vote-type counts are not available for the registered voters view.",
            x=0.5,
            y=0.5,
            xref="paper",
            yref="paper",
            showarrow=False,
        )
        figure.update_xaxes(visible=False)
        figure.update_yaxes(visible=False)
    else:
        choices, totals = load_vote_type_totals(page)
        contest_total = sum(total[2] for total in totals)
        percentages = []
        for mail, in_person, total in totals:
            if contest_total:
                total_tenths = round(total * 1000 / contest_total)
                mail_tenths = round(mail * 1000 / contest_total)
                percentages.append((
                    f"{mail_tenths / 10:.1f}%",
                    f"{(total_tenths - mail_tenths) / 10:.1f}%",
                    f"{total_tenths / 10:.1f}%",
                ))
            else:
                percentages.append(("N/A", "N/A", "N/A"))
        for index, (name, color) in enumerate((
            ("By mail", "#2563eb"),
            ("In person", "#16a34a"),
            ("Total", "#64748b"),
        )):
            counts = [total[index] for total in totals]
            figure.add_trace(go.Bar(
                name=name,
                x=choices,
                y=counts,
                text=[percentage[index] for percentage in percentages],
                textposition="auto",
                marker_color=color,
                hovertemplate=(
                    "<b>%{x}</b><br>"
                    f"{name}: %{{y:,.0f}} votes<br>"
                    "Share of contest votes: %{text}<extra></extra>"
                ),
            ))
        figure.update_yaxes(title="Vote count", rangemode="tozero")
    figure.update_layout(
        title={
            "text": f"{display_contest_name(contest)} — Votes by type",
            "subtitle": {"text": "Percentages are shares of total contest votes"},
        },
        barmode="group",
        paper_bgcolor="white",
        plot_bgcolor="white",
        margin={"l": 60, "r": 30, "t": 80, "b": 55},
        legend={"orientation": "v", "x": 1, "xanchor": "right", "y": 1, "yanchor": "top"},
    )
    return figure


default_page = CONTEST_PAGES.get("Governor (Vote For 1)", next(iter(CONTESTS)))
default_choices, _ = load_contest(default_page)

app = dash.Dash(__name__)
app.title = "Santa Clara County Election Results"
app.layout = html.Div(
    [
        html.H1(
            "Santa Clara County General Election Results, November 3 2026", 
            style={"margin": "0 0 6px"}
        ),
        html.P(
            [
                "Explore precinct-level results, vote share, and turnout. "
                "These election results were released by the Santa Clara County "
                f"Registrar of Voters on {results_date_label} and are partial; "
                "counting is still underway. ",
                html.A(
                    f"Estimated unprocessed ballots: {format_count(UNPROCESSED_BALLOTS)}",
                    href=UNPROCESSED_BALLOTS_URL,
                ),
                f" (as of {UNPROCESSED_BALLOTS_UPDATED}; estimate subject to change).",
            ],
            style={"color": "#475569", "margin": "0 0 20px"},
        ),
        html.Div(
            [
                html.Div(
                    [
                        html.Label("Contest", htmlFor="contest-selector"),
                        dcc.Dropdown(
                            id="contest-selector",
                            options=[
                                {"label": display_contest_name(contest), "value": page}
                                for page, contest in CONTESTS.items()
                            ],
                            value=default_page,
                            clearable=False,
                        ),
                    ],
                    style={"flex": "2", "minWidth": "280px"},
                ),
                html.Div(
                    [
                        html.Label("Candidate", htmlFor="map-choice"),
                        dcc.Dropdown(
                            id="map-choice",
                            options=[
                                {"label": choice, "value": choice}
                                for choice in default_choices
                            ],
                            value=default_choices[0],
                            clearable=False,
                        ),
                    ],
                    style={"flex": "1", "minWidth": "220px"},
                ),
                html.Div(
                    [
                        html.Label("Map style", htmlFor="map-style"),
                        dcc.Dropdown(
                            id="map-style",
                            options=[
                                {"label": style, "value": style}
                                for style in MAP_STYLES
                            ],
                            value='carto-positron',
                            clearable=False,
                        ),
                    ],
                    style={"flex": "1", "minWidth": "220px"},
                ),
            ],
            style={"display": "flex", "gap": "16px", "flexWrap": "wrap"},
        ),
        html.Div(
            id="metric-cards",
            style={
                "display": "flex",
                "gap": "12px",
                "flexWrap": "wrap",
                "margin": "18px 0",
            },
        ),
        dcc.Graph(id="precinct-map", config={"displayModeBar": True}),
        html.Div(
            [
                dcc.Graph(
                    id="blueness-scatter",
                    config={"displayModeBar": True},
                    style={"flex": "1 1 420px", "minWidth": 0},
                ),
                dcc.Graph(
                    id="vote-type-chart",
                    config={"displayModeBar": True},
                    style={"flex": "1 1 420px", "minWidth": 0},
                ),
            ],
            style={"display": "flex", "flexWrap": "wrap", "gap": "12px"},
        ),
        html.H2("Precinct results", style={"margin": "24px 0 12px"}),
        dag.AgGrid(
            id="results-grid",
            columnDefs=[],
            rowData=[],
            defaultColDef={"minWidth": 120},
            dashGridOptions={
                "pagination": True,
                "paginationPageSize": 25,
                "animateRows": False,
            },
            columnSize="responsiveSizeToFit",
            style={"height": "520px", "width": "100%"},
        ),
        html.P(
            "Election results and precinct shape files provided by Santa Clara County "
            "Registrar of Voters. This dashboard created by Mike Purtell.",
            style={"color": "#475569", "margin": "24px 0 0"},
        ),
    ],
    style={
        "fontFamily": "Arial, sans-serif",
        "color": "#0f172a",
        "background": "#f8fafc",
        "padding": "24px",
        "maxWidth": "1500px",
        "margin": "0 auto",
    },
)


@app.callback(
    Output("map-choice", "options"),
    Output("map-choice", "value"),
    Input("contest-selector", "value"),
)
def update_map_choices(page):
    choices, _ = load_contest(page)
    return (
        [{"label": choice, "value": choice} for choice in choices],
        choices[0],
    )


@app.callback(
    Output("precinct-map", "figure"),
    Output("metric-cards", "children"),
    Output("results-grid", "columnDefs"),
    Output("results-grid", "rowData"),
    Input("contest-selector", "value"),
    Input("map-choice", "value"),
    Input("map-style", "value"),
)
def update_dashboard(page, selected_choice, map_style):
    return build_dashboard(page, selected_choice, map_style)


@app.callback(
    Output("blueness-scatter", "figure"),
    Input("contest-selector", "value"),
    Input("map-choice", "value"),
)
def update_blueness_scatter(page, selected_choice):
    return build_partisanship_scatter(page, selected_choice, BECERRA, "#2563eb")


@app.callback(
    Output("vote-type-chart", "figure"),
    Input("contest-selector", "value"),
)
def update_vote_type_chart(page):
    return build_vote_type_chart(page)


if __name__ == "__main__":
    app.run(debug=True)
