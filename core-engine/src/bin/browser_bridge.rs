//! Chrome Native Messaging Host: 只负责通过用户私有 Unix Socket 转发扩展消息。

use std::{
    io::{self, BufRead, BufReader, Read, Write},
    path::PathBuf,
};

use memory_bread_core::browser_extension::bounded_browser_bridge_socket_path;
use serde_json::Value;

const MAX_MESSAGE_BYTES: usize = 1024 * 1024;
const BRIDGE_SOCKET_ENV: &str = "MEMORY_BREAD_BROWSER_BRIDGE_SOCKET";

fn read_message(reader: &mut impl Read) -> io::Result<Option<Value>> {
    let mut length = [0_u8; 4];
    match reader.read_exact(&mut length) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(error) => return Err(error),
    }
    let length = u32::from_le_bytes(length) as usize;
    if length == 0 || length > MAX_MESSAGE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "native message too large",
        ));
    }
    let mut bytes = vec![0_u8; length];
    reader.read_exact(&mut bytes)?;
    serde_json::from_slice(&bytes)
        .map(Some)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))
}

fn write_message(writer: &mut impl Write, value: &Value) -> io::Result<()> {
    let bytes = serde_json::to_vec(value)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    if bytes.len() > MAX_MESSAGE_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "native response too large",
        ));
    }
    writer.write_all(&(bytes.len() as u32).to_le_bytes())?;
    writer.write_all(&bytes)?;
    writer.flush()
}

#[cfg(unix)]
fn socket_candidates() -> Vec<PathBuf> {
    if let Some(path) = std::env::var_os(BRIDGE_SOCKET_ENV) {
        return vec![bounded_browser_bridge_socket_path(PathBuf::from(path))];
    }
    let home = PathBuf::from(std::env::var_os("HOME").unwrap_or_else(|| ".".into()));
    vec![
        home.join("Library")
            .join("Application Support")
            .join("com.memory-bread.app")
            .join("runtime")
            .join(".memory-bread")
            .join("browser-bridge.sock"),
        home.join(".memory-bread").join("browser-bridge.sock"),
    ]
    .into_iter()
    .map(bounded_browser_bridge_socket_path)
    .collect()
}

#[cfg(unix)]
fn connect_core() -> io::Result<std::os::unix::net::UnixStream> {
    let mut last_error = None;
    for path in socket_candidates() {
        match std::os::unix::net::UnixStream::connect(path) {
            Ok(stream) => return Ok(stream),
            Err(error) => last_error = Some(error),
        }
    }
    Err(last_error.unwrap_or_else(|| {
        io::Error::new(io::ErrorKind::NotFound, "browser bridge socket unavailable")
    }))
}

#[cfg(unix)]
fn main() -> io::Result<()> {
    let core = connect_core()?;
    let mut core_reader = BufReader::new(core.try_clone()?);
    let mut core_writer = core;
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut reader = stdin.lock();
    let mut writer = stdout.lock();
    while let Some(message) = read_message(&mut reader)? {
        serde_json::to_writer(&mut core_writer, &message)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        core_writer.write_all(b"\n")?;
        core_writer.flush()?;
        let mut response_bytes = Vec::new();
        core_reader.read_until(b'\n', &mut response_bytes)?;
        if response_bytes.len() > MAX_MESSAGE_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "core bridge response too large",
            ));
        }
        let response = serde_json::from_slice::<Value>(&response_bytes)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        write_message(&mut writer, &response)?;
    }
    Ok(())
}

#[cfg(not(unix))]
fn main() -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "Chrome browser bridge currently requires Unix sockets",
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn native_message_round_trip_uses_little_endian_length() {
        let mut bytes = Vec::new();
        write_message(&mut bytes, &json!({"type": "poll"})).unwrap();
        let decoded = read_message(&mut bytes.as_slice()).unwrap().unwrap();
        assert_eq!(decoded["type"], "poll");
    }

    #[test]
    fn oversized_message_is_rejected_before_allocation() {
        let bytes = ((MAX_MESSAGE_BYTES as u32) + 1).to_le_bytes().to_vec();
        assert!(read_message(&mut bytes.as_slice()).is_err());
    }
}
