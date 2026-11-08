# SCC General Election Results 2026

## Election dashboard

Install the Python dependencies and start the Dash app from this folder:

```powershell
python -m pip install -r requirements.txt
python 2026_SCC_Gen_Election.py
```

Open the local address printed by Dash (usually `http://127.0.0.1:8050`). Choose a contest and a candidate or response to color the precinct map by vote share. Precinct numbers appear on the map and in scatter plot hover details. The map hover shows precinct votes and turnout; summary cards and the precinct results grid update with the selected contest.

Candidate and response cards show vote totals and their share of all votes cast in the selected contest. If the contest has no votes, the percentage is shown as N/A.

Precinct numbers are displayed as four digits, without the source data's three leading zeroes.

Contest names omit the default “Vote For 0” and “Vote For 1” suffixes; higher vote limits remain visible. Selecting a contest centers and zooms the map to its displayed precinct boundaries.

Drag the map to pan. The map sends only the selected contest's precinct boundaries to keep updates responsive; changing contests resets the view to the new precincts.

The color scale spans the 10th to 90th percentiles of displayed precinct shares (or turnout for the registered voters view). Values outside this range use the endpoint colors; hover values remain unchanged.

Contest results exclude precincts with zero total contest votes from the map, percentile calculation, and grid. The registered voters view continues to show all precincts.

The scatter plots show one point per precinct for the selected candidate or response. Their x-axes show that choice's percentage of total contest votes in the precinct. The left y-axis shows blueness (Xavier Becerra's governor votes divided by total governor contest votes), and the right y-axis shows redness (Steve Hilton's governor votes divided by the same total). Each axis spans its plotted values with a 10% margin on each side. Marker area is proportional to precinct ballots cast. Precincts with zero votes for the selected candidate or response, missing necessary votes, no governor contest votes, or no ballots cast are omitted from the scatter plots and their fits. In the registered voters view, the x-axes show voter turnout. The dashboard uses Polars rather than pandas.

Each scatter plot includes an unweighted linear best-fit line and displays its slope, y-intercept, and Pearson correlation coefficient (r). The fit requires at least two distinct x values; otherwise the plot reports that it is unavailable.

The app reads `detail_2026_11_07.xlsx` and the shapefile in `Precinct_Boundaries`. The results date shown on the dashboard comes from the source filename, which must end in `YYYY_MM_DD` before the extension. Results are marked partial because counting is still underway.
