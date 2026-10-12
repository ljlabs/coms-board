# Container temporary space can break read-only SQLite diagnostics

## Symptom
Inspecting a live job failed: Docker copy reported a read-only root filesystem, writing the helper to `/tmp` reported `No space left on device`, and a read-only SQLite query reported `database or disk is full`. `/data` still had ample space.

## Failed approaches
Copying into the read-only container through Docker did not reach the writable mount. Writing through `tee` reached `/tmp`, but that 128 MiB mount was full of extracted native libraries. Moving only the helper to persistent home still left SQLite sorting dependent on the full temporary mount.

## Fix
Run the saved diagnostic helper from writable persistent home and use `PRAGMA temp_store=MEMORY` on its read-only SQLite connection. This restored inspection; it did not repair the agent runtime's native-library accumulation. Remove the helper after collecting records.

## Signal
For `database or disk is full`, check `/tmp` as well as the database volume, including hidden files; successful HTTP health checks do not demonstrate healthy agent execution.
