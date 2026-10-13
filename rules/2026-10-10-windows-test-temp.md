# Windows sandbox test fixtures

## Symptom
SQLite fixtures failed with `unable to open database file`; temporary-directory
cleanup also failed because the system temporary directory was not writable.

## Failed approaches
Rerunning the full suite and separate focused suites with the default temporary
location reproduced permission errors unrelated to the application change.

## Fix
Use a project-root `TemporaryDirectory` for new isolated unit fixtures. Existing
HTTP/subprocess suites need permission to bind a local server; enable
`PYTHONUTF8=1` for repository source reads on Windows.

## Signal
Check the fixture's absolute database path before debugging SQLite or schema code.
