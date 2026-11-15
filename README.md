# SCC General Election Results 2026

## Election dashboard

Install the Python dependencies and start the Dash app from this folder:

```powershell
python -m pip install -r requirements.txt
python app.py
```

Open the local address printed by Dash (usually `http://127.0.0.1:8050`). Choose a contest and a candidate or response to color the precinct map by vote share. Use the map style dropdown to select a built-in Plotly basemap; the default is OpenStreetMap. Most basemaps require an internet connection; white-bg does not. Precinct numbers appear on the map and in scatter plot hover details. The map hover shows precinct votes and turnout; summary cards and the precinct results grid update with the selected contest.

The subtitle includes the registrar's estimated unprocessed ballot total, linked to its source and labeled with the estimate's update time. This is a fixed snapshot, not a live feed; update `UNPROCESSED_BALLOTS` and `UNPROCESSED_BALLOTS_UPDATED` in `app.py` when incorporating a newer estimate.

Candidate and response cards show vote totals and their share of all votes cast in the selected contest. If the contest has no votes, the percentage is shown as N/A.

Precinct numbers are displayed as four digits, without the source data's three leading zeroes.

The precinct cross-reference grid beside the results table maps regular precincts to consolidated voting precincts using `e148 wmr008_votabsregpctxref.pdf`. It shows mappings for the voting precincts in the selected contest's results table, ordered by regular precinct. Use the floating filters to find either precinct number.

On first startup, the app extracts the cross-reference from the PDF and saves `precinct_cross_reference.parquet`. Subsequent startups read the Parquet file instead. Delete the Parquet file to rebuild it after updating the PDF.

The app also caches the projected precinct geometry in the operating system's temporary folder. The cache is reused on later starts and rebuilt automatically when any geodatabase file changes. Precinct boundaries are kept at their original detail.

Contest names omit the default “Vote For 0” and “Vote For 1” suffixes; higher vote limits remain visible. Selecting a contest centers and zooms the map to its displayed precinct boundaries.

Drag the map to pan. The map sends only the selected contest's precinct boundaries. Candidate and map-style changes reuse those exact boundaries; changing contests loads the new boundaries and resets the view.

The map uses a yellow-to-green color scale, with yellow indicating lower shares or turnout and green indicating higher values. It spans the 10th to 90th percentiles of displayed precinct shares (or turnout for the registered voters view). Values outside this range use the endpoint colors; hover values remain unchanged.

Contest results exclude precincts with zero total contest votes from the map, percentile calculation, and grid. The registered voters view continues to show all precincts.

The correlation with Governor-Elect Xavier Becerra scatter plot shows one point per precinct for the selected candidate or response. Its x-axis shows that choice's percentage of total contest votes in the precinct; its y-axis shows Xavier Becerra's governor votes divided by total governor contest votes. Each axis spans its plotted values with a 10% margin on each side. Marker area is proportional to precinct ballots cast. Precincts with zero votes for the selected candidate or response, missing necessary votes, no governor contest votes, or no ballots cast are omitted from the scatter plot and its fit. In the registered voters view, the x-axis shows voter turnout. The dashboard uses Polars rather than pandas.

The scatter plot includes a linear best-fit line and displays its slope, y-intercept, and Pearson correlation coefficient (r). For the fit, each precinct contributes once per vote for the selected candidate or response; in the registered voters view, it contributes once per ballot cast. These vote weights are calculated directly rather than expanding the data to one row per vote. Each plotted point still represents one precinct. The fit requires at least two distinct x values; otherwise the plot reports that it is unavailable.

The precinct-density heat map uses the same precincts and axis measures as the scatter plot. Each axis is initially zoomed to its 5th–95th percentile range; outliers remain in the data. It uses a white-to-green color scale to show the number of precincts in each bin, with zero shown in white and each precinct counted once.

The grouped bar chart shows countywide by-mail, in-person, and total votes for each candidate or response. All percentages use total contest votes as their denominator, so each choice's by-mail and in-person shares add up to its total share. Displayed percentages are rounded to one decimal place, with the in-person share adjusted when necessary so the displayed values add up. A contest with no votes shows N/A. The registered voters sheet has no vote-type breakdown, so the chart displays a message instead.

The precinct distribution histogram counts precincts in five-percentage-point ranges of the selected choice's vote share, including precincts with zero votes for that choice. Its dashed line marks the countywide choice votes divided by total contest votes, not the average of precinct percentages. In the registered voters view, it shows turnout instead, excludes precincts with no registered voters, and marks countywide turnout. Turnout above 100% remains visible.

The app reads `detail_2026_11_12.xlsx` and the `E148_Consolidated_Precinct` layer in `E148_Consolidated_Precinct.gdb`. The results date shown on the dashboard comes from the source filename, which must end in `YYYY_MM_DD` before the extension. Results are marked partial because counting is still underway.
