//! Canary eligibility is an operator-signed attestation from the Shadow review.
use crate::{Market, Result, Venue};
use ed25519_dalek::{Signature, Verifier, VerifyingKey};
use serde::Deserialize;
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};

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
    account_key_ids: BTreeMap<Venue, String>,
    ledger_path: PathBuf,
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
            || doc.account_key_ids.is_empty()
            || !doc.ledger_path.is_absolute()
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

    pub fn active_genomes<'a>(&'a self, market: &'a Market, now: u64) -> impl Iterator<Item = &'a str> + 'a {
        self.grants.iter().filter(move |g| now < g.valid_until_ns && g.markets.contains(market)).map(|g| g.genome_id.as_str())
    }

    pub fn kalshi_zero_fee(&self, market: &Market) -> bool {
        self.kalshi_zero_fee_markets.contains(&market.id)
    }

    pub fn matches_account<'a>(&'a self, ledger: &Path, keys: impl Iterator<Item = (Venue, &'a str)>) -> bool {
        ledger == self.ledger_path
            && keys.collect::<BTreeMap<_, _>>() == self.account_key_ids.iter().map(|(&venue, id)| (venue, id.as_str())).collect()
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{
        netting::{Netter, Signal},
        Kind,
    };
    use ed25519_dalek::{Signer, SigningKey};
    use serde_json::json;

    #[test]
    fn verified_canary_roster_requires_all_active_genomes_at_one_clock() {
        let key = SigningKey::from_bytes(&[17; 32]);
        let market = Market { venue: Venue::Pmus, id: "btc-up".into() };
        let rows = ["a".repeat(64), "b".repeat(64)];
        let raw = serde_json::to_vec(&json!({
            "schema":"doppel.canary-approval/v1", "account_scope":"isolated", "account_key_ids":{"pmus":"fixture"},
            "ledger_path":"/fixture/oms.jsonl", "start_flat_verified":true, "issued_ns":900,
            "valid_until_ns":2000, "grants":rows.iter().map(|g| json!({"genome_id":g,"markets":[market],
                   "shadow_receipt_sha256":"e".repeat(64),"e_value":20.0,"shadow_windows":40,"valid_until_ns":1900})).collect::<Vec<_>>()
        }))
        .unwrap();
        let signature = key.sign(&[DOMAIN, &raw].concat());
        let approval = Approval::verify(&raw, &signature.to_bytes(), &key.verifying_key(), 1000).unwrap();
        assert!(approval.matches_account(Path::new("/fixture/oms.jsonl"), [(Venue::Pmus, "fixture")].into_iter()));
        assert!(!approval.matches_account(Path::new("/fixture/other"), [(Venue::Pmus, "fixture")].into_iter()));
        let mut netter = Netter::default();
        for (i, g) in rows.iter().enumerate() {
            assert!(netter.upsert(
                Signal {
                    genome_id: g.clone(),
                    market: market.clone(),
                    p_hat: 0.5,
                    epistemic_var: 0.1,
                    target_contracts: 0,
                    shadow_expected_price_bps: Some(5000),
                    kind: Kind::Maker,
                    ts_ns: 1000
                },
                1000
            ));
            assert_eq!(netter.complete(&market, 1000, approval.active_genomes(&market, 1000)), i == 1);
        }
        assert!(Approval::verify(&raw, &signature.to_bytes(), &key.verifying_key(), 2000).is_err());
    }
}
