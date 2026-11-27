"""Run the wallet command on a four-week synthetic chain and its actual absorption receipts."""
import hashlib
from datetime import UTC, datetime

import orjson

from doppel.lab import absorb, selection, selection_plan
from doppel.lab.ordertape import Pool
from test_absorb import EX, O, U, FakeChain, _pages, _w
from test_selection import CANDIDATE, CONTROLS, sources


def buy(network, wallet, token, block, tx_id, usd):
    """Settle a 10-share buy on the fixture network and return the independent feed row."""
    tx, oid = f"0x{tx_id:064x}", f"0x{tx_id + 1000:064x}"
    fee = 20_000
    network.single(tx, block, O, wallet, token, 10 * U)
    network.cash(tx, block, wallet, O, usd * U)
    network.cash(tx, block, wallet, EX, fee)
    network._log(EX, [absorb.T_FILLED, oid, absorb.topic(wallet), absorb.topic(O)],
                 _w(0, int(token), usd * U, 10 * U, fee, 0, 0), tx, block)
    return {"id": tx, "transaction_hash": tx, "order_id": oid, "token_id": token, "address": wallet,
            "block_number": block, "block_timestamp": 1000 + 2 * block, "side": "BUY",
            "order_type": "maker", "price": usd / 10, "amount_shares": 10,
            "amount_usdc": usd, "fee": fee / U}


def test_absorption_receipts_to_wallet_selection_cli(tmp_path, capsys):
    scan, cohort = sources()
    def sha(doc):
        return hashlib.sha256(orjson.dumps(doc)).hexdigest()
    # The synthetic freeze is historical. FakeChain's block n has timestamp 1000 + 2n.
    plan = selection_plan.build_plan(scan, cohort, scan_sha256=sha(scan), cohort_sha256=sha(cohort),
                                     now_ns=1200 * 10**9)
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(orjson.dumps(plan))
    network = FakeChain(head=(plan["windows"][-1]["end"] - 1000) // 2)
    chain = absorb.Chain(Pool(["fixture"], network, rate=1e9), tmp_path / "chain-cache")
    members = [CANDIDATE, *CONTROLS]
    for wallet in members:
        network.open[absorb.PUSD, wallet, "cash"] = 100 * U
    metadata = []
    for week, span in enumerate(plan["windows"]):
        start, end = span["start"], span["end"]
        output = tmp_path / str(start)
        output.mkdir()
        pairs = {}
        fills = {wallet: [] for wallet in members}
        for index in range(2):
            event = week * 2 + index + 1
            opened = start + (index + 1) * 300
            up, down = str(2 * event), str(2 * event + 1)
            condition = f"{event:064x}"
            for outcome, token in enumerate((up, down)):
                pairs[token] = (condition, outcome)
                metadata.append({"token": token, "condition": "0x" + condition, "idx": outcome,
                                 "slug": f"btc-updown-5m-{opened}", "payout": 1 - outcome,
                                 "status": "resolved", "end": datetime.fromtimestamp(opened + 300, UTC).isoformat()})
            for member_idx, (wallet, usd) in enumerate(zip(members, (4, 6, 8))):
                fills[wallet].append(buy(network, wallet, up, (opened - 1000) // 2 + member_idx + 1,
                                         event * 10 + member_idx, usd))
        blocks = (chain.block_at(start, network.head), chain.block_at(end, network.head) - 1)
        for wallet in members:
            pages = _pages(wallet, start, end, fills[wallet])
            report = absorb.absorb_wallet(chain, wallet, fills[wallet], blocks, (start, end), 302400, pages, pairs)
            assert report["status"] == "reconciled" and report["fill_check"]["order_filled"]["matched"] == 2
            (output / f"wallet-{wallet}.json").write_bytes(orjson.dumps(report))
    tokens = tmp_path / "tokens.jsonl"
    tokens.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in metadata))
    argv = ["wallet", "--plan", str(plan_path), "--plan-sha256", plan["sha256"], "--wallet", CANDIDATE,
            "--reports", str(tmp_path), "--tokens", str(tokens)]
    assert selection.main(argv) == 0
    result = orjson.loads(capsys.readouterr().out)
    assert result["status"] == "PASS" and result["selected_for_next_layer"] and result["controls"] == CONTROLS
    assert result["winning_weeks"] == result["complete_weeks"] == 4
    assert result["candidate_pnl_micro"] == 47_840_000 and result["paired_excess_ci95_usd"] == [24, 24]
    for week in result["weeks"]:
        folder = tmp_path / str(week["start"])
        expected = {w: hashlib.sha256((folder / f"wallet-{w}.json").read_bytes()).hexdigest() for w in members}
        assert week["receipt_sha256"] == expected

    # A valid three-week subset cannot promote a wallet when one frozen control receipt is missing.
    (tmp_path / str(plan["windows"][3]["start"]) / f"wallet-{CONTROLS[1]}.json").unlink()
    assert selection.main(argv) == 1
    result = orjson.loads(capsys.readouterr().out)
    assert result["status"] == "INCONCLUSIVE" and not result["selected_for_next_layer"]
    assert result["complete_weeks"] == 3 and result["weeks"][3]["reasons"][CONTROLS[1]] == "missing absorption receipt"
