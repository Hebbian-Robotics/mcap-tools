# Agent guide

Read CONTRIBUTING.md. Keep this repository generic and independent of its consumers.
The Rust crate owns MCAP grouping and serialization; Python adapts streams and
atomic path publication. Do not duplicate the file-format implementation in Python.
Use descriptive names and outcome tests with real MCAP readers.
