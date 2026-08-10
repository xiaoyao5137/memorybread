//! storage 模块公开 API
//!
//! 外部代码只需：
//! ```rust
//! use memory_bread_core::storage::{StorageManager, models::*, error::StorageError};
//! ```

pub mod cleanup;
pub mod db;
pub(crate) mod document_identity;
pub mod error;
pub mod fts;
pub mod models;
pub mod models_bake;
pub mod models_data;
pub mod models_integration;
pub mod repo;
pub mod snapshot;

pub use db::StorageManager;
pub use error::StorageError;
pub use models_bake::*;
pub use models_data::*;
pub use models_integration::*;
