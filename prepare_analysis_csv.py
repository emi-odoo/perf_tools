import pandas as pd

# read `analysis.csv` file
# this file is a whitespace separated file
# with no header and the following columns:
# - date
# - datetime
# - url
# - query_count
# - query_time (time to execute the query)
# - remaining_time (this + query_time = total time to execute the call)

# read the file
df = pd.read_csv(
    "analysis.csv", sep=" ", header=None, names=["date", "datetime", "url", "query_count", "query_time", "remaining_time"]
)
df["count"] = 1
# remove whatever comes after `?` in the url
df["url"] = df["url"].str.split("?").str[0]

# prepare a file prepare_analysis.csv
# this file is a comma separated file, with header and that will
# provide the grouped data based on the url and date
# the file should have the following columns:
# - date
# - url
# - query_count
# - query_time
# - remaining_time
# - total_time (query_time + remaining_time)
# - average_time (total_time / #n of rows with the same url)

# group the data based on the url and date
grouped_df = df.groupby(["date", "url"]).agg(
    {"query_count": ["sum", "median"], "query_time": ["sum", "median"], "remaining_time": ["sum", "median"], "count": "size"}
)
# flatten the columns
grouped_df.columns = ["_".join(x) for x in grouped_df.columns.ravel()]
# calculate the total_time
grouped_df["total_time"] = grouped_df["query_time_sum"] + grouped_df["remaining_time_sum"]
# filter out the urls that were called less than 10 times
grouped_df = grouped_df[grouped_df["count_size"] > 10]

# output the file
grouped_df.to_csv("prepare_analysis.csv")
