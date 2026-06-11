# Industrial Data Transfer API

This project transfers raw data from a factory-side SQLite database to a receiver-side storage system. The main goal is to collect reliable raw data.


## Main workflow

```text
1. User opens the Receiver web application
        |
        v
2. User selects a request type
   Examples:
   - Sync New Data
   - Full Database
   - Selected Tables
   - Custom Query
   - Inspect Schema
        |
        v
3. Receiver API sends the request to the Factory Agent
        |
        v
4. Factory Agent connects to the SQLite database
        |
        v
5. Factory Agent inspects the database schema
   - Tables
   - Columns
   - Primary keys
   - Schema versions
        |
        v
6. Factory Agent reads the required rows
   - Only new rows for incremental sync
   - Full table content for full refresh
   - Filtered rows for custom query
        |
        v
7. Factory Agent prepares transferable files
   - Converts data to Parquet
   - Adds metadata
   - Calculates SHA256 checksum
   - Splits large transfers into numbered part files
        |
        v
8. Factory Agent uploads files to Receiver API
        |
        v
9. Receiver verifies each uploaded file
   - Validates metadata
   - Checks checksum
   - Rejects corrupted files
        |
        v
10. Receiver stores the result
   - Parquet files are saved in structured folders
   - Metadata is saved next to the files
   - Metadata is also registered in the Receiver database
        |
        v
11. Receiver UI shows the result
   - Total received rows
   - Number of part files
   - File names
   - Storage paths
   - Checksums
   - Repair or warning messages if needed
```

## Project parts

```text
industrial-data-transfer-api/
  factory_agent/    Reads the factory SQLite database and sends exported data
  receiver_api/     Provides the web UI, receives files, verifies them, and stores them
```


## What the system handles

- Requesting only new data.
- Requesting full database snapshots.
- Requesting selected tables.
- Running controlled custom queries.
- Inspecting schema without transferring row data.
- Detecting schema changes.
- Splitting large transfers into readable part files.
- Verifying file integrity with checksums.
- Detecting missing files in Receiver storage.
- Repairing missing files when possible.
