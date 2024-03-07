#!/usr/bin/awk -f
# we parse the following type of logs
#2024-03-06 16:15:10,744 43 INFO contec-bv-16-0-sandbox-odoo-12107614 werkzeug: 109.136.147.34 - - [06/Mar/2024 16:15:10] "GET /web?debug=1 HTTP/1.0" 200 - 117 0.057 2.290
#2024-03-06 16:15:10,982 43 INFO contec-bv-16-0-sandbox-odoo-12107614 werkzeug: 109.136.147.34 - - [06/Mar/2024 16:15:10] "POST /websocket/update_bus_presence HTTP/1.1" 200 - 22 0.013 0.183
#2024-03-06 16:15:10,997 43 INFO contec-bv-16-0-sandbox-odoo-12107614 werkzeug: 109.136.147.34 - - [06/Mar/2024 16:15:10] "POST /websocket/peek_notifications HTTP/1.1" 200 - 8 0.004 0.006
# 2024-03-06 16:29:31,123 574 DEBUG ? odoo.sql_db: create serialized cursor to {'database': 'contec-bv-16-0-sandbox-odoo-12107614', 'application_name': 'WorkerOdooHTTP-WJsDxFkpvr odoo-574', 'host': '192.168.1.1', 'port': 5432, 'sslmode': 'prefer'}
# 2024-03-06 16:29:31,123 574 DEBUG ? odoo.sql_db.connection: ConnectionPool(used=1/count=1/max=20) Borrow existing connection to "application_name='WorkerOdooHTTP-WJsDxFkpvr odoo-574' host=192.168.1.1 port=5432 sslmode=prefer dbname=contec-bv-16-0-sandbox-odoo-12107614" at index 0
# 2024-03-06 16:29:31,126 574 DEBUG ? odoo.sql_db: [2.590 ms] query: SELECT sequence_name FROM information_schema.sequences WHERE sequence_name='base_registry_signaling'
# 2024-03-06 16:29:31,127 574 DEBUG ? odoo.sql_db: [0.587 ms] query:  SELECT base_registry_signaling.last_value,
#                                   base_cache_signaling.last_value
#                            FROM base_registry_signaling, base_cache_signaling
# 2024-03-06 16:29:31,128 574 DEBUG ? odoo.sql_db: SUM from:0:00:00/2 [4]
# 2024-03-06 16:29:31,129 574 DEBUG ? odoo.sql_db: SUM into:0:00:00/2 [4]

# whate we need to extract is the `query` statements (they have log level DEBUG and have query in the message)
# we need to keep all the lines following that statement until a new one with a date, datetime and an integer is found
# This is meant to be run using the following command
# ssh $WHATEVER  "bash -s" < ./fetch_sql_logs.sh  > $OUT_FILE
# this can be then analyzed using the following command
# pgbadfer $OUT_FILE -f stderr
zcat -f ~/logs/odoo* | awk '
BEGIN {
    query = 0
}
($4 == "INFO" || ($4 == "DEBUG" && $9 != "query:")) {
    query = 0
}($4 == "DEBUG" && $6 == "odoo.sql_db:" && $9 == "query:"){
    query = 1
}(query == 1) {
    if ($4 == "DEBUG" && $6 == "odoo.sql_db:" && $9 == "query:") {
        # replace the "," with "." from time
        datetime = $1 " " $2
        gsub(/,/, ".", datetime)
        duration = $7 " " $8
        gsub(/\[/, "", duration)
        gsub(/\]/, "", duration)
        printf("%s [1]: LOG:  duration: %s  statement: ",datetime, duration)
        # print the line after index 10
        for (i = 10; i <= NF; i++) {
            printf("%s ", $i)
        }
        printf("\n")
    } else {
        print($0)
    }
}
'
