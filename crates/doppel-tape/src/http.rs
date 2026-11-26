//! HTTP helpers: a shared token-bucket rate limiter and client builders.

use std::time::{Duration, Instant};

use tokio::sync::Mutex;

pub const UA: &str = "doppel-tape/0.1 (research recorder)";

/// Token bucket shared by pollers that hit one host (Python `RateLimiter`).
pub struct RateLimiter {
    rate: f64,
    burst: f64,
    state: Mutex<(f64, Instant)>,
}

impl RateLimiter {
    pub fn new(rate_per_s: f64, burst: u32) -> Self {
        Self { rate: rate_per_s, burst: burst as f64, state: Mutex::new((burst as f64, Instant::now())) }
    }

    pub async fn take(&self) {
        let mut g = self.state.lock().await;
        loop {
            let now = Instant::now();
            g.0 = (g.0 + now.duration_since(g.1).as_secs_f64() * self.rate).min(self.burst);
            g.1 = now;
            if g.0 >= 1.0 {
                g.0 -= 1.0;
                return;
            }
            tokio::time::sleep(Duration::from_secs_f64((1.0 - g.0) / self.rate)).await;
        }
    }
}

/// Plain client for public REST (redirects allowed, as aiohttp does).
pub fn client(timeout_s: u64) -> reqwest::Client {
    reqwest::Client::builder().user_agent(UA).timeout(Duration::from_secs(timeout_s)).build().expect("http client")
}

/// Client for signed requests: never follows redirects (auth headers must not be replayed).
pub fn signed_client(timeout_s: u64) -> reqwest::Client {
    reqwest::Client::builder()
        .user_agent(UA)
        .timeout(Duration::from_secs(timeout_s))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .expect("http client")
}

/// Error text without anything a server or a URL query could carry: the error kind only.
pub fn err_kind(e: &reqwest::Error) -> &'static str {
    if e.is_timeout() {
        "Timeout"
    } else if e.is_connect() {
        "Connect"
    } else if e.is_redirect() {
        "Redirect"
    } else if e.is_body() || e.is_decode() {
        "Body"
    } else if e.is_request() {
        "Request"
    } else {
        "Http"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn rate_limiter_paces() {
        let lim = RateLimiter::new(20.0, 1);
        let t = Instant::now();
        for _ in 0..5 {
            lim.take().await;
        }
        let dt = t.elapsed().as_secs_f64();
        assert!((0.15..1.0).contains(&dt), "{dt}");
    }
}
