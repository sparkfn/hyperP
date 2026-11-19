use base64::{
    engine::general_purpose::{STANDARD, URL_SAFE_NO_PAD},
    Engine,
};
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use sha2::Sha256;

const DOMAIN: &[u8] = b"hyperp-rust-spike:identity-link-cursor:v1\0";
type CursorMac = Hmac<Sha256>;

// Snapshot continuation stores the public event ID rather than the private link key.
// The reader resolves it to its key server-side at the frozen revision.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
pub enum IdentityCursor {
    #[serde(rename = "events")]
    Events {
        cursor_version: u8,
        after_revision: i64,
        through_revision: i64,
    },
    #[serde(rename = "snapshot")]
    Snapshot {
        cursor_version: u8,
        snapshot_revision: i64,
        after_event_id: String,
    },
}

impl IdentityCursor {
    fn valid(&self) -> bool {
        match self {
            Self::Events {
                cursor_version,
                after_revision,
                through_revision,
            } => *cursor_version == 1 && *after_revision >= 0 && after_revision < through_revision,
            Self::Snapshot {
                cursor_version,
                snapshot_revision,
                after_event_id,
            } => *cursor_version == 1 && *snapshot_revision >= 0 && !after_event_id.is_empty(),
        }
    }
}

pub fn encode_identity(cursor: &IdentityCursor, key: &[u8]) -> Result<String, ()> {
    if !cursor.valid() || key.is_empty() {
        return Err(());
    }
    let payload = serde_json::to_vec(cursor).map_err(|_| ())?;
    let mut mac = CursorMac::new_from_slice(key).map_err(|_| ())?;
    mac.update(DOMAIN);
    mac.update(&payload);
    let signature = mac.finalize().into_bytes();
    Ok(format!(
        "{}.{}",
        URL_SAFE_NO_PAD.encode(payload),
        URL_SAFE_NO_PAD.encode(signature)
    ))
}

pub fn decode_identity(token: &str, key: &[u8]) -> Result<IdentityCursor, ()> {
    if token.len() > 4096 || key.is_empty() {
        return Err(());
    }
    let (payload, signature) = token.split_once('.').ok_or(())?;
    let payload = URL_SAFE_NO_PAD.decode(payload).map_err(|_| ())?;
    let signature = URL_SAFE_NO_PAD.decode(signature).map_err(|_| ())?;
    let mut mac = CursorMac::new_from_slice(key).map_err(|_| ())?;
    mac.update(DOMAIN);
    mac.update(&payload);
    mac.verify_slice(&signature).map_err(|_| ())?;
    let cursor: IdentityCursor = serde_json::from_slice(&payload).map_err(|_| ())?;
    if !cursor.valid() {
        return Err(());
    }
    Ok(cursor)
}

pub fn encode_offset(offset: i64) -> String {
    STANDARD.encode(offset.to_string())
}

pub fn decode_offset(value: Option<&str>) -> Result<i64, ()> {
    let Some(value) = value.filter(|v| !v.is_empty()) else {
        return Ok(0);
    };
    if value.len() > 32 {
        return Err(());
    }
    let decoded = STANDARD.decode(value).map_err(|_| ())?;
    let text = std::str::from_utf8(&decoded).map_err(|_| ())?;
    let offset: i64 = text.parse().map_err(|_| ())?;
    if offset < 0 {
        return Err(());
    }
    Ok(offset)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cursor_is_kind_specific_authenticated_and_preserves_bounds() {
        let cursor = IdentityCursor::Snapshot {
            cursor_version: 1,
            snapshot_revision: 9,
            after_event_id: "event-2".into(),
        };
        let encoded = encode_identity(&cursor, b"secret").unwrap();
        assert_eq!(decode_identity(&encoded, b"secret"), Ok(cursor));
        assert!(decode_identity(&encoded, b"wrong").is_err());
        let mut tampered = encoded.into_bytes();
        tampered[0] = if tampered[0] == b'A' { b'B' } else { b'A' };
        assert!(decode_identity(std::str::from_utf8(&tampered).unwrap(), b"secret").is_err());
        assert_eq!(decode_offset(Some(&encode_offset(42))), Ok(42));
        assert!(decode_offset(Some(&encode_offset(-1))).is_err());
    }
}
