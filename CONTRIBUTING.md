# Contributing

Install Rust 1.92+ and uv. Run `uv sync` to build the Python extension locally.
Rust changes require rebuilding the extension with `uv run maturin develop`.

Validate with:

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test
uv run ruff check --fix
uv run ruff format
uv run ty check
uv run pytest
```

Keep Python >=3.11 support and typed public APIs. Test observable behavior using
standard MCAP readers rather than asserting Rust/Python implementation details.
Contributions are distributed under Apache-2.0.
