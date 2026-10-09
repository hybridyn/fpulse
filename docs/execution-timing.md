# Execution elapsed time

Execution history uses recorded completed_at minus started_at, not the sum of
step durations. Manual and scheduled runs refresh completion time at the end of
server-side finalization. This includes their result processing and notification
work before that final timestamp; the final history-write latency is excluded.
Replay records include their recorded execution boundary and errors now receive
a completion timestamp.

This is not click-to-browser-render time. Network latency, UI polling/rendering,
and any delay before a start timestamp is recorded cannot be inferred. Queue wait
before that timestamp requires its own measured admission timestamp.

Step durations remain unchanged. Lists, details and average duration use the same
normalized execution measurement. Running/queued jobs do not enter the average.
Metadata retains original_reported_duration_ms and duration_basis.

Existing records are normalized on read without a database migration. Missing,
invalid or reversed timestamps retain the original reported duration with
duration_basis=legacy_reported. Historical time never captured cannot be recovered.
