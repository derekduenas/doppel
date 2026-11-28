//! Canary eligibility is an operator-signed attestation from the Shadow review.
use crate::{Market, Result};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde::Deserialize;

const DOMAIN: &[u8] = b"DOPPEL-CANARY-APPROVAL/v1\0";

#[derive(Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct Grant {
    genome_id: String,
    markets: Vec<Market>,
    shadow_receipt_sha256: String,
    e_value: f64,
    shadow_windows: u32,
    valid_until_ns: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Approval {
    schema: String,
    account_scope: String,
    start_flat_verified: bool,
    issued_ns: u64,
    valid_until_ns: u64,
    grants: Vec<Grant>,
    #[serde(default)]
    kalshi_zero_fee_markets: Vec<String>,
}

impl Approval {
    pub fn verify(raw: &[u8], sig: &[u8], key: &VerifyingKey, now: u64) -> Result<Self> {
        if raw.len() > 65536 {
            return Err("approval too large".into());
        }
        key.verify(&[DOMAIN, raw].concat(), &Signature::from_slice(sig)?)?;
        let doc: Self = serde_json::from_slice(raw)?;
        let hex = |s: &str| s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit());
        let mut ids = std::collections::BTreeSet::new();
        if doc.schema != "doppel.canary-approval/v1"
            || doc.account_scope.is_empty()
            || !doc.start_flat_verified
            || doc.issued_ns > now
            || now >= doc.valid_until_ns
            || doc.grants.is_empty()
            || doc.grants.iter().any(|g| {
                !hex(&g.genome_id)
                    || !hex(&g.shadow_receipt_sha256)
                    || !ids.insert(&g.genome_id)
                    || !g.e_value.is_finite()
                    || g.e_value < 20.0
                    || g.shadow_windows < 40
                    || g.valid_until_ns > doc.valid_until_ns
                    || g.valid_until_ns <= now
                    || g.markets.is_empty()
                    || g.markets.iter().any(|m| !m.valid())
            })
        {
            return Err("canary approval is incomplete, ineligible or expired".into());
        }
        Ok(doc)
    }

    pub fn allows(&self, genome: &str, market: &Market, now: u64) -> bool {
        now >= self.issued_ns
            && now < self.valid_until_ns
            && self.grants.iter().any(|g| g.genome_id == genome && now < g.valid_until_ns && g.markets.contains(market))
    }

    pub fn kalshi_zero_fee(&self, market: &Market) -> bool {
        self.kalshi_zero_fee_markets.contains(&market.id)
    }

    pub fn account_scope(&self) -> &str {
        &self.account_scope
    }

    pub fn issued_ns(&self) -> u64 {
        self.issued_ns
    }

    pub fn valid(&self, now: u64) -> bool {
        now >= self.issued_ns && now < self.valid_until_ns
    }
}
