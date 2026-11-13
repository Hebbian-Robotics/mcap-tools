//! Generic grouped MCAP writing and standard message encodings.
pub mod messages;
mod writer;
pub use mcap;
pub use writer::{GroupedWriter, WriterOptions};
