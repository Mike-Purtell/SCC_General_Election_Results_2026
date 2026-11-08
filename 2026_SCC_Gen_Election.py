import polars as pl
import polars.selectors as cs
import plotly.express as px
import dash
# import dash_core_components as dcc

source_data = 'detail_2026_11_07.xlsx'
# read table of contents from Excel file to for mapping 
# sheet number to election
def make_election_dict(dict_elections):
     # Convert to dictionary
    keys = dict_elections['Page'].to_list()
    values = dict_elections['Contest'].to_list()
    my_dict = dict(zip(keys, values))

    return my_dict

# make dictionary for mapping contest to sheet name
df_dict_elections = (
    pl.read_excel(
        source_data, 
        sheet_name='Table of Contents',
        read_options={'header_row': 3}
    )
)

dict_elections = dict(zip(
    df_dict_elections['Contest'].to_list(), # keys
    df_dict_elections['Page'].to_list(),    # values
))

print(dict_elections)

# enter sheet number to get election name and its data
# TODO: replace hard coded sheet number with dashboard selector

contest = 'Registered Voters'  # example contest
contest = 'Palo Alto Unified SD (Vote For 2)'  # example contest
contest = 'City of San José Council, District 9 (Vote For 1)'  # example contest
contest = 'Registered Voters'  # example contest
sheet_number = 2  # example sheet number

sheet_name = dict_elections.get(contest)
print('contest: ', contest)
print('sheet_name: ', sheet_name)
if contest == 'Registered Voters':
    header_row = 0
    sheet_name = 'Registered Voters'
else:
    header_row=2
df = (
    pl.read_excel(
        source_data,
        sheet_name=str(sheet_name),
        read_options={'header_row': header_row}
    )
    .filter(pl.col('Precinct')!='Total:')
    .with_columns(cs.integer().cast(pl.UInt32))
)
choice_headers = (
    pl.read_excel(
        source_data,
        sheet_name=str(sheet_name),
        read_options={'header_row': header_row - 1}
    ).columns
    if header_row > 0
    else []
)
total_vote_names = {}
for index, column in enumerate(df.columns):
    if column == 'Total Votes' or column.startswith('Total Votes_'):
        choice_index = index - 2
        if choice_index < len(choice_headers):
            choice = choice_headers[choice_index]
            if choice.upper() in {'YES', 'NO'}:
                choice = choice.title()
            total_vote_names[column] = choice

df = df.drop([
    column for column in df.columns
    if column in {'In Person', 'Vote By Mail'}
    or column.startswith(('In Person_', 'Vote By Mail_'))
]).rename(total_vote_names)
print(df)
for contest in dict_elections.keys():
    print(contest)