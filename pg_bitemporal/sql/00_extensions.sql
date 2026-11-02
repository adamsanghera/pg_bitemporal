-- Exclusion constraints on the key need btree_gist, and record_txn_history and
-- split_record use hstore. Creating an extension needs CREATE on the database.
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS hstore;
