# Windows sandbox test fixtures

## Symptom
SQLite fixtures failed with `unable to open database file`; temporary-directory
cleanup then reported `WinError 5` under the sandbox's `AC/Temp` directory.

## Failed approaches
Rerunning the full suite and separate focused suites with the default temp
location reproduced permission errors unrelated to the application change.

## Fix
Use a project-root `TemporaryDirectory` for new isolated unit fixtures. Existing
HTTP/subprocess suites run with the required local-server permissions; enable
`PYTHONUTF8=1` for repository source reads on Windows.

## Signal
Check the fixture's absolute database path before debugging SQLite or schema code.
