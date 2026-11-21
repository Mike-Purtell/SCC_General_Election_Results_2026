import json
import os
import re
import tempfile
from datetime import datetime
from functools import lru_cache
from hashlib import sha256
from math import (
    asinh,
    atan,
    degrees,
    fsum,
    isfinite,
    log2,
    pi,
    radians,
    sinh,
    sqrt,
    tan,
)
from pathlib import Path

import dash
import dash_ag_grid as dag
from dash import Input, Output, Patch, State, dcc, html
import plotly.express as px
import plotly.graph_objects as go
import polars as pl
from pypdf import PdfReader
from pyogrio.raw import read as read_geodatabase_layer
from pyproj import Transformer
from shapely.geometry import shape
from shapely.wkb import loads as load_wkb

VOTES_REMAINING = 1500
ROOT = Path(__file__).resolve().parent
SOURCE_DATA = ROOT / 'detail_2026_11_20.xlsx'
PRECINCT_CROSS_REFERENCE = ROOT / 'e148 wmr008_votabsregpctxref.pdf'
PRECINCT_CROSS_REFERENCE_PARQUET = ROOT / 'precinct_cross_reference.parquet'
PRECINCT_GEODATABASE = ROOT / 'E148_Consolidated_Precinct.gdb'
PRECINCT_GEOJSON_CACHE = Path(tempfile.gettempdir()) / (
    f'scc_election_precinct_geojson_{sha256(str(ROOT).encode()).hexdigest()[:16]}.json'
)
PRECINCT_LAYER = 'E148_Consolidated_Precinct'
PRECINCT_ID_FIELD = 'VPCT'
PRECINCT_COLOR_SCALE = ['#ffff00', '#008000']
REGISTERED_VOTERS = 'Registered Voters'
GOVERNOR_CONTEST = 'Governor (Vote For 1)'
BECERRA = 'Xavier Becerra'
UNPROCESSED_BALLOTS_URL = (
    'https://vote.santaclaracounty.gov/elections/unprocessed-ballot-estimates'
)
MAP_STYLES = (
    'open-street-map',
    'white-bg',
    'carto-voyager',
    'carto-voyager-nolabels',
    'carto-positron',
    'carto-positron-nolabels',
    'carto-darkmatter',
    'carto-darkmatter-nolabels',
    'satellite',
    'satellite-streets',
    'basic',
    'streets',
    'outdoors',
    'light',
    'dark',
)


def source_results_date(source_path):
    match = re.search(r'(?:^|_)(\d{4}_\d{2}_\d{2})$', source_path.stem)
    if match is None:
        raise ValueError(
            f'Source filename must end with a date in YYYY_MM_DD format: {source_path.name}'
        )
    return datetime.strptime(match.group(1), '%Y_%m_%d').date()


results_date = source_results_date(SOURCE_DATA)
results_date_label = f'{results_date:%B} {results_date.day}, {results_date.year}'


def precinct_id(value):
    return str(value).strip().zfill(7)


def display_precinct_id(value):
    return value[3:] if len(value) == 7 and value.startswith('000') else value


def read_precinct_cross_reference(path):
    row_pattern = re.compile(
        r'^\s*\d+(?:(\d{7})\s+- PCT (\d+)\s+|\s+)(\d{7}) PCT (\d+)\s*$'
    )
    totals_pattern = re.compile(r'Regular Pcts:\s*([\d,]+)\s*Total Voting Pcts:\s*([\d,]+)')
    rows = []
    voting_precinct = None
    expected_totals = None
    for page in PdfReader(path).pages:
        for line in page.extract_text().splitlines():
            match = row_pattern.fullmatch(line)
            if match:
                voting_id, voting_label, regular_id, regular_label = match.groups()
                if voting_id:
                    if int(voting_id) != int(voting_label):
                        raise ValueError(f'Voting precinct ID mismatch: {line}')
                    voting_precinct = voting_id
                if voting_precinct is None or int(regular_id) != int(regular_label):
                    raise ValueError(f'Invalid precinct cross-reference row: {line}')
                rows.append(
                    {
                        'Regular Precinct': display_precinct_id(regular_id),
                        'Voting Precinct': display_precinct_id(voting_precinct),
                    }
                )
            elif ' PCT ' in line and not line.startswith('EWMR008'):
                raise ValueError(f'Unrecognized precinct cross-reference row: {line}')
            else:
                totals = totals_pattern.search(line)
                if totals:
                    expected_totals = tuple(int(value.replace(',', '')) for value in totals.groups())

    regular_ids = {row['Regular Precinct'] for row in rows}
    voting_ids = {row['Voting Precinct'] for row in rows}
    if expected_totals != (len(rows), len(voting_ids)) or len(regular_ids) != len(rows):
        raise ValueError(f'Precinct cross-reference totals do not match {path}')
    return pl.DataFrame(rows, schema={'Regular Precinct': pl.String, 'Voting Precinct': pl.String})


def load_precinct_cross_reference(pdf_path, parquet_path):
    if parquet_path.exists():
        return pl.read_parquet(parquet_path)
    frame = read_precinct_cross_reference(pdf_path)
    frame.write_parquet(parquet_path)
    return frame


def display_contest_name(contest):
    contest = re.sub(
        r'\s*\(Vote\s+For\s+[01]\)\s*$', '', contest, flags=re.IGNORECASE
    )
    return re.sub(r'^State Proposition\b', 'CA Prop', contest, flags=re.IGNORECASE)


def read_turnout():
    turnout = (
        pl.read_excel(
            SOURCE_DATA,
            sheet_name=REGISTERED_VOTERS,
            read_options={'header_row': 0},
        )
        .filter(pl.col('Precinct') != 'Total:')
        .with_columns(pl.col('Precinct').str.pad_start(7, '0'))
    )
    turnout_rows = turnout.to_dicts()
    turnout_by_precinct = {
        row['Precinct']: {
            'Registered Voters': row['Registered Voters'] or 0,
            'Ballots Cast': row['Ballots Cast'] or 0,
            'Turnout Percent': float(row['Voter Turnout'].rstrip('%')),
        }
        for row in turnout_rows
    }
    registered = sum(row['Registered Voters'] for row in turnout_rows)
    ballots = sum(row['Ballots Cast'] for row in turnout_rows)
    county_turnout = ballots / registered * 100 if registered else 0
    return turnout_by_precinct, registered, ballots, county_turnout


TURNOUT_BY_PRECINCT, COUNTY_REGISTERED, COUNTY_BALLOTS, COUNTY_TURNOUT = read_turnout()

contest_table = pl.read_excel(
    SOURCE_DATA,
    sheet_name='Table of Contents',
    read_options={'header_row': 3},
)
CONTESTS = {
    str(row['Page']): row['Contest']
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


def precinct_geodatabase_signature():
    signature = []
    for path in sorted(PRECINCT_GEODATABASE.rglob('*')):
        if path.is_file():
            stat = path.stat()
            signature.append(
                {
                    'path': path.relative_to(PRECINCT_GEODATABASE).as_posix(),
                    'size': stat.st_size,
                    'mtime_ns': stat.st_mtime_ns,
                }
            )
    return signature


def load_precinct_geojson():
    source_signature = precinct_geodatabase_signature()
    if PRECINCT_GEOJSON_CACHE.exists():
        cached = json.loads(PRECINCT_GEOJSON_CACHE.read_text(encoding='utf-8'))
        if (
            cached.get('version') == 1
            and cached.get('source_signature') == source_signature
        ):
            bounds = {
                precinct: tuple(bound)
                for precinct, bound in cached['bounds'].items()
            }
            return cached['geojson'], bounds

    metadata, _, geometries, fields = read_geodatabase_layer(
        str(PRECINCT_GEODATABASE),
        layer=PRECINCT_LAYER,
        columns=[PRECINCT_ID_FIELD],
    )
    transformer = Transformer.from_crs(
        metadata['crs'], 'EPSG:4326', always_xy=True
    )
    features = []

    for precinct_value, geometry_wkb in zip(fields[0], geometries, strict=True):
        precinct = precinct_id(precinct_value)
        polygon = load_wkb(geometry_wkb)
        geometry = polygon.__geo_interface__
        if polygon.geom_type == 'MultiPolygon':
            polygon = max(polygon.geoms, key=lambda part: part.area)
        point = polygon.representative_point()
        longitude, latitude = transformer.transform(point.x, point.y)
        features.append(
            {
                'type': 'Feature',
                'id': precinct,
                'properties': {
                    'Precinct': precinct,
                    'label_lon': longitude,
                    'label_lat': latitude,
                },
                'geometry': {
                    'type': geometry['type'],
                    'coordinates': project_coordinates(
                        geometry['coordinates'], transformer
                    ),
                },
            }
        )

    geojson = {'type': 'FeatureCollection', 'features': features}
    bounds = {
        feature['properties']['Precinct']: shape(feature['geometry']).bounds
        for feature in features
    }
    cache = {
        'version': 1,
        'source_signature': source_signature,
        'geojson': geojson,
        'bounds': bounds,
    }
    temporary_path = PRECINCT_GEOJSON_CACHE.with_name(
        f'{PRECINCT_GEOJSON_CACHE.name}.{os.getpid()}.tmp'
    )
    temporary_path.write_text(
        json.dumps(cache, separators=(',', ':')),
        encoding='utf-8',
    )
    temporary_path.replace(PRECINCT_GEOJSON_CACHE)
    return geojson, bounds


PRECINCT_GEOJSON, PRECINCT_BOUNDS = load_precinct_geojson()
PRECINCT_IDS = {
    feature['properties']['Precinct'] for feature in PRECINCT_GEOJSON['features']
}
PRECINCT_FEATURES = {
    feature['properties']['Precinct']: feature
    for feature in PRECINCT_GEOJSON['features']
}
PRECINCT_LABELS = {
    feature['properties']['Precinct']: (
        feature['properties']['label_lon'],
        feature['properties']['label_lat'],
    )
    for feature in PRECINCT_GEOJSON['features']
}


def precinct_map_view(precincts):
    bounds = [PRECINCT_BOUNDS[precinct] for precinct in precincts if precinct in PRECINCT_BOUNDS]
    if not bounds:
        return {'lat': 37.25, 'lon': -121.93}, 8
    west = min(bound[0] for bound in bounds)
    south = min(bound[1] for bound in bounds)
    east = max(bound[2] for bound in bounds)
    north = max(bound[3] for bound in bounds)
    south_mercator = asinh(tan(radians(south)))
    north_mercator = asinh(tan(radians(north)))
    center = {
        'lon': (west + east) / 2,
        'lat': degrees(atan(sinh((south_mercator + north_mercator) / 2))),
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
            read_options={'header_row': header_row},
        )
        .filter(pl.col('Precinct') != 'Total:')
        .with_columns(pl.col('Precinct').str.pad_start(7, '0'))
    )

    if contest == REGISTERED_VOTERS:
        rows = []
        for row in frame.to_dicts():
            turnout = TURNOUT_BY_PRECINCT.get(row['Precinct'], {})
            rows.append(
                {
                    'Precinct': row['Precinct'],
                    'Registered Voters': row['Registered Voters'] or 0,
                    'Ballots Cast': row['Ballots Cast'] or 0,
                    'Turnout': turnout.get('Turnout Percent', 0),
                }
            )
        return ('Voter Turnout',), tuple(rows)

    if not include_zero_votes:
        frame = frame.filter(pl.col('Total') != 0)
    choice_headers = pl.read_excel(
        SOURCE_DATA,
        sheet_name=sheet_name,
        read_options={'header_row': header_row - 1},
    ).columns
    choice_names = {}
    for index, column in enumerate(frame.columns):
        if column == 'Total Votes' or column.startswith('Total Votes_'):
            choice = choice_headers[index - 2]
            if choice.upper() in {'YES', 'NO'}:
                choice = choice.title()
            choice_names[column] = choice

    frame = frame.drop(
        [
            column
            for column in frame.columns
            if column in {'In Person', 'Vote By Mail'}
            or column.startswith(('In Person_', 'Vote By Mail_'))
        ]
    ).rename(choice_names)
    choices = tuple(choice_names.values())
    rows = []
    for row in frame.to_dicts():
        turnout = TURNOUT_BY_PRECINCT.get(row['Precinct'], {})
        row['Contest Total Votes'] = row.pop('Total', 0) or 0
        row['Registered Voters'] = row.get('Registered Voters') or 0
        row['Ballots Cast'] = turnout.get('Ballots Cast', 0)
        row['Turnout'] = turnout.get('Turnout Percent', 0)
        rows.append(row)
    return choices, tuple(rows)


@lru_cache(maxsize=32)
def load_contest_map_geometry(page):
    _, rows = load_contest(page)
    map_precincts = tuple(
        row['Precinct']
        for row in rows
        if row['Precinct'] in PRECINCT_FEATURES
    )
    geojson = {
        'type': 'FeatureCollection',
        'features': [PRECINCT_FEATURES[precinct] for precinct in map_precincts],
    }
    center, zoom = precinct_map_view(map_precincts)
    label_ids = tuple(
        row['Precinct']
        for row in rows
        if row['Precinct'] in PRECINCT_LABELS
    )
    return geojson, center, zoom, label_ids


def format_count(value):
    return f'{value:,.0f}'


def metric_card(label, value):
    return html.Div(
        [
            html.P(label, style={'margin': '0 0 8px', 'color': '#64748b'}),
            html.H3(value, style={'margin': 0, 'color': '#0f172a'}),
        ],
        style={
            'background': 'white',
            'border': '1px solid #e2e8f0',
            'borderRadius': '10px',
            'padding': '16px',
            'minWidth': '150px',
        },
    )


def build_dashboard(
    page,
    selected_choice,
    map_style='open-street-map',
    current_figure=None,
):
    if map_style not in MAP_STYLES:
        raise ValueError(f'Unsupported map style: {map_style}')
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
                    'Precinct': row['Precinct'],
                    'Choice': 'Voter Turnout',
                    'Choice Votes': row['Ballots Cast'],
                    'Vote Percentage': row['Turnout'],
                    'Contest Total Votes': row['Ballots Cast'],
                    'Turnout': row['Turnout'],
                    'Map Value': row['Turnout'],
                    'Registered Voters': row['Registered Voters'],
                }
            )
            grid_rows.append({**row, 'Precinct': display_precinct_id(row['Precinct'])})
        cards = [
            metric_card('Registered voters', format_count(COUNTY_REGISTERED)),
            metric_card('Ballots cast', format_count(COUNTY_BALLOTS)),
            metric_card('Voter turnout', f'{COUNTY_TURNOUT:.2f}%'),
        ]
    else:
        choice_totals = {choice: 0 for choice in choices}
        contest_total = 0
        for row in rows:
            total_votes = row['Contest Total Votes']
            contest_total += total_votes
            for choice in choices:
                votes = row.get(choice) or 0
                choice_totals[choice] += votes
            selected_votes = row.get(selected_choice) or 0
            share = selected_votes / total_votes * 100 if total_votes else 0
            map_rows.append(
                {
                    'Precinct': row['Precinct'],
                    'Choice': selected_choice,
                    'Choice Votes': selected_votes,
                    'Vote Percentage': share,
                    'Contest Total Votes': total_votes,
                    'Turnout': row['Turnout'],
                    'Map Value': share,
                    'Registered Voters': row['Registered Voters'],
                }
            )
            grid_rows.append({**row, 'Precinct': display_precinct_id(row['Precinct'])})

        cards = [
            metric_card(
                choice,
                f'{format_count(total)} '
                f'({total / contest_total * 100:.2f}%)'
                if contest_total
                else f'{format_count(total)} (N/A)',
            )
            for choice, total in choice_totals.items()
        ]
        cards.extend(
            [
                metric_card('Contest votes', format_count(contest_total)),
                metric_card('Voter turnout', f'{COUNTY_TURNOUT:.2f}%'),
            ]
        )

    displayed_values = pl.Series(
        'Map Value',
        [
            row['Map Value']
            for row in map_rows
            if row['Precinct'] in PRECINCT_IDS
        ],
        dtype=pl.Float64,
    )
    color_range = (
        (
            displayed_values.quantile(0.1, interpolation='linear'),
            displayed_values.quantile(0.9, interpolation='linear'),
        )
        if not displayed_values.is_empty()
        else None
    )
    map_frame = pl.DataFrame(
        map_rows,
        schema={
            'Precinct': pl.String,
            'Choice': pl.String,
            'Choice Votes': pl.Int64,
            'Vote Percentage': pl.Float64,
            'Contest Total Votes': pl.Int64,
            'Turnout': pl.Float64,
            'Map Value': pl.Float64,
            'Registered Voters': pl.Int64,
        },
    ).with_columns(
        pl.when(pl.col('Precinct').str.contains(r'^000\d{4}$'))
        .then(pl.col('Precinct').str.slice(3))
        .otherwise(pl.col('Precinct'))
        .alias('Display Precinct')
    )
    if (
        current_figure
        and current_figure.get('layout', {}).get('uirevision') == page
    ):
        figure = Patch()
        figure['data'][0]['z'] = map_frame['Map Value'].to_list()
        figure['data'][0]['customdata'] = [
            [
                row[column]
                for column in (
                    'Choice',
                    'Choice Votes',
                    'Vote Percentage',
                    'Contest Total Votes',
                    'Turnout',
                    'Display Precinct',
                    'Registered Voters',
                )
            ]
            for row in map_frame.to_dicts()
        ]
        figure['layout']['map']['style'] = map_style
        figure['layout']['title']['text'] = (
            f'{display_contest_name(contest)} — {selected_choice}'
        )
        figure['layout']['coloraxis']['colorbar']['title']['text'] = (
            selected_choice
            if contest == REGISTERED_VOTERS
            else f'{selected_choice} share'
        )
        if color_range is not None:
            figure['layout']['coloraxis']['cmin'] = color_range[0]
            figure['layout']['coloraxis']['cmax'] = color_range[1]
    else:
        contest_geojson, center, zoom, label_ids = load_contest_map_geometry(page)
        figure = px.choropleth_map(
            map_frame,
            geojson=contest_geojson,
            locations='Precinct',
            featureidkey='properties.Precinct',
            color='Map Value',
            range_color=color_range,
            color_continuous_scale=PRECINCT_COLOR_SCALE,
            opacity=0.5,
            map_style=map_style,
            center=center,
            zoom=zoom,
            custom_data=[
                'Choice',
                'Choice Votes',
                'Vote Percentage',
                'Contest Total Votes',
                'Turnout',
                'Display Precinct',
                'Registered Voters',
            ],
        )
        figure.update_traces(
            hovertemplate=(
                '<b>Consolidated Precinct %{customdata[5]}</b><br>'
                'Ballots Cast: %{customdata[1]:,.0f}<br>'
                'Registered Voters: %{customdata[6]:,.0f}<br>'
                'Turnout: %{customdata[4]:.1f}%<extra></extra>'
                if contest == REGISTERED_VOTERS
                else (
                    '<b>Consolidated Precinct %{customdata[5]}</b><br>'
                    '%{customdata[0]}: %{customdata[1]:,.0f} votes (%{customdata[2]:.1f}%)<br>'
                    'Precinct votes: %{customdata[3]:,.0f}<br>'
                    'Precinct turnout: %{customdata[4]:.1f}%'
                    '<extra></extra>'
                )
            )
        )
        figure.add_trace(go.Scattermap(
            lon=[PRECINCT_LABELS[precinct][0] for precinct in label_ids],
            lat=[PRECINCT_LABELS[precinct][1] for precinct in label_ids],
            text=[display_precinct_id(precinct) for precinct in label_ids],
            mode='text',
            textfont={'size': 10, 'color': '#111827'},
            hoverinfo='skip',
            showlegend=False,
        ))
        figure.update_layout(
            title=f'{display_contest_name(contest)} — {selected_choice}',
            margin={'l': 0, 'r': 0, 't': 48, 'b': 0},
            height=620,
            coloraxis_colorbar={
                'title': selected_choice
                if contest == REGISTERED_VOTERS
                else f'{selected_choice} share',
                'ticksuffix': '%',
            },
            paper_bgcolor='white',
            dragmode='pan',
            uirevision=page,
        )

    grid_columns = list(grid_rows[0]) if grid_rows else []
    column_header_names = {
        'Precinct': 'Consolidated Voting Precinct',
        'Registered Voters': 'Registered Voters',
        'Contest Total Votes': 'Contest Total Votes',
        'Ballots Cast': 'Ballots Cast',
    }
    column_defs = []
    for column in grid_columns:
        definition = {
            'field': column,
            'width': 200,
            'filter': True,
            'sortable': True,
            'resizable': True,
        }
        if column in column_header_names:
            definition.update(
                {
                    'headerName': column_header_names[column],
                    'wrapHeaderText': True,
                    'autoHeaderHeight': True,
                }
            )
        if (
            column != 'Precinct'
            and grid_rows
            and isinstance(grid_rows[0].get(column), (int, float))
        ):
            definition['type'] = 'numericColumn'
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


def density_axis_range(values):
    if not values:
        return scatter_axis_range(values)
    series = pl.Series(values)
    lower = series.quantile(0.05, interpolation='linear')
    upper = series.quantile(0.95, interpolation='linear')
    if lower == upper:
        return scatter_axis_range([lower])
    return [lower, upper]


@lru_cache(maxsize=32)
def load_partisanship_data(page, selected_choice, governor_candidate):
    contest = CONTESTS[page]
    choices, rows = load_contest(page)
    if selected_choice not in choices:
        selected_choice = choices[0]

    governor_choices, governor_rows = load_contest(
        CONTEST_PAGES[GOVERNOR_CONTEST], include_zero_votes=True
    )
    for candidate in {governor_candidate, BECERRA}:
        if candidate not in governor_choices:
            raise ValueError(f'{candidate} is missing from the governor contest')
    governor_by_precinct = {row['Precinct']: row for row in governor_rows}

    vote_percentages = []
    governor_shares = []
    ballot_counts = []
    choice_counts = []
    hover_data = []
    for row in rows:
        governor = governor_by_precinct.get(row['Precinct'])
        turnout = TURNOUT_BY_PRECINCT.get(row['Precinct'])
        choice_votes = (
            row['Ballots Cast'] if contest == REGISTERED_VOTERS
            else row[selected_choice]
        )
        if contest != REGISTERED_VOTERS and choice_votes == 0:
            continue
        if (
            governor is None
            or turnout is None
            or not turnout['Ballots Cast']
            or not governor['Contest Total Votes']
            or choice_votes is None
        ):
            continue
        governor_votes = governor[governor_candidate]
        if governor_votes is None:
            continue
        ballots = turnout['Ballots Cast']
        percentage = (
            row['Turnout']
            if contest == REGISTERED_VOTERS
            else choice_votes / row['Contest Total Votes'] * 100
        )
        vote_percentages.append(percentage)
        governor_shares.append(governor_votes / governor['Contest Total Votes'] * 100)
        ballot_counts.append(ballots)
        choice_counts.append(choice_votes)
        becerra_votes = governor[BECERRA]
        blue_share = (
            f"{becerra_votes / governor['Contest Total Votes'] * 100:.1f}%"
            if becerra_votes is not None
            else 'Unavailable'
        )
        hover_data.append([
            choice_votes,
            ballots if contest == REGISTERED_VOTERS else row['Contest Total Votes'],
            blue_share,
            display_precinct_id(row['Precinct']),
        ])

    return (
        contest,
        selected_choice,
        tuple(vote_percentages),
        tuple(governor_shares),
        tuple(ballot_counts),
        tuple(choice_counts),
        tuple(tuple(row) for row in hover_data),
    )


def weighted_linear_regression(x_values, y_values, weights):
    if (
        not x_values
        or len(x_values) != len(y_values)
        or len(x_values) != len(weights)
        or any(weight <= 0 for weight in weights)
    ):
        raise ValueError(
            'Weighted regression requires matching values and positive weights'
        )

    weight_total = fsum(weights)
    x_mean = (
        fsum(x * weight for x, weight in zip(x_values, weights, strict=True))
        / weight_total
    )
    y_mean = (
        fsum(y * weight for y, weight in zip(y_values, weights, strict=True))
        / weight_total
    )
    ss_xx = fsum(
        weight * (x - x_mean) ** 2
        for x, weight in zip(x_values, weights, strict=True)
    )
    ss_yy = fsum(
        weight * (y - y_mean) ** 2
        for y, weight in zip(y_values, weights, strict=True)
    )
    ss_xy = fsum(
        weight * (x - x_mean) * (y - y_mean)
        for x, y, weight in zip(x_values, y_values, weights, strict=True)
    )
    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean
    correlation = ss_xy / sqrt(ss_xx * ss_yy) if ss_yy else 0.0
    return slope, intercept, max(-1.0, min(1.0, correlation))


def build_partisanship_scatter(page, selected_choice, governor_candidate, color):
    (
        contest,
        selected_choice,
        vote_percentages,
        governor_shares,
        ballot_counts,
        choice_counts,
        hover_data,
    ) = load_partisanship_data(page, selected_choice, governor_candidate)
    x_title = (
        'Voter turnout'
        if contest == REGISTERED_VOTERS
        else f'{selected_choice} Precinct Vote Share'
    )
    hovertemplate = (
        '<b>Precinct %{customdata[3]}</b><br>'
        f'{x_title}: %{{x:.1f}}%<br>'
        f'{selected_choice} votes: %{{customdata[0]:,.0f}}<br>'
        'Contest votes cast: %{customdata[1]:,.0f}<br>'
        'Xavier Becerra precinct vote share: %{customdata[2]}<extra></extra>'
        if contest != REGISTERED_VOTERS
        else (
            '<b>Precinct %{customdata[3]}</b><br>'
            'Voter turnout: %{x:.1f}%<br>'
            'Ballots cast: %{customdata[0]:,.0f}<br>'
            'Xavier Becerra precinct vote share: %{customdata[2]}<extra></extra>'
        )
    )
    figure = go.Figure(go.Scatter(
        x=vote_percentages,
        y=governor_shares,
        customdata=hover_data,
        mode='markers',
        marker={
            'color': color,
            'opacity': 0.7,
            'size': ballot_counts,
            'sizemode': 'area',
            'sizeref': 2 * max(ballot_counts, default=1) / 40**2,
        },
        hovertemplate=hovertemplate,
    ))
    if len(vote_percentages) >= 2 and min(vote_percentages) != max(vote_percentages):
        slope, intercept, rvalue = weighted_linear_regression(
            vote_percentages,
            governor_shares,
            choice_counts,
        )
        line_x = [min(vote_percentages), max(vote_percentages)]
        figure.add_trace(go.Scatter(
            x=line_x,
            y=[slope * x + intercept for x in line_x],
            mode='lines',
            name='Best fit',
            line={'color': color, 'width': 2},
            hoverinfo='skip',
        ))
        figure.add_annotation(
            x=line_x[-1],
            y=slope * line_x[-1] + intercept,
            text='Best Fit',
            font={'color': color},
            showarrow=False,
            xanchor='right',
            yanchor='bottom',
        )
        correlation = (
            f'{rvalue:.3f}' if isfinite(rvalue) else 'undefined'
        )
        subtitle = (
            f'| r: {correlation} | '
            f'Slope: {slope:.3f} | '
            f'Y-intercept: {intercept:.3f}'
        )
    else:
        subtitle = 'Best fit unavailable: at least two distinct x values required'
    figure.update_layout(
        title={
            'text': f'{display_contest_name(contest)} — Correlation with X. Becerra',
            'subtitle': {
                'text': f'{selected_choice} {subtitle}'
            },
        },
        xaxis={
            'title': x_title,
            'range': scatter_axis_range(vote_percentages),
            'ticksuffix': '%',
        },
        yaxis={
            'title': 'Xavier Becerra Precinct Vote Share',
            'range': scatter_axis_range(governor_shares),
            'ticksuffix': '%',
        },
        paper_bgcolor='white',
        plot_bgcolor='white',
        showlegend=False,
        margin={'l': 60, 'r': 30, 't': 80, 'b': 55},
    )
    return figure


def build_partisanship_heatmap(page, selected_choice, governor_candidate):
    contest, selected_choice, x_values, y_values, _, _, _ = load_partisanship_data(
        page, selected_choice, governor_candidate
    )
    x_title = (
        'Voter turnout'
        if contest == REGISTERED_VOTERS
        else f'{selected_choice} Precinct Vote Share'
    )
    x_bins = density_bins(x_values)
    y_bins = density_bins(y_values)
    bin_counts = {}
    for x_value, y_value in zip(x_values, y_values, strict=True):
        x_index = int((x_value - x_bins['start']) / x_bins['size'])
        y_index = int((y_value - y_bins['start']) / y_bins['size'])
        key = (x_index, y_index)
        bin_counts[key] = bin_counts.get(key, 0) + 1
    figure = go.Figure(go.Histogram2d(
        x=x_values,
        y=y_values,
        xbins=x_bins,
        ybins=y_bins,
        colorscale=['#ffffff', '#008000'],
        zmin=0,
        zmax=max(bin_counts.values(), default=1),
        colorbar={'title': 'Precinct count'},
        hovertemplate=(
            f'{x_title}: %{{x:.1f}}%<br>'
            'Xavier Becerra precinct vote share: %{y:.1f}%<br>'
            'Precincts: %{z}<extra></extra>'
        ),
    ))
    figure.update_layout(
        title=f'{display_contest_name(contest)} — Precinct density',
        xaxis={
            'title': x_title,
            'range': density_axis_range(x_values),
            'ticksuffix': '%',
        },
        yaxis={
            'title': 'Xavier Becerra Precinct Vote Share',
            'range': density_axis_range(y_values),
            'ticksuffix': '%',
        },
        paper_bgcolor='white',
        plot_bgcolor='white',
        margin={'l': 60, 'r': 30, 't': 80, 'b': 55},
    )
    return figure


def density_bins(values):
    if not values:
        return {'start': 0, 'end': 1, 'size': 1 / 30}
    minimum, maximum = min(values), max(values)
    if minimum == maximum:
        minimum -= 0.5
        maximum += 0.5
    # Keep the largest value inside the final bin rather than on its upper edge.
    size = (maximum - minimum) / 29.999999
    return {'start': minimum, 'end': minimum + 30 * size, 'size': size}


@lru_cache(maxsize=32)
def load_vote_type_totals(page):
    choices, _ = load_contest(page)
    frame = (
        pl.read_excel(SOURCE_DATA, sheet_name=page, read_options={'header_row': 2})
        .filter(pl.col('Precinct') != 'Total:')
    )
    total_columns = [
        column for column in frame.columns
        if column == 'Total Votes' or column.startswith('Total Votes_')
    ]
    if len(total_columns) != len(choices):
        raise ValueError(f'Vote columns do not match choices for contest {page}')

    totals = []
    for total_column in total_columns:
        suffix = total_column[len('Total Votes'):]
        columns = [f'Vote By Mail{suffix}', f'In Person{suffix}', total_column]
        if any(column not in frame.columns for column in columns):
            raise ValueError(f'Missing vote-type columns for {total_column} in contest {page}')
        totals.append(tuple(frame[column].sum() or 0 for column in columns))
    return choices, tuple(totals)


def build_vote_type_chart(page):
    contest = CONTESTS[page]
    figure = go.Figure()
    if contest == REGISTERED_VOTERS:
        figure.add_annotation(
            text='Vote-type counts are not available for the registered voters view.',
            x=0.5,
            y=0.5,
            xref='paper',
            yref='paper',
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
                    f'{mail_tenths / 10:.1f}%',
                    f'{(total_tenths - mail_tenths) / 10:.1f}%',
                    f'{total_tenths / 10:.1f}%',
                ))
            else:
                percentages.append(('N/A', 'N/A', 'N/A'))
        for index, (name, color) in enumerate((
            ('By mail', '#2563eb'),
            ('In person', '#16a34a'),
            ('Total', '#64748b'),
        )):
            counts = [total[index] for total in totals]
            figure.add_trace(go.Bar(
                name=name,
                x=choices,
                y=counts,
                text=[percentage[index] for percentage in percentages],
                textposition='auto',
                marker_color=color,
                hovertemplate=(
                    '<b>%{x}</b><br>'
                    f'{name}: %{{y:,.0f}} votes<br>'
                    'Share of contest votes: %{text}<extra></extra>'
                ),
            ))
        figure.update_yaxes(title='Vote count', rangemode='tozero')
    figure.update_layout(
        title={
            'text': f'{display_contest_name(contest)} — Votes by type',
            'subtitle': {'text': 'Percentages are shares of total contest votes'},
        },
        barmode='group',
        paper_bgcolor='white',
        plot_bgcolor='white',
        margin={'l': 60, 'r': 30, 't': 80, 'b': 55},
        legend={'orientation': 'v', 'x': 1, 'xanchor': 'right', 'y': 1, 'yanchor': 'top'},
    )
    return figure


def build_precinct_distribution(page, selected_choice):
    contest = CONTESTS[page]
    choices, rows = load_contest(page)
    if selected_choice not in choices:
        selected_choice = choices[0]

    if contest == REGISTERED_VOTERS:
        values = [row['Turnout'] for row in rows if row['Registered Voters'] > 0]
        county_share = COUNTY_TURNOUT if COUNTY_REGISTERED else None
        x_title = 'Precinct voter turnout'
        line_label = 'Turnout'
    else:
        values = [
            (row.get(selected_choice) or 0) / row['Contest Total Votes'] * 100
            for row in rows
            if row['Contest Total Votes'] > 0
        ]
        total_votes = sum(row['Contest Total Votes'] for row in rows)
        county_share = (
            sum(row.get(selected_choice) or 0 for row in rows) / total_votes * 100
            if total_votes
            else None
        )
        x_title = f'{selected_choice} precinct vote share'
        line_label = 'Vote share'

    figure = go.Figure()
    if values:
        figure.add_trace(go.Histogram(
            x=values,
            xbins={'start': 0, 'size': 5},
            marker_color='#bacaba',
            marker_line={'color': '#808080', 'width': 1},
            hovertemplate=f'{x_title}: %{{x}}%<br>Precincts: %{{y}}<extra></extra>',
        ))
    else:
        figure.add_annotation(
            text='No precinct data available for this distribution.',
            x=0.5,
            y=0.5,
            xref='paper',
            yref='paper',
            showarrow=False,
        )
    if county_share is not None:
        figure.add_vline(
            x=county_share,
            line_dash='dash',
            line_color='#2563eb',
            annotation_text=f'{line_label}: {county_share:.1f}%',
            annotation_position='top right',
        )
    figure.update_layout(
        title=f'{display_contest_name(contest)} — Precinct distribution',
        bargap=0,
        xaxis={'title': x_title, 'ticksuffix': '%'},
        yaxis={'title': 'Number of precincts', 'rangemode': 'tozero'},
        paper_bgcolor='white',
        plot_bgcolor='white',
        margin={'l': 60, 'r': 30, 't': 80, 'b': 55},
        showlegend=False,
    )
    return figure


default_page = CONTEST_PAGES.get('Governor (Vote For 1)', next(iter(CONTESTS)))
default_choices, _ = load_contest(default_page)
precinct_cross_reference = load_precinct_cross_reference(
    PRECINCT_CROSS_REFERENCE, PRECINCT_CROSS_REFERENCE_PARQUET
)


def contest_cross_reference(page):
    _, rows = load_contest(page)
    precincts = [display_precinct_id(row['Precinct']) for row in rows]
    return (
        precinct_cross_reference.filter(pl.col('Voting Precinct').is_in(precincts))
        .sort('Regular Precinct')
        .to_dicts()
    )


app = dash.Dash(__name__)
app.title = 'Santa Clara County Election Results'
app.layout = html.Div(
    [
        html.H1(
            'General Election Results for Santa Clara County, November 2026',
            style={'margin': '0 0 6px'}
        ),
        html.P(
            [
                f'Election results released on {results_date_label} by '
                f'the Santa Clara County Registrar of Voters. '
                f'{VOTES_REMAINING:,d} votes remain to be counted.',
            ],
            style={'color': '#475569', 'margin': '0 0 20px'},
        ),
        html.Div(
            [
                html.Div(
                    [
                        html.Label('Contest', htmlFor='contest-selector'),
                        dcc.Dropdown(
                            id='contest-selector',
                            options=[
                                {'label': display_contest_name(contest), 'value': page}
                                for page, contest in CONTESTS.items()
                            ],
                            value=default_page,
                            clearable=False,
                        ),
                    ],
                    style={'flex': '2', 'minWidth': '280px'},
                ),
                html.Div(
                    [
                        html.Label('Select a Candidate', htmlFor='map-choice'),
                        dcc.Dropdown(
                            id='map-choice',
                            options=[
                                {'label': choice, 'value': choice}
                                for choice in default_choices
                            ],
                            value=default_choices[0],
                            clearable=False,
                        ),
                    ],
                    style={'flex': '1', 'minWidth': '220px'},
                ),
                html.Div(
                    [
                        html.Label('Pick a Map style', htmlFor='map-style'),
                        dcc.Dropdown(
                            id='map-style',
                            options=[
                                {'label': style, 'value': style}
                                for style in MAP_STYLES
                            ],
                            value='carto-positron',
                            clearable=False,
                        ),
                    ],
                    style={'flex': '1', 'minWidth': '220px'},
                ),
            ],
            style={'display': 'flex', 'gap': '16px', 'flexWrap': 'wrap'},
        ),
        html.Div(
            id='metric-cards',
            style={
                'display': 'flex',
                'gap': '12px',
                'flexWrap': 'wrap',
                'margin': '18px 0',
            },
        ),
        dcc.Graph(id='precinct-map', config={'displayModeBar': True}),
        html.Div(
            [
                dcc.Graph(
                    id='blueness-scatter',
                    config={'displayModeBar': True},
                    style={'flex': '1 1 calc(50% - 6px)', 'minWidth': 'min(100%, 420px)'},
                ),
                dcc.Graph(
                    id='blueness-heatmap',
                    config={'displayModeBar': True},
                    style={'flex': '1 1 calc(50% - 6px)', 'minWidth': 'min(100%, 420px)'},
                ),
                dcc.Graph(
                    id='vote-type-chart',
                    config={'displayModeBar': True},
                    style={'flex': '1 1 calc(50% - 6px)', 'minWidth': 'min(100%, 420px)'},
                ),
                dcc.Graph(
                    id='precinct-distribution',
                    config={'displayModeBar': True},
                    style={'flex': '1 1 calc(50% - 6px)', 'minWidth': 'min(100%, 420px)'},
                ),
            ],
            style={'display': 'flex', 'flexWrap': 'wrap', 'gap': '12px'},
        ),
        html.Div(
            [
                html.Div(
                    [
                        html.H2('Precinct results', style={'margin': '24px 0 12px'}),
                        dag.AgGrid(
                            id='results-grid',
                            columnDefs=[],
                            rowData=[],
                            defaultColDef={'minWidth': 1},
                            dashGridOptions={
                                'pagination': True,
                                'paginationPageSize': 25,
                                'animateRows': False,
                            },
                            columnSize='responsiveSizeToFit',
                            style={'height': '520px', 'width': '95%'},
                        ),
                    ],
                    style={'flex': '1 1 600px', 'minWidth': 0},
                ),
                html.Div(
                    [
                        html.H2('Precinct cross-reference', style={'margin': '24px 0 12px'}),
                        dag.AgGrid(
                            id='precinct-cross-reference-grid',
                            columnDefs=[
                                {
                                    'field': 'Regular Precinct',
                                    'headerName': 'Regular Precinct',
                                    'wrapHeaderText': True,
                                    'autoHeaderHeight': True,
                                },
                                {
                                    'field': 'Voting Precinct',
                                    'headerName': 'Consolidated Voting Precinct',
                                    'wrapHeaderText': True,
                                    'autoHeaderHeight': True,
                                },
                            ],
                            rowData=contest_cross_reference(default_page),
                            defaultColDef={
                                'filter': 'agTextColumnFilter',
                                'floatingFilter': True,
                                'sortable': True,
                                'minWidth': 140,
                            },
                            dashGridOptions={
                                'pagination': True,
                                'paginationPageSize': 25,
                                'animateRows': False,
                            },
                            columnSize='responsiveSizeToFit',
                            style={'height': '520px', 'width': '100%'},
                        ),
                    ],
                    style={'flex': '0 1 340px', 'minWidth': '280px'},
                ),
            ],
            style={'display': 'flex', 'flexWrap': 'wrap', 'gap': '16px'},
        ),
        html.P(
            'Election results and precinct shape files provided by Santa Clara County '
            'Registrar of Voters. This dashboard created by Mike Purtell.',
            style={'color': '#475569', 'margin': '24px 0 0'},
        ),
    ],
    style={
        'fontFamily': 'Arial, sans-serif',
        'color': '#0f172a',
        'background': '#f8fafc',
        'padding': '24px',
        'maxWidth': '1500px',
        'margin': '0 auto',
    },
)


@app.callback(
    Output('precinct-cross-reference-grid', 'rowData'),
    Input('contest-selector', 'value'),
)
def update_precinct_cross_reference(page):
    return contest_cross_reference(page)


@app.callback(
    Output('map-choice', 'options'),
    Output('map-choice', 'value'),
    Input('contest-selector', 'value'),
)
def update_map_choices(page):
    choices, _ = load_contest(page)
    return (
        [{'label': choice, 'value': choice} for choice in choices],
        choices[0],
    )


@app.callback(
    Output('precinct-map', 'figure'),
    Output('metric-cards', 'children'),
    Output('results-grid', 'columnDefs'),
    Output('results-grid', 'rowData'),
    Input('contest-selector', 'value'),
    Input('map-choice', 'value'),
    Input('map-style', 'value'),
    State('precinct-map', 'figure'),
)
def update_dashboard(page, selected_choice, map_style, current_figure):
    return build_dashboard(page, selected_choice, map_style, current_figure)


@app.callback(
    Output('blueness-scatter', 'figure'),
    Input('contest-selector', 'value'),
    Input('map-choice', 'value'),
)
def update_blueness_scatter(page, selected_choice):
    return build_partisanship_scatter(page, selected_choice, BECERRA, '#2563eb')


@app.callback(
    Output('blueness-heatmap', 'figure'),
    Input('contest-selector', 'value'),
    Input('map-choice', 'value'),
)
def update_blueness_heatmap(page, selected_choice):
    return build_partisanship_heatmap(page, selected_choice, BECERRA)


@app.callback(
    Output('vote-type-chart', 'figure'),
    Input('contest-selector', 'value'),
)
def update_vote_type_chart(page):
    return build_vote_type_chart(page)


@app.callback(
    Output('precinct-distribution', 'figure'),
    Input('contest-selector', 'value'),
    Input('map-choice', 'value'),
)
def update_precinct_distribution(page, selected_choice):
    return build_precinct_distribution(page, selected_choice)


if __name__ == '__main__':
    app.run(debug=True)
