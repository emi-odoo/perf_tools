"""Shared vocabulary: which ORM methods read/write/chain, severity ranks."""

SEVERITIES = ("info", "warning", "error")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# ORM methods that hit the database to read
QUERY_METHODS = {
    "search", "search_count", "search_read", "search_fetch", "_search",
    "read_group", "_read_group", "next_by_code", "execute",
}
# ORM methods that write
WRITE_METHODS = {"write", "create", "unlink", "copy"}
# methods that return a recordset derived from their receiver
CHAIN_METHODS = {
    "sudo", "with_context", "with_user", "with_company", "with_env",
    "with_prefetch", "exists", "browse", "filtered", "filtered_domain",
    "sorted", "search", "search_fetch", "create", "copy",
}
SEARCHY = {"search", "search_fetch"}
# cache flush/invalidation — batching killers when called per iteration
FLUSH_METHODS = {
    "flush_all", "flush_model", "flush_recordset",
    "invalidate_all", "invalidate_model", "invalidate_recordset",
    "invalidate_cache", "clear_caches", "clear_cache",
}
# receiver methods whose lambda argument runs once per record
PER_RECORD_LAMBDA = {"filtered", "mapped", "sorted"}
DOMAIN_METHODS = {
    "search", "search_count", "search_read", "search_fetch", "_search",
    "read_group", "_read_group",
}
X2MANY = {"One2many", "Many2many"}
RELATIONAL = {"Many2one", "One2many", "Many2many"}
# operators for which a btree index actually helps (SD302)
INDEXABLE_OPS = {"=", "!=", "in", "not in", "<", "<=", ">", ">=", "=?"}
# low-cardinality field types where an index is rarely worth flagging
LOW_CARDINALITY = {"Selection", "Boolean"}
