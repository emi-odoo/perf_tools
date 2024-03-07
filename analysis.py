import pandas as pd
import matplotlib.pyplot as plt

# read `prepare_analysis.csv` file
columns = [
    "date",
    "url",
    "query_count_sum",
    "query_count_median",
    "query_time_sum",
    "query_time_median",
    "remaining_time_sum",
    "remaining_time_median",
    "count_size",  # number of times the url was called
    "total_time",
]

# read the file
df = pd.read_csv("prepare_analysis.csv")
# convert `date` to date
df["date"] = pd.to_datetime(df["date"])
# don't take into account the urls that were called less than 10 times
df = df[df["count_size"] > 10]
# group by `url`
grouped_df = df.groupby("url").agg(
    {
        "query_count_sum": "sum",
        "query_count_median": "median",
        "query_time_sum": "sum",
        "query_time_median": "median",
        "remaining_time_sum": "sum",
        "remaining_time_median": "median",
        "count_size": "sum",
        "total_time": "sum",
    }
)
# 5 most called urls by `count_size`
top_urls = grouped_df.nlargest(5, "count_size")
# 5 most time consuming urls by `total_time`
top_time_consuming_urls = grouped_df.nlargest(5, "total_time")
# 5 most time consuming urls by `query_time_sum`
top_query_time_urls = grouped_df.nlargest(5, "query_time_sum")
# 5 most time consuming urls by `remaining_time_sum`
top_remaining_time_urls = grouped_df.nlargest(5, "remaining_time_sum")
# 5 most time consuming urls by `query_count_sum`
top_query_count_urls = grouped_df.nlargest(10, "query_count_sum")

print("Top 5 urls by count_size")
print(top_urls)
print("Top 5 urls by total_time")
print(top_time_consuming_urls)
print("Top 5 urls by query_time_sum")
print(top_query_time_urls)
print("Top 5 urls by remaining_time_sum")
print(top_remaining_time_urls)
print("Top 5 urls by query_count_sum")
print(top_query_count_urls)


# filter df to only include the top 5 urls by `count_size`
df = df[df["url"].isin(top_query_time_urls.index)]

plt.figure()
df.sort_values(by=["date"])
# Group data by type and plot each group
for name, group in df.groupby("url"):
    group = group.sort_values(by=["date"])
    plt.plot(group["date"], group["query_time_sum"], label=name)

plt.xlabel("Date")
plt.ylabel("Total Time")
plt.title("Total Time vs Date")
plt.legend()
plt.grid(True)
plt.show()
