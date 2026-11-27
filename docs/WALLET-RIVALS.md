# Layer 4 · Reverse-engineering two candidate posting rules

The question is whether the same wallets followed through Layers 0–3 post a Down bid whenever the book is eligible or only while the queue at that price is small. The primary candidate is `0xf9b7876ba2c35370e0bedb7f8ee772da722aa37c`. Its registered control is `0x28cc505fb60501dbb2199a9662ba175919af904f`. Both can have a completely reconciled trading history while some posting decisions remain unknowable.

## What we registered before scoring

Both rules propose a new 60-share buy at the Down best bid at ten-second intervals. The queue rule acts only at 1,600 displayed bid shares or fewer. The threshold was chosen because it separates the two anonymous additions seen for the candidate's first known order. This makes the original case a development example. The two rules predict the same action when the queue is shallow. A covered observation above 1,600 shares is the case that can distinguish them. A third, always-silent forecast measures how well inaction alone describes the target.

We fixed the threshold, subjects, whole markets, evidence requirements, scoring gates and negative control in review commit `a02cf9f`, on both GitHub and Apex. The real hour was fixed by the clock, outside the Layer 3 examples and the formal Layer 5 exam. Earlier layers had summarized the encompassing day, so this is a newly registered comparison on historical data. [Registration and decision rule](../evidence/2026-11-27-wallet-rivals/PROTOCOL.md)

`doppel-rivals predict` reads only the pinned registration and public grid. Both forecasts use data received strictly before the cutoff. The retrospective capture flag can exclude a score, independently of what either rival would have predicted. `score` recomputes the public forecasts, checks their digest against the disclosed labels, and requires one row at every registered time for every subject. It counts known skips, missing labels and excluded market data separately. It reports results for the complete schedule and gives each qualifying market equal weight for the comparison. A real public execution cannot be passed to this study as a wallet’s private decision.

## Controlled owner decisions

A simulator supplied complete decisions, including unfilled intentions and deliberate skips. It generated public snapshots through the production receipt-time book reader, and a separate private journal from the underlying market. The public forecasts were sealed on both mirrors in `3d109e6` before the journals were opened. The six development markets and twelve evaluation markets used different seeds and different clocks. The controlled book intentionally provides 14 deep queues among 28 eligible decisions in each market. This balanced diagnostic tests the scorer's discrimination. Its success rate applies to these specified alternatives and controls.

| Held-out subject | Usable decisions | Rival disagreements | Market votes (touch / queue / tie) | Result |
|---|---:|---:|---:|---|
| Always submits at touch | 336 | 168 | 12 / 0 / 0 | Touch recovered |
| Submits only at or below 1,600 shares | 336 | 168 | 0 / 12 / 0 | Queue rule recovered |
| Independent coin determines submission | 336 | 168 | 5 / 4 / 3 | Abstained |

Each positive control's correct rule matched all 336 eligible actions. The exact two-sided market sign probability was 0.000488 for each. For the random subject, touch scored 51.49%, queue scored 52.08% and silence scored 48.51%. Its sign probability was 1. An independent verifier checked 1,080 individual holdout intentions, including each observable order's side, size, token and limit price. Each market contributed one missing first book and one final capture break. A second full run reproduced the public grids, predictions, private journals, labels and scores byte for byte in both splits. [Independent verification and artifact digests](../evidence/2026-11-27-wallet-rivals/controlled-verification.json)

## What the controls establish

Within this experiment the program can distinguish the two registered participation rules when an independent record tells it whether the owner posted or skipped. The controlled result leaves size selection, cancellation, repricing, inventory management and hedging unspecified. The simulator's positive policies are drawn from the two families under test. The formal Stage 5 hidden-signal and decoy exam on recorded books remains a separate outstanding test.

For the real wallets, an execution may follow a placement made in an earlier cell. An empty execution cell can also contain an unfilled resting bid. The economic endpoint therefore asks a separate question. Given a displayed bid at the cutoff, was a signed Down buy with that limit first observed executing in the next ten seconds? Any signed size qualifies. A potentially matching order without a located execution censors an otherwise negative cell. A reconstructed fill gives an execution label only.

## Reproduce the controlled experiment

Use the review branch `review/wallet-rivals-20261127` and a newly created scratch directory. The repository's Python environment needs the current checkout's `src` on its import path.

```sh
export PYTHONPATH="$PWD/src"
ev=evidence/2026-11-27-wallet-rivals
plan="$ev/registration.json"
sha=ce5ac05f279af0588879e4054ca78d7145dec22de055958cff825e4b04ac6c44
dest=$(mktemp -d)
for split in dev eval; do
  python "$ev/controlled.py" public --split "$split" --out "$dest/$split-public.json"
  python -m doppel.lab.rivals predict --plan "$plan" --plan-sha256 "$sha" \
    --public "$dest/$split-public.json" --out "$dest/$split-predictions.json"
  python "$ev/controlled.py" oracle --split "$split" --public "$dest/$split-public.json" \
    --predictions "$dest/$split-predictions.json" --journal "$dest/$split-journal.jsonl" --out "$dest/$split-labels.json"
  python -m doppel.lab.rivals score --plan "$plan" --plan-sha256 "$sha" --public "$dest/$split-public.json" \
    --predictions "$dest/$split-predictions.json" --labels "$dest/$split-labels.json" --out "$dest/$split-scores.json"
done
```

The production scorer also checks that the direction of the exact sign test agrees with the market-average effect before it names a winner. Compilation of the controlled receipt used the original sealed scorer. The registered minimum effect and significance thresholds make an opposite direction mathematically impossible with twelve markets. A regression checks the bound, and replaying the final scorer reproduced both original forecasts and both score files byte for byte.

## Real books and wallets

The historical comparison spans 12 consecutive BTC five-minute markets on Nov 25 from 23:00 to 00:00 UTC. Commit `f91a167` sealed the forecast on both mirrors before execution scoring. The first public replay produced the complete grid but failed to write its source receipt because of integer JSON keys. After fixing the serializer, a second replay reproduced the grid byte for byte and wrote the source receipt. The independent verifier recomputed all 24 wallet-market comparisons and checked them against the reconciled order receipts on Apex.

| Registered role | Eligible books | Known execution outcomes | Qualifying executions | Censored execution cells | Owner decisions |
| --- | ---: | ---: | ---: | ---: | ---: |
| Primary candidate | 267 | 267 | 0 | 0 | 0 |
| Matched control | 267 | 266 | 3 | 1 | 0 |

Missing books excluded 93 of 360 scheduled cutoffs. The rules disagreed on 62 scored execution cells for each wallet. Only two markets for each wallet cleared both per-market coverage minima. In those markets both wallets had zero qualifying positives. The always-silent baseline scored 100%, the queue rule 55.1% and the touch rule 0%, averaged across those two markets. Both markets favored the queue rule over touch, with an exact two-sided sign-test p-value of 0.5. The control's three positives were outside those markets. The study leaves both posting strategies unidentified.

## Changes to the normal fitter

The fitter previously scored training-only coefficients then exported a full-period refit. The candidate's executable price, side, size, age, scope and risk parameters now use inputs available within completed training windows. The fitter checks the same candidate on held-out observations, using the runtime quote and risk logic and training-side median prices as the price baseline. Scheduled market starts fix the split independently of wallet activity. Market windows that cross a boundary and training rows whose confirming receipts arrive after their boundary are excluded. Native execution matching and inventory now account for receipt-time token pairing, venue-to-block consistency and conflicting executions.

Historical order matching still uses later orders when resolving competition and lacks a contemporaneous receipt for signed order terms. This leaves real retrospective placement diagnostics without independent training provenance. A real hypothesis that clears the other gates can appear in the report with exact draft bytes and their digest for prospective testing. It remains unvalidated. The controlled path can emit drafts for the answer key.

## Apex reproduction and next gate

The production tape recorder and the original six-hour baseline were active at the final operations check. Apex had about 5.6 GiB free on hot tape and 24.6 GiB free on the root and archive filesystem. Both are below the 12 GiB and 35 GiB reserves. The previous Rust cutover comparison failed on both RTDS streams. Tape and individual wallet evidence stay on Apex.

Use review source `37e0854` in a fresh scratch checkout. The public and label readers are in `evidence/2026-11-27-wallet-rivals/real_study.py`. Its `public` phase makes the public grid and source receipt. Use the registered `doppel-rivals predict` command with scope determined by that grid. Compare against the [published forecast seal](../evidence/2026-11-27-wallet-rivals/real-forecast-seal.json) before running the reader's `labels` phase and `doppel-rivals score`. Read the complete retained files under `/var/backups/doppel-sealed-20261126/research/wallet-rivals.FsyLZq`. The sealed forecast is `real/predictions.json`, the repeated public grid, sources, labels and scores are under `real-repeat/`, and the independent receipt is `release.SRNTxF/verification.json`. Its SHA-256 is `d3f184b0736c485754050c7c437faecefd0d070b7e8b768c0d8ee672f30635f9`. All recorded individual labels remain there.

Layer 5 is the prespecified three-seed, six-policy exam on recorded books. The two-rule controlled result here measures a narrower recovery task. The six-hour preflight, memory limit and reserved inputs are defined in the [exam protocol](../evidence/2026-11-27-answerkey-inputs/EXAM.md). The real-wallet gate is independently attributed decision evidence including unfilled orders and skips. An opt-in account's order and decision journal would supply it. Any real candidate carried forward should be published before a fresh holdout begins.
