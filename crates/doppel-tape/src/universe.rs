//! The recording universe: short-dated crypto up/down windows (Python `universe.py`).

use std::collections::HashMap;
use std::time::{Duration, Instant};

use futures_util::future::join_all;
use serde_json::Value;

use crate::clock::utc_parts;

pub const ASSETS: [&str; 4] = ["btc", "eth", "sol", "xrp"];
pub const HZ: [&str; 2] = ["5m", "15m"];
pub const GAMMA: &str = "https://gamma-api.polymarket.com";
pub const KALSHI_REST: &str = "https://api.elections.kalshi.com/trade-api/v2";
pub const KALSHI_SERIES: [&str; 4] = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"];

pub fn horizon_s(h: &str) -> i64 {
    match h {
        "5m" => 300,
        "15m" => 900,
        _ => panic!("unknown horizon {h}"),
    }
}

/// Start times of the current window plus `ahead` upcoming ones.
pub fn window_starts(now_s: f64, length_s: i64, ahead: i64) -> Vec<i64> {
    let cur = (now_s / length_s as f64).floor() as i64 * length_s;
    (0..=ahead).map(|i| cur + i * length_s).collect()
}

pub fn intl_slug(asset: &str, horizon: &str, start: i64) -> String {
    format!("{asset}-updown-{horizon}-{start}")
}

pub fn pmus_slug(asset: &str, horizon: &str, start: i64) -> String {
    let prefix = match asset {
        "btc" => "cpc-btc",
        "eth" => "cpceth-eth",
        "sol" => "cpcsol-sol",
        "xrp" => "cpcxrp-xrp",
        _ => panic!("unknown asset {asset}"),
    };
    let (y, m, d, hh, mm) = utc_parts(start);
    format!("{prefix}-updown-{horizon}-{y:04}-{m:02}-{d:02}-{hh:02}{mm:02}z")
}

#[derive(Debug, Clone)]
pub struct IntlWindow {
    pub slug: String,
    pub start: i64,
    pub end: i64,
    pub tokens: Vec<String>,
}

/// Resolves intl window slugs to CLOB token ids via Gamma, with positive and negative caching.
pub struct IntlUniverse {
    pub assets: Vec<String>,
    pub horizons: Vec<String>,
    pub ahead: i64,
    pub gamma: String,
    cache: HashMap<String, IntlWindow>,
    miss_at: HashMap<String, Instant>,
}

impl IntlUniverse {
    pub fn new(assets: &[&str], horizons: &[&str]) -> Self {
        Self {
            assets: assets.iter().map(|s| s.to_string()).collect(),
            horizons: horizons.iter().map(|s| s.to_string()).collect(),
            ahead: 1,
            gamma: GAMMA.to_string(),
            cache: HashMap::new(),
            miss_at: HashMap::new(),
        }
    }

    async fn fetch(client: &reqwest::Client, gamma: &str, slug: &str) -> Result<Option<Vec<String>>, String> {
        let r = client.get(format!("{gamma}/events")).query(&[("slug", slug)]).send().await.map_err(|e| e.to_string())?;
        if !r.status().is_success() {
            return Ok(None);
        }
        let v: Value = json_body(r).await?;
        let ids = v
            .get(0)
            .and_then(|e| e.get("markets"))
            .and_then(|m| m.get(0))
            .and_then(|m| m.get("clobTokenIds"))
            .and_then(Value::as_str)
            .and_then(|s| serde_json::from_str::<Vec<String>>(s).ok());
        Ok(ids)
    }

    pub async fn current(&mut self, client: &reqwest::Client, now_s: f64) -> Vec<IntlWindow> {
        let mut want = Vec::new();
        for h in &self.horizons {
            let l = horizon_s(h);
            for st in window_starts(now_s, l, self.ahead) {
                for a in &self.assets {
                    want.push((intl_slug(a, h, st), st, l));
                }
            }
        }
        let todo: Vec<_> = want
            .iter()
            .filter(|(s, _, _)| !self.cache.contains_key(s))
            .filter(|(s, _, _)| self.miss_at.get(s).is_none_or(|t| t.elapsed() >= Duration::from_secs(20)))
            .cloned()
            .collect();
        let gamma = self.gamma.clone();
        let res = join_all(todo.iter().map(|(s, _, _)| Self::fetch(client, &gamma, s))).await;
        for ((slug, st, l), r) in todo.into_iter().zip(res) {
            match r {
                Ok(Some(tokens)) if !tokens.is_empty() => {
                    self.cache.insert(slug.clone(), IntlWindow { slug, start: st, end: st + l, tokens });
                }
                _ => {
                    self.miss_at.insert(slug, Instant::now());
                }
            }
        }
        let out = want.iter().filter_map(|(s, _, _)| self.cache.get(s)).filter(|w| w.end as f64 > now_s).cloned().collect();
        self.cache.retain(|_, w| (w.end as f64) >= now_s - 60.0);
        out
    }
}

/// Parse a response body as JSON with a short error.
pub async fn json_body(r: reqwest::Response) -> Result<Value, String> {
    let b = r.bytes().await.map_err(|e| e.to_string())?;
    serde_json::from_slice(&b).map_err(|e| format!("json: {e}"))
}

/// Nearest open markets of each Kalshi series (public REST), refreshed every `refresh`.
pub struct KalshiUniverse {
    pub series: Vec<String>,
    pub base: String,
    pub per_series: usize,
    pub refresh: Duration,
    tickers: Vec<String>,
    last: HashMap<String, Vec<String>>,
    at: Option<Instant>,
}

impl KalshiUniverse {
    pub fn new(base: &str) -> Self {
        Self {
            series: KALSHI_SERIES.iter().map(|s| s.to_string()).collect(),
            base: base.to_string(),
            per_series: 2,
            refresh: Duration::from_secs(20),
            tickers: Vec::new(),
            last: HashMap::new(),
            at: None,
        }
    }

    pub fn cached(&self) -> &[String] {
        &self.tickers
    }

    pub fn due(&self) -> bool {
        self.at.is_none_or(|t| t.elapsed() >= self.refresh)
    }

    pub async fn tickers(&mut self, client: &reqwest::Client) -> Result<Vec<String>, String> {
        if !self.due() {
            return Ok(self.tickers.clone());
        }
        let mut found = Vec::new();
        for s in &self.series {
            let r = client
                .get(format!("{}/markets", self.base))
                .query(&[("series_ticker", s.as_str()), ("status", "open"), ("limit", "20")])
                .send()
                .await
                .map_err(|e| format!("markets: {}", crate::http::err_kind(&e)))?;
            if !r.status().is_success() {
                // e.g. 429: keep this series' last good listing (Python cached the gap for 20 s too)
                found.extend(self.last.get(s).cloned().unwrap_or_default());
                continue;
            }
            let v = json_body(r).await?;
            let mut mk: Vec<(String, String)> = v
                .get("markets")
                .and_then(Value::as_array)
                .map(|a| {
                    a.iter()
                        .filter_map(|m| {
                            let t = m.get("ticker")?.as_str()?.to_string();
                            let c = m.get("close_time").and_then(Value::as_str).unwrap_or("").to_string();
                            Some((c, t))
                        })
                        .collect()
                })
                .unwrap_or_default();
            mk.sort();
            let got: Vec<String> = mk.into_iter().take(self.per_series).map(|(_, t)| t).collect();
            self.last.insert(s.clone(), got.clone());
            found.extend(got);
        }
        self.tickers = found.clone();
        self.at = Some(Instant::now());
        Ok(found)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const T0: i64 = 1_795_622_400;

    #[test]
    fn windows_and_slugs() {
        assert_eq!(window_starts(T0 as f64 + 10.0, 300, 1), vec![T0, T0 + 300]);
        assert_eq!(intl_slug("btc", "5m", T0), "btc-updown-5m-1795622400");
        assert_eq!(pmus_slug("btc", "15m", T0), "cpc-btc-updown-15m-2026-11-25-1600z");
        assert_eq!(pmus_slug("eth", "5m", T0 + 6 * 3600 + 1200), "cpceth-eth-updown-5m-2026-11-25-2220z");
    }
}
