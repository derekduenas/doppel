//! Stream table, CLI/TOML config, status.json and the run loop (Python `cli.py run`).

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::json;
use tokio::sync::watch;

use crate::clock::wall_ns;
use crate::http::RateLimiter;
use crate::kalshi::{KalshiBooks, KalshiBrti, KalshiWs};
use crate::l2::{RestL2, SpotL2, L2_REST_VENUES, L2_VENUES};
use crate::lead::{self, LeadFeed};
use crate::pmus::PmusMarketsWs;
use crate::recorder::{spawn_writer, stats_map, Recorder, Stats, WriterThread, DEFAULT_QUEUE};
use crate::universe::{IntlUniverse, ASSETS, HZ};
use crate::venues::{BitstampTrades, CoinbaseTrades, IntlClobMarket, KrakenTrades, RtdsTrades};
use crate::ws::{run_ws, WsOpts};

/// Every stream name, in start order. Same names as the Python recorder, plus `kalshi_ws`.
pub fn all_streams() -> Vec<String> {
    let mut v: Vec<String> = vec!["rtds_a".into(), "rtds_b".into()];
    for a in ASSETS {
        for h in HZ {
            v.push(format!("clob_{a}_{h}"));
        }
    }
    for s in ["pmus_ws_5m", "pmus_ws_15m", "kalshi_books", "ref_brti_live", "kalshi_ws", "spot_coinbase", "spot_kraken", "spot_bitstamp"] {
        v.push(s.into());
    }
    v
}

/// Spot L2 book streams (`spot_l2_<venue>`). Opt-in only: `all` does not include them, so a unit
/// without `--only` keeps recording exactly what it did before.
pub fn l2_streams() -> Vec<String> {
    L2_VENUES.iter().chain(&L2_REST_VENUES).map(|v| format!("spot_l2_{v}")).collect()
}

/// Resolve `--only` names: `clob_intl` = every CLOB shard, `kalshi_brti` = `ref_brti_live`
/// (the Python key), `spot_l2` = every BRTI spot L2 book stream, `lead_feeds` = every public
/// Binance and Hyperliquid lead stream. Both groups are opt-in. `pmus_books` is deliberately not
/// ported (the PM US REST book is CDN-stale).
pub fn resolve_streams(only: &[String]) -> Result<Vec<String>, String> {
    let all = all_streams();
    let l2 = l2_streams();
    let leads: Vec<_> = lead::stream_names().map(str::to_owned).collect();
    let mut want = BTreeSet::new();
    for raw in only {
        let n = raw.trim();
        match n {
            "" => {}
            "all" => want.extend(all.iter().cloned()),
            "clob_intl" => want.extend(all.iter().filter(|s| s.starts_with("clob_")).cloned()),
            "kalshi_brti" => {
                want.insert("ref_brti_live".to_string());
            }
            "spot_l2" => want.extend(l2.iter().cloned()),
            "lead_feeds" => want.extend(leads.iter().cloned()),
            "pmus_books" => return Err("pmus_books is not ported (PM US REST book is CDN-stale)".into()),
            n if all.iter().chain(&l2).chain(&leads).any(|s| s == n) => {
                want.insert(n.to_string());
            }
            n => return Err(format!("unknown stream: {n}")),
        }
    }
    Ok(all.into_iter().chain(l2).chain(leads).filter(|s| want.contains(s)).collect())
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileConfig {
    pub data: Option<PathBuf>,
    pub only: Option<Vec<String>>,
    pub queue: Option<usize>,
    pub seconds: Option<f64>,
}

#[derive(Debug)]
pub struct Config {
    pub data: PathBuf,
    pub streams: Vec<String>,
    pub queue: usize,
    pub seconds: Option<f64>,
}

pub const USAGE: &str = "usage: doppel-taped run --data DIR [--only a,b,...] [--seconds N] [--queue N] [--config FILE.toml]";

pub fn parse_args(args: &[String]) -> Result<Config, String> {
    let mut it = args.iter();
    match it.next().map(String::as_str) {
        Some("run") => {}
        _ => return Err(USAGE.into()),
    }
    let mut file = FileConfig::default();
    let (mut data, mut only, mut queue, mut seconds) = (None, None, None, None);
    while let Some(a) = it.next() {
        let mut val = || it.next().cloned().ok_or_else(|| format!("{a} needs a value"));
        match a.as_str() {
            "--data" => data = Some(PathBuf::from(val()?)),
            "--only" => only = Some(val()?.split(',').map(str::to_string).collect::<Vec<_>>()),
            "--seconds" => seconds = Some(val()?.parse::<f64>().map_err(|_| "--seconds: number")?),
            "--queue" => queue = Some(val()?.parse::<usize>().map_err(|_| "--queue: integer")?),
            "--config" => {
                let p = val()?;
                let text = std::fs::read_to_string(&p).map_err(|e| format!("{p}: {e}"))?;
                file = toml::from_str(&text).map_err(|e| format!("{p}: {e}"))?;
            }
            other => return Err(format!("unknown argument {other}\n{USAGE}")),
        }
    }
    let data = data.or(file.data).ok_or_else(|| format!("--data is required\n{USAGE}"))?;
    let only = only.or(file.only).unwrap_or_else(|| vec!["all".into()]);
    Ok(Config {
        data,
        streams: resolve_streams(&only)?,
        queue: queue.or(file.queue).unwrap_or(DEFAULT_QUEUE),
        seconds: seconds.or(file.seconds),
    })
}

fn spawn_source(name: &str, rec: Recorder, stop: watch::Receiver<bool>, kalshi_lim: &Arc<RateLimiter>) -> tokio::task::JoinHandle<()> {
    let o = WsOpts::default;
    match name {
        "rtds_a" | "rtds_b" => tokio::spawn(run_ws(RtdsTrades::new(), rec, stop, o())),
        "pmus_ws_5m" => tokio::spawn(run_ws(PmusMarketsWs::new(&["5m"]), rec, stop, o())),
        "pmus_ws_15m" => tokio::spawn(run_ws(PmusMarketsWs::new(&["15m"]), rec, stop, o())),
        "kalshi_books" => tokio::spawn(KalshiBooks::new(kalshi_lim.clone()).run(rec, stop)),
        "ref_brti_live" => tokio::spawn(KalshiBrti::new().run(rec, stop)),
        "kalshi_ws" => tokio::spawn(run_ws(KalshiWs::new(), rec, stop, o())),
        "spot_coinbase" => tokio::spawn(run_ws(CoinbaseTrades, rec, stop, o())),
        "spot_kraken" => tokio::spawn(run_ws(KrakenTrades, rec, stop, o())),
        "spot_bitstamp" => tokio::spawn(run_ws(BitstampTrades, rec, stop, o())),
        n if n.starts_with("lead_") => {
            let feed = LeadFeed::new(n).unwrap_or_else(|| panic!("no source for {n}"));
            tokio::spawn(run_ws(feed, rec, stop, WsOpts { backoff_max_s: 120.0, ..o() }))
        }
        n if n.starts_with("spot_l2_") => {
            let venue = &n["spot_l2_".len()..];
            if let Some(r) = RestL2::new(venue) {
                return tokio::spawn(r.run(rec, stop));
            }
            let v = SpotL2::new(venue).unwrap_or_else(|| panic!("no source for {n}"));
            tokio::spawn(run_ws(v, rec, stop, o()))
        }
        n if n.starts_with("clob_") => {
            let mut p = n.splitn(3, '_').skip(1);
            let (a, h) = (p.next().unwrap_or(""), p.next().unwrap_or(""));
            tokio::spawn(run_ws(IntlClobMarket::new(IntlUniverse::new(&[a], &[h])), rec, stop, o()))
        }
        n => panic!("no source for {n}"),
    }
}

pub fn write_status(data: &Path, started: Instant, stats: &[(String, Arc<Stats>)]) -> std::io::Result<()> {
    let st = json!({
        "tw": wall_ns(),
        "pid": std::process::id(),
        "uptime_s": started.elapsed().as_secs_f64().round() as u64,
        "sources": stats_map(stats),
    });
    let path = data.join("status.json");
    let tmp = data.join("status.tmp");
    std::fs::write(&tmp, serde_json::to_vec_pretty(&st).expect("status serializes"))?;
    std::fs::rename(tmp, path)
}

pub async fn run(cfg: Config) -> std::io::Result<()> {
    std::fs::create_dir_all(&cfg.data)?;
    let (stop_tx, stop_rx) = watch::channel(false);
    let kalshi_lim = Arc::new(RateLimiter::new(8.0, 4));
    let mut writers: Vec<WriterThread> = Vec::new();
    let mut stats: Vec<(String, Arc<Stats>)> = Vec::new();
    let mut tasks = Vec::new();
    for name in &cfg.streams {
        let level = if name.starts_with("clob_") || name.starts_with("lead_") { 3 } else { 6 };
        let (rec, wt) = spawn_writer(cfg.data.clone(), name, level, cfg.queue)?;
        stats.push((name.clone(), rec.stats.clone()));
        writers.push(wt);
        tasks.push(spawn_source(name, rec, stop_rx.clone(), &kalshi_lim));
    }
    eprintln!("doppel-taped: recording {} streams to {}", cfg.streams.len(), cfg.data.display());
    let started = Instant::now();
    let mut sigterm = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut tick = tokio::time::interval(Duration::from_secs(5));
    tick.tick().await;
    loop {
        tokio::select! {
            _ = tick.tick() => {}
            _ = sigterm.recv() => break,
            _ = tokio::signal::ctrl_c() => break,
        }
        // file I/O off the runtime workers: a stalled disk must not stall the socket readers
        let (d, s) = (cfg.data.clone(), stats.clone());
        match tokio::task::spawn_blocking(move || write_status(&d, started, &s)).await {
            Ok(Err(e)) => eprintln!("doppel-taped: status.json: {e}"),
            Err(e) => eprintln!("doppel-taped: status task: {e}"),
            Ok(Ok(())) => {}
        }
        if cfg.seconds.is_some_and(|s| started.elapsed().as_secs_f64() >= s) {
            break;
        }
    }
    let _ = stop_tx.send(true);
    for t in tasks {
        if tokio::time::timeout(Duration::from_secs(10), t).await.is_err() {
            eprintln!("doppel-taped: a source did not stop within 10 s");
        }
    }
    let (d, s) = (cfg.data.clone(), stats.clone());
    let _ = tokio::task::spawn_blocking(move || write_status(&d, started, &s)).await;
    // every Recorder is gone with its task: writers drain, flush, close and manifest
    tokio::task::spawn_blocking(move || writers.into_iter().for_each(WriterThread::join)).await.ok();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn stream_names() {
        let all = all_streams();
        assert_eq!(all.len(), 2 + 8 + 8);
        assert!(all.contains(&"clob_xrp_15m".to_string()));
        assert_eq!(resolve_streams(&s(&["clob_intl"])).unwrap().len(), 8);
        assert_eq!(resolve_streams(&s(&["kalshi_brti", "rtds_b"])).unwrap(), s(&["rtds_b", "ref_brti_live"]));
        assert!(resolve_streams(&s(&["pmus_books"])).is_err());
        assert!(resolve_streams(&s(&["nope"])).is_err());
        // spot L2 books are opt-in: `all` (the default) does not start them
        assert!(resolve_streams(&s(&["all"])).unwrap().iter().all(|n| !n.starts_with("spot_l2")));
        assert_eq!(resolve_streams(&s(&["spot_l2"])).unwrap(), l2_streams());
        assert_eq!(l2_streams().len(), 7);
        assert_eq!(l2_streams().last().unwrap(), "spot_l2_lmax");
        assert_eq!(resolve_streams(&s(&["spot_l2_kraken", "spot_kraken"])).unwrap(), s(&["spot_kraken", "spot_l2_kraken"]));
        assert_eq!(resolve_streams(&s(&["all", "spot_l2"])).unwrap().len(), 18 + 7);
        assert_eq!(resolve_streams(&s(&["lead_feeds", "lead_binance_perp_trades"])).unwrap(), lead::stream_names().collect::<Vec<_>>());
        assert_eq!(resolve_streams(&s(&["all", "spot_l2", "lead_feeds"])).unwrap().len(), 18 + 7 + 6);
        assert!(resolve_streams(&s(&["all"])).unwrap().iter().all(|n| !n.starts_with("lead_")));
    }

    #[test]
    fn args_and_toml() {
        let c = parse_args(&s(&["run", "--data", "/x", "--only", "kalshi_ws,spot_kraken", "--queue", "16"])).unwrap();
        assert_eq!(c.streams, s(&["kalshi_ws", "spot_kraken"]));
        assert_eq!((c.queue, c.data), (16, PathBuf::from("/x")));
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("c.toml");
        std::fs::write(&p, "data = \"/y\"\nonly = [\"rtds_a\"]\n").unwrap();
        let c = parse_args(&s(&["run", "--config", p.to_str().unwrap()])).unwrap();
        assert_eq!((c.data, c.streams, c.queue), (PathBuf::from("/y"), s(&["rtds_a"]), DEFAULT_QUEUE));
        assert!(parse_args(&s(&["run"])).is_err());
    }

    #[tokio::test]
    async fn run_writes_status_and_stops() {
        crate::install_crypto();
        let d = tempfile::tempdir().unwrap();
        // ref_brti_live without a credential idles: no network is touched
        std::env::remove_var("CREDENTIALS_DIRECTORY");
        let cfg = Config { data: d.path().to_path_buf(), streams: s(&["ref_brti_live"]), queue: 16, seconds: Some(0.1) };
        tokio::time::timeout(Duration::from_secs(30), run(cfg)).await.unwrap().unwrap();
        let st: serde_json::Value = serde_json::from_slice(&std::fs::read(d.path().join("status.json")).unwrap()).unwrap();
        let src = &st["sources"]["ref_brti_live"];
        assert_eq!(src["idle"], "no_credentials");
        assert_eq!(src["connected"], false);
        assert!(st["pid"].as_u64().is_some());
        assert!(d.path().join("ref_brti_live/manifest.jsonl").exists());
    }
}
