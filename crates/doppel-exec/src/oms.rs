//! The sole order-emitting state machine. A timeout keeps the reservation live.
use crate::{
    admission::Approval,
    ledger::Ledger,
    netting::{fresh, Net, Netter, Signal, WatchState},
    risk::{self, Book, Limits, Plan, Position},
    Effect, Kind, Market, Order, Result, Venue, WireEvent, DAY_NS, MILLIS,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::Path,
};

#[derive(Debug, Copy, Clone, Eq, PartialEq, Ord, PartialOrd, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Feed {
    Brti,
    Binance,
    Hyperliquid,
}

#[derive(Debug, Copy, Clone, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Stop {
    HaltedDailyStop,
    Demoted,
    ReconciliationRequired,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Held {
    market: Market,
    position: Position,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Working {
    order: Order,
    venue_id: Option<String>,
    filled: u32,
    terminal_fill: Option<u32>,
    cancel_sent: bool,
    sent: bool,
    submitted_ns: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Mark {
    market: Market,
    order: Order,
    fill_id: String,
    price_bps: u16,
    due_ns: u64,
    seconds: u32,
}

#[derive(Debug, Default, Clone, Serialize, Deserialize)]
pub(crate) struct State {
    scope: String,
    day: u64,
    realized_loss: u64,
    halt: Option<Stop>,
    halt_day: Option<u64>,
    reconcile_required: bool,
    flatten: bool,
    nonce: u64,
    last_ns: u64,
    holdings: Vec<Held>,
    working: Vec<Working>,
    marks: Vec<Mark>,
}

#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    Submit(Order),
    Cancel { market: Market, venue_id: String },
    CancelAll { venue: Venue },
}

pub struct Oms {
    ledger: Ledger,
    state: State,
    limits: Limits,
    approval: Option<Approval>,
    venues: BTreeSet<Venue>,
    cancel_dispatched: BTreeSet<Venue>,
    feeds: BTreeMap<Feed, u64>,
    rtts: BTreeMap<Venue, (u64, u64)>,
    ready: BTreeSet<Venue>,
    books: BTreeMap<Market, Book>,
    netter: Netter,
    seen: BTreeSet<(Venue, String)>,
    sweeping: bool,
    failed: bool,
}

impl Oms {
    pub fn open(path: &Path, limits: Limits, approval: Option<Approval>, venues: BTreeSet<Venue>, now: u64) -> Result<Self> {
        limits.validate()?;
        if venues.is_empty() || now == 0 {
            return Err("missing configured venues or clock".into());
        }
        let (mut ledger, existing, seen) = Ledger::open(path)?;
        let scope = approval.as_ref().map_or("dry-run", Approval::account_scope).to_owned();
        let state = if let Some(mut state) = existing {
            if state.scope != scope || state.last_ns > now {
                return Err("ledger account or clock mismatch".into());
            }
            if !state.working.is_empty() || (approval.is_some() && !state.holdings.is_empty()) {
                state.reconcile_required = true;
                if state.halt.is_none() {
                    state.halt = Some(Stop::ReconciliationRequired);
                }
            }
            state
        } else {
            State { scope, day: now / DAY_NS, last_ns: now, ..State::default() }
        };
        ledger.append(json!({"type":"startup","ts_ns":now}), &state)?;
        Ok(Self {
            ledger,
            state,
            limits,
            approval,
            venues,
            seen,
            cancel_dispatched: BTreeSet::new(),
            feeds: BTreeMap::new(),
            rtts: BTreeMap::new(),
            ready: BTreeSet::new(),
            books: BTreeMap::new(),
            netter: Netter::default(),
            sweeping: false,
            failed: false,
        })
    }

    fn save(&mut self, event: Value) -> Result<()> {
        if self.failed {
            return Err("ledger failure latched".into());
        }
        if let Err(err) = self.ledger.append(event, &self.state) {
            self.failed = true;
            return Err(err);
        }
        Ok(())
    }

    fn clock(&mut self, now: u64) -> Result<()> {
        if now < self.state.last_ns {
            self.fault("execution_clock_reversed", self.state.last_ns)?;
            return Err("execution clock reversed".into());
        }
        self.state.last_ns = now;
        if now / DAY_NS > self.state.day {
            self.state.day = now / DAY_NS;
            if self.state.halt != Some(Stop::HaltedDailyStop) {
                self.state.realized_loss = 0;
            }
        }
        Ok(())
    }

    pub fn stop(&self) -> Option<Stop> {
        self.state.halt
    }

    pub fn reset_daily_stop(&mut self, now: u64) -> Result<()> {
        if self.state.halt != Some(Stop::HaltedDailyStop)
            || self.state.halt_day.is_none_or(|day| now / DAY_NS <= day)
            || self.state.flatten
            || self.state.reconcile_required
            || !self.state.holdings.is_empty()
            || !self.state.working.is_empty()
            || self.ready != self.venues
        {
            return Err("daily reset requires a later UTC day, reconciled venues and a flat ledger".into());
        }
        self.state.halt = None;
        self.state.halt_day = None;
        self.state.realized_loss = 0;
        self.clock(now)?;
        self.save(json!({"type":"manual_daily_reset","ts_ns":now}))
    }

    pub fn fault(&mut self, reason: &str, now: u64) -> Result<()> {
        if self.state.halt.is_none() {
            self.state.halt = Some(Stop::ReconciliationRequired);
        }
        self.state.reconcile_required = true;
        self.ready.clear();
        self.cancel_dispatched.clear();
        self.save(json!({"type":"halt","reason":reason,"ts_ns":now}))
    }

    pub fn feed(&mut self, feed: Feed, received_ns: u64, now: u64) -> bool {
        if !fresh(received_ns, now) || self.feeds.get(&feed).is_some_and(|&old| old >= received_ns) {
            return false;
        }
        self.feeds.insert(feed, received_ns);
        true
    }

    pub fn rtt(&mut self, venue: Venue, rtt_ms: u64, now: u64) {
        self.rtts.insert(venue, (rtt_ms, now));
    }

    pub fn venue_ready(&mut self, venue: Venue, ready: bool) {
        if ready {
            self.ready.insert(venue);
        } else {
            self.ready.remove(&venue);
        }
    }

    pub fn set_sweep(&mut self, active: bool) {
        self.sweeping = active;
    }

    pub fn signal(&mut self, signal: Signal, now: u64) -> bool {
        if !self.venues.contains(&signal.market.venue)
            || self.approval.as_ref().is_some_and(|a| {
                signal.shadow_expected_price_bps.is_none()
                    || !a.allows(&signal.genome_id, &signal.market, now)
                    || (signal.market.venue == Venue::Kalshi && signal.kind == Kind::Maker && !a.kalshi_zero_fee(&signal.market))
            })
        {
            return false;
        }
        self.netter.upsert(signal, now)
    }

    pub fn watchdog(&mut self, genome: String, state: WatchState, ts: u64, now: u64) -> Result<()> {
        self.clock(now)?;
        if ts > now || ts == 0 || !self.netter.watch(genome.clone(), state, ts) {
            return Ok(());
        }
        if state == WatchState::KillDemoteShadow {
            self.state.flatten = true;
            if self.state.halt.is_none() {
                self.state.halt = Some(Stop::Demoted);
            }
            self.cancel_dispatched.clear();
        }
        self.save(json!({"type":"watchdog","genome_id":genome,"state":state,"ts_ns":ts}))
    }

    pub fn book(&mut self, market: Market, book: Book, now: u64) -> Result<bool> {
        if !market.valid()
            || !book.valid()
            || !fresh(book.received_ns, now)
            || self.books.get(&market).is_some_and(|old| old.received_ns >= book.received_ns)
        {
            return Ok(false);
        }
        let mut i = 0;
        while i < self.state.marks.len() {
            let m = &self.state.marks[i];
            if m.market != market || m.due_ns > book.received_ns {
                i += 1;
                continue;
            }
            let m = self.state.marks.swap_remove(i);
            let markout = (book.received_ns - m.due_ns <= 250 * MILLIS)
                .then(|| (i32::from(book.mid()) - i32::from(m.price_bps)) * m.order.side.signed(1) as i32);
            self.save(json!({"type":"markout","venue":market.venue,"market_id":market.id,"fill_id":m.fill_id,
                             "horizon_s":m.seconds,"due_ts_ns":m.due_ns,"book_ts_ns":book.received_ns,
                             "markout_cents":markout.map(|bps| f64::from(bps) / 100.0)}))?;
        }
        self.books.insert(market, book);
        Ok(true)
    }

    fn venue_safe(&self, venue: Venue, now: u64) -> bool {
        self.ready.contains(&venue)
            && self
                .rtts
                .get(&venue)
                .is_some_and(|&(ms, ts)| ms <= risk::MAX_API_RTT_MS && now.checked_sub(ts).is_some_and(|age| age <= 2_000 * MILLIS))
    }

    fn stop_entries(&self, now: u64) -> bool {
        self.state.halt.is_some()
            || self.sweeping
            || self.state.working.iter().any(|w| self.cancel_dispatched.contains(&w.order.market.venue))
            || self.failed
            || self.approval.as_ref().is_some_and(|a| !a.valid(now))
            || [Feed::Brti, Feed::Binance, Feed::Hyperliquid].iter().any(|f| !self.feeds.get(f).is_some_and(|&ts| fresh(ts, now)))
            || self.venues.iter().any(|&v| !self.venue_safe(v, now))
    }

    fn reserved(&self) -> u64 {
        self.state.holdings.iter().map(|h| h.position.basis).sum::<u64>()
            + self
                .state
                .working
                .iter()
                .filter(|w| w.order.effect == Effect::Open)
                .map(|w| risk::reserve(&w.order, w.order.quantity - w.filled))
                .sum::<u64>()
    }

    fn trip_daily(&mut self, now: u64) -> Result<()> {
        if self.state.realized_loss >= self.limits.daily() && self.state.halt != Some(Stop::HaltedDailyStop) {
            self.state.halt = Some(Stop::HaltedDailyStop);
            self.state.halt_day = Some(self.state.day);
            self.cancel_dispatched.clear();
            self.save(json!({"type":"halt","reason":"HALTED_DAILY_STOP","ts_ns":now,
                             "realized_loss_micros":self.state.realized_loss}))?;
        }
        Ok(())
    }

    /// Each call emits at most one action; the caller processes it before asking again.
    fn expire_marks(&mut self, now: u64) -> Result<()> {
        while let Some(i) = self.state.marks.iter().position(|mark| now > mark.due_ns.saturating_add(250 * MILLIS)) {
            let mark = self.state.marks.swap_remove(i);
            self.save(json!({"type":"markout","venue":mark.market.venue,"market_id":mark.market.id,
                             "fill_id":mark.fill_id,"horizon_s":mark.seconds,"due_ts_ns":mark.due_ns,
                             "book_ts_ns":null,"markout_cents":null,"reason":"no_eligible_book"}))?;
        }
        Ok(())
    }

    pub fn next(&mut self, now: u64) -> Result<Option<Action>> {
        self.clock(now)?;
        self.trip_daily(now)?;
        self.expire_marks(now)?;
        self.netter.prune(now);
        let stopped = self.stop_entries(now);
        if stopped {
            if let Some(&venue) = self.venues.iter().find(|v| !self.cancel_dispatched.contains(v)) {
                return Ok(Some(Action::CancelAll { venue }));
            }
        } else {
            self.cancel_dispatched.clear();
        }
        let flatten = self.state.flatten && !self.state.reconcile_required;
        if (stopped && !flatten) || self.failed || (flatten && !self.state.working.is_empty()) {
            return Ok(None);
        }
        let mut exposure: BTreeSet<_> = self.state.holdings.iter().map(|h| h.market.clone()).collect();
        exposure.extend(self.state.working.iter().map(|w| w.order.market.clone()));
        let remaining_day = self.limits.daily().saturating_sub(self.state.realized_loss.saturating_add(self.reserved()));
        let mut candidate = None;
        for (market, book) in &self.books {
            if !self.venue_safe(market.venue, now) {
                continue;
            }
            let empty = Position::default();
            let holding = self.state.holdings.iter().find(|h| &h.market == market).map_or(&empty, |h| &h.position);
            let net = if flatten {
                Some(Net {
                    p_meta: 0.5,
                    q_target: 0,
                    epistemic_var: 0.0,
                    size_scale: 1.0,
                    shadow_expected_price_bps: None,
                    kind: Kind::Taker,
                    ts_ns: now,
                })
            } else {
                if self.approval.as_ref().is_some_and(|a| !self.netter.complete(market, now, a.active_genomes(market, now))) {
                    None
                } else {
                    self.netter.net(market, now, |g| self.approval.as_ref().is_none_or(|a| a.allows(g, market, now)))
                }
            };
            let desired = net.and_then(|net| {
                Plan { market, book, position: holding, net, limits: &self.limits, remaining_day, now, flatten, sweeping: self.sweeping }
                    .order()
            });
            if let Some(work) = self.state.working.iter().find(|w| &w.order.market == market) {
                if !work.cancel_sent
                    && work.terminal_fill.is_none()
                    && desired.as_ref().is_none_or(|o| {
                        o.price_bps != work.order.price_bps
                            || o.side != work.order.side
                            || o.effect != work.order.effect
                            || o.kind != work.order.kind
                            || o.quantity != work.order.quantity - work.filled
                    })
                {
                    if let Some(id) = work.venue_id.clone() {
                        return Ok(Some(Action::Cancel { market: market.clone(), venue_id: id }));
                    }
                }
                continue;
            }
            if let Some(order) = desired {
                if order.effect == Effect::Open && !exposure.contains(market) && exposure.len() >= self.limits.max_open_positions {
                    continue;
                }
                candidate = Some(order);
                break;
            }
        }
        let Some(mut order) = candidate else { return Ok(None) };
        self.state.nonce = self.state.nonce.checked_add(1).ok_or("client order ID exhausted")?;
        let digest = hex::encode(Sha256::digest(format!("{}:{}:{}", self.state.scope, now, self.state.nonce)));
        order.client_id = format!("{}-{}-4{}-8{}-{}", &digest[..8], &digest[8..12], &digest[13..16], &digest[17..20], &digest[20..32]);
        self.state.working.push(Working {
            order: order.clone(),
            venue_id: None,
            filled: 0,
            terminal_fill: None,
            cancel_sent: false,
            sent: false,
            submitted_ns: now,
        });
        self.save(json!({"type":"intent","intent_ts_ns":order.intent_ts_ns,"order":order}))?;
        Ok(Some(Action::Submit(order)))
    }

    pub fn working_order(&self, venue: Venue, venue_id: &str) -> Option<&Order> {
        self.state.working.iter().find(|w| w.order.market.venue == venue && w.venue_id.as_deref() == Some(venue_id)).map(|w| &w.order)
    }

    pub fn request(&mut self, action: &Action, method: &str, path: &str, body: Option<&Value>, now: u64) -> Result<()> {
        self.clock(now)?;
        let record = match action {
            Action::Submit(o) => {
                if !self.state.working.iter().any(|w| &w.order == o && !w.sent && w.venue_id.is_none()) {
                    return Err("request was not reserved by the OMS".into());
                }
                // Re-evaluate the circuit breakers just before transport dispatch.
                if !self.venue_safe(o.market.venue, now)
                    || !self.books.get(&o.market).is_some_and(|b| fresh(b.received_ns, now))
                    || (o.effect == Effect::Open && (self.stop_entries(now) || !fresh(o.intent_ts_ns, now)))
                {
                    self.reject_submission(&o.client_id, now)?;
                    return Err("entry circuit opened before submit".into());
                }
                if let Some(w) = self.state.working.iter_mut().find(|w| w.order.client_id == o.client_id) {
                    w.sent = true;
                    w.submitted_ns = now;
                }
                json!({"type":"submit","client_id":o.client_id,"intent_ts_ns":o.intent_ts_ns,
                       "submit_ts_ns":now,"shadow_expected_price_bps":o.shadow_price_bps})
            }
            Action::Cancel { market, venue_id } => json!({"type":"cancel","venue":market.venue,
                                                        "venue_id":venue_id,"submit_ts_ns":now}),
            Action::CancelAll { venue } => json!({"type":"cancel_all","venue":venue,"submit_ts_ns":now}),
        };
        self.save(json!({"type":"request","request":record,"method":method,"path":path,"body":body}))
    }

    pub fn cancellation_ack(&mut self, action: &Action, now: u64) -> Result<()> {
        match action {
            Action::CancelAll { venue } => {
                self.cancel_dispatched.insert(*venue);
                for w in self.state.working.iter_mut().filter(|w| w.order.market.venue == *venue) {
                    w.cancel_sent = true;
                }
            }
            Action::Cancel { market, venue_id } => {
                if let Some(w) = self.state.working.iter_mut().find(|w| w.order.market == *market && w.venue_id.as_ref() == Some(venue_id))
                {
                    w.cancel_sent = true;
                }
            }
            Action::Submit(_) => return Err("submit is not a cancellation".into()),
        }
        self.save(json!({"type":"cancel_ack","ack_ts_ns":now}))
    }

    /// Only use on a definitive rejection, including a local request that was never dispatched.
    pub fn reject_submission(&mut self, client_id: &str, now: u64) -> Result<()> {
        self.state.working.retain(|w| w.order.client_id != client_id || w.venue_id.is_some());
        self.save(json!({"type":"definitive_reject","client_id":client_id,"ts_ns":now}))
    }

    pub fn event(&mut self, venue: Venue, event: WireEvent, now: u64) -> Result<()> {
        self.clock(now)?;
        match event {
            WireEvent::Ack { client_id, venue_id } => {
                if venue_id.is_empty()
                    || self
                        .state
                        .working
                        .iter()
                        .any(|w| w.order.market.venue == venue && w.venue_id.as_ref() == Some(&venue_id) && w.order.client_id != client_id)
                {
                    return self.fault("invalid_ack", now);
                }
                let Some(w) = self.state.working.iter_mut().find(|w| w.order.market.venue == venue && w.order.client_id == client_id)
                else {
                    return self.fault("unknown_ack", now);
                };
                if w.venue_id.as_ref().is_some_and(|id| *id != venue_id) {
                    return self.fault("conflicting_ack", now);
                }
                w.venue_id = Some(venue_id.clone());
                let submitted = w.submitted_ns;
                self.save(json!({"type":"ack","venue":venue,"venue_id":venue_id,"client_id":client_id,
                                 "submit_ts_ns":submitted,"ack_ts_ns":now}))
            }
            WireEvent::Terminal { venue_id, filled } => {
                if let Some(i) =
                    self.state.working.iter().position(|w| w.order.market.venue == venue && w.venue_id.as_ref() == Some(&venue_id))
                {
                    let w = &mut self.state.working[i];
                    if filled < w.filled || filled > w.order.quantity || w.terminal_fill.is_some_and(|prev| prev != filled) {
                        return self.fault("invalid_terminal_size", now);
                    }
                    w.terminal_fill = Some(filled);
                    if w.filled == filled {
                        self.state.working.swap_remove(i);
                    }
                    self.save(json!({"type":"terminal","venue":venue,"venue_id":venue_id,
                                     "cum_filled":filled,"ack_ts_ns":now}))?;
                }
                Ok(())
            }
            WireEvent::Fill { id, venue_id, client_id, market_id, quantity, price_bps, fee_micros, ts_ns } => {
                let key = (venue, id.clone());
                if self.seen.contains(&key) {
                    return Ok(());
                }
                let Some(i) = self.state.working.iter().position(|w| {
                    w.order.market.venue == venue
                        && (w.venue_id.as_ref() == Some(&venue_id)
                            || (w.venue_id.is_none() && client_id.as_ref() == Some(&w.order.client_id)))
                }) else {
                    return self.fault("unreconciled_fill", now);
                };
                let w = &self.state.working[i];
                let order = w.order.clone();
                if id.is_empty()
                    || venue_id.is_empty()
                    || market_id != order.market.id
                    || quantity == 0
                    || quantity > order.quantity - w.filled
                    || w.terminal_fill.is_some_and(|total| w.filled + quantity > total)
                    || !(1..10_000).contains(&price_bps)
                    || order.side.directional_price(price_bps) > order.side.directional_price(order.price_bps)
                    || fee_micros.is_some_and(|fee| fee > u64::from(quantity) * risk::USD)
                    || ts_ns == 0
                {
                    return self.fault("invalid_fill", now);
                }
                let actual_fee = fee_micros.unwrap_or(u64::from(quantity) * 20_000);
                if !self.apply_fill(&order, quantity, price_bps, actual_fee) {
                    return self.fault("position_or_fee_reconciliation", now);
                }
                let w = &mut self.state.working[i];
                w.venue_id = Some(venue_id.clone());
                w.filled += quantity;
                if w.filled == w.order.quantity || w.terminal_fill == Some(w.filled) {
                    self.state.working.swap_remove(i);
                }
                self.seen.insert(key);
                for seconds in [2_u32, 10] {
                    self.state.marks.push(Mark {
                        market: order.market.clone(),
                        order: order.clone(),
                        fill_id: id.clone(),
                        price_bps,
                        due_ns: ts_ns.saturating_add(u64::from(seconds) * 1000 * MILLIS),
                        seconds,
                    });
                }
                let slip = (i32::from(price_bps) - i32::from(order.shadow_price_bps)) * order.side.signed(1) as i32;
                self.save(json!({"type":"fill","venue":venue,"market_id":market_id,"venue_id":venue_id,
                    "client_id":order.client_id,"fill_id":id,"fill_ts_ns":ts_ns,"receive_ts_ns":now,
                    "intent_ts_ns":order.intent_ts_ns,"fill_price_bps":price_bps,"quantity":quantity,
                    "fee_paid_micros":fee_micros,"rebate_est_micros":risk::estimated_rebate(&order,quantity,price_bps,ts_ns),
                    "shadow_expected_price_bps":order.shadow_price_bps,"slippage_cents":f64::from(slip)/100.0,
                    "canary_slippage_pass":slip <= 50}))?;
                self.trip_daily(now)?;
                if fee_micros.is_none() {
                    self.fault("fill_fee_unmeasured", now)?;
                } else if (order.kind == Kind::Maker && fee_micros != Some(0)) || actual_fee > u64::from(quantity) * 20_000 {
                    self.fault("fee_differs_from_risk_reserve", now)?;
                }
                Ok(())
            }
        }
    }

    fn apply_fill(&mut self, order: &Order, qty: u32, p: u16, fee: u64) -> bool {
        let idx = self.state.holdings.iter().position(|h| h.market == order.market);
        let signed = order.side.signed(qty);
        let price = u64::from(order.side.directional_price(p)) * 100;
        let qty = u64::from(qty);
        if order.effect == Effect::Open {
            if let Some(i) = idx {
                let h = &mut self.state.holdings[i].position;
                if h.quantity.signum() != signed.signum() {
                    return false;
                }
                h.quantity += signed;
                h.basis += price * qty + fee;
            } else {
                self.state
                    .holdings
                    .push(Held { market: order.market.clone(), position: Position { quantity: signed, basis: price * qty + fee } });
            }
        } else {
            let Some(i) = idx else { return false };
            let h = &mut self.state.holdings[i].position;
            let held = h.quantity.unsigned_abs();
            if h.quantity.signum() == signed.signum() || held < qty {
                return false;
            }
            let basis = h.basis.saturating_mul(qty).div_ceil(held).min(h.basis);
            let loss = (basis + fee).saturating_sub((risk::USD - price) * qty);
            self.state.realized_loss = self.state.realized_loss.saturating_add(loss);
            h.quantity += signed;
            h.basis -= basis;
            if h.quantity == 0 {
                self.state.holdings.swap_remove(i);
            }
        }
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::risk::Level;

    const T: u64 = 1_000 * MILLIS;
    fn market() -> Market {
        Market { venue: Venue::Pmus, id: "btc-up-fixture".into() }
    }
    fn book(now: u64) -> Book {
        Book {
            received_ns: now,
            bids: vec![Level { price_bps: 4800, quantity: 500 }],
            asks: vec![Level { price_bps: 5200, quantity: 500 }],
        }
    }
    fn signal(id: &str, target: i32, now: u64) -> Signal {
        Signal {
            genome_id: id.into(),
            market: market(),
            p_hat: 0.8,
            epistemic_var: 1e-6,
            target_contracts: target,
            shadow_expected_price_bps: Some(5200),
            kind: Kind::Maker,
            ts_ns: now,
        }
    }
    fn healthy(path: &Path, now: u64) -> Oms {
        let mut oms = Oms::open(path, Limits::default(), None, [Venue::Pmus].into(), now).unwrap();
        oms.venue_ready(Venue::Pmus, true);
        oms.rtt(Venue::Pmus, 20, now);
        for feed in [Feed::Brti, Feed::Binance, Feed::Hyperliquid] {
            assert!(oms.feed(feed, now, now));
        }
        assert!(oms.book(market(), book(now), now).unwrap());
        oms
    }

    #[test]
    fn opposite_signals_net_before_reservation_and_book() {
        let dir = tempfile::tempdir().unwrap();
        let mut oms = healthy(&dir.path().join("ledger"), T);
        assert!(oms.signal(signal("genome-a", 10, T), T));
        assert!(oms.signal(signal("genome-b", -10, T), T));
        assert_eq!(oms.next(T).unwrap(), None);
        assert!(oms.state.working.is_empty());
        assert!(oms.signal(signal("genome-b", -9, T + 1), T + 1));
        let Some(Action::Submit(order)) = oms.next(T + 1).unwrap() else { panic!("one net entry expected") };
        assert_eq!(order.quantity, 1);
        assert_eq!(order.side, crate::Side::BuyYes);
        assert_eq!(order.effect, Effect::Open);
    }

    #[test]
    fn stale_feed_and_high_rtt_request_cancel_all_without_new_entry() {
        let dir = tempfile::tempdir().unwrap();
        for (case, latency, at) in [("stale", 10, T + 251 * MILLIS), ("rtt", 301, T)] {
            let mut oms = healthy(&dir.path().join(case), T);
            oms.signal(signal("genome-a", 10, T), T);
            oms.rtt(Venue::Pmus, latency, T);
            assert_eq!(oms.next(at).unwrap(), Some(Action::CancelAll { venue: Venue::Pmus }));
        }
    }

    #[test]
    fn daily_stop_is_durable_and_requires_manual_reset() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("ledger");
        let mut oms = healthy(&path, T);
        oms.state.realized_loss = 250 * risk::USD;
        oms.signal(signal("genome-a", 10, T), T);
        assert_eq!(oms.next(T).unwrap(), Some(Action::CancelAll { venue: Venue::Pmus }));
        assert_eq!(oms.stop(), Some(Stop::HaltedDailyStop));
        assert!(oms.reset_daily_stop(T).is_err());
        drop(oms);
        let later = DAY_NS + T;
        let mut reopened = healthy(&path, later);
        assert_eq!(reopened.stop(), Some(Stop::HaltedDailyStop));
        assert_eq!(reopened.next(later).unwrap(), Some(Action::CancelAll { venue: Venue::Pmus }));
        reopened.reset_daily_stop(later).unwrap();
        assert_eq!(reopened.stop(), None);
    }

    #[test]
    fn demotion_cancels_then_only_emits_a_reduce_only_close() {
        let dir = tempfile::tempdir().unwrap();
        let mut oms = healthy(&dir.path().join("ledger"), T);
        oms.state.holdings.push(Held { market: market(), position: Position { quantity: 2, basis: 900_000 } });
        oms.signal(signal("genome-a", 10, T), T);
        oms.watchdog("genome-a".into(), WatchState::KillDemoteShadow, T, T).unwrap();
        assert_eq!(oms.stop(), Some(Stop::Demoted));
        let cancel = oms.next(T).unwrap().unwrap();
        assert_eq!(cancel, Action::CancelAll { venue: Venue::Pmus });
        oms.cancellation_ack(&cancel, T).unwrap();
        let Some(Action::Submit(order)) = oms.next(T).unwrap() else { panic!("reduce-only close expected") };
        assert_eq!(order.effect, Effect::Close);
        assert_eq!(order.side, crate::Side::SellYes);
        assert_eq!(order.quantity, 2);
    }

    #[test]
    fn fills_are_idempotent_and_terminal_waits_for_missing_fill() {
        let dir = tempfile::tempdir().unwrap();
        let mut oms = healthy(&dir.path().join("ledger"), T);
        oms.signal(signal("genome-a", 6, T), T);
        let Some(Action::Submit(o)) = oms.next(T).unwrap() else { panic!("entry expected") };
        oms.event(Venue::Pmus, WireEvent::Ack { client_id: o.client_id.clone(), venue_id: "o".into() }, T).unwrap();
        oms.event(Venue::Pmus, WireEvent::Terminal { venue_id: "o".into(), filled: 2 }, T).unwrap();
        assert!(oms.working_order(Venue::Pmus, "o").is_some());
        let fill = WireEvent::Fill {
            id: "f".into(),
            venue_id: "o".into(),
            client_id: None,
            market_id: o.market.id.clone(),
            quantity: 2,
            price_bps: 4800,
            fee_micros: Some(0),
            ts_ns: T,
        };
        oms.event(Venue::Pmus, fill.clone(), T).unwrap();
        oms.event(Venue::Pmus, fill, T).unwrap();
        assert!(oms.working_order(Venue::Pmus, "o").is_none());
        assert_eq!(oms.state.holdings[0].position.quantity, 2);
    }

    #[test]
    fn warn_throttles_size_and_sweep_cancels_resting_quotes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("ledger");
        let mut oms = healthy(&path, T);
        oms.signal(signal("genome-a", 20, T), T);
        oms.watchdog("genome-a".into(), WatchState::WarnThrottle50, T, T).unwrap();
        let Some(Action::Submit(o)) = oms.next(T).unwrap() else { panic!("throttled entry expected") };
        assert!(o.quantity <= 10);
        oms.set_sweep(true);
        assert_eq!(oms.next(T).unwrap(), Some(Action::CancelAll { venue: Venue::Pmus }));
    }
}
