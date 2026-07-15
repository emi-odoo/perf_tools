#!/usr/bin/awk -f
# other version of fetch_sql_logs.sh
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