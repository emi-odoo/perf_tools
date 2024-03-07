#!/bin/bash
# This is meant to be run using the following command
# ssh $WHATEVER  "bash -s" < ./fetch_sql_logs.sh  > $OUT_FILE
zcat ~/logs/odoo* | awk '($12 == "\"POST" || $12 == "\"GET"){print $1,$2,$13, $17, $18, $19}'
