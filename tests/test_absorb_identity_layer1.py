from doppel.lab import absorb, ordertape


WALLET, SIGNER, OTHER = ("0x" + c * 40 for c in "abc")
BASE = {"maker": WALLET, "signer": SIGNER, "sig_type": 2, "verified": True,
        "token_id": "11", "side": "BUY", "tx": "0xt", "order_id": "01", "block": 10}


def test_identity_joins_verified_hash_in_exact_wallet_and_window(tmp_path):
    rows = [BASE, BASE, BASE | {"block": 9, "signer": OTHER}, BASE | {"tx": "unrelated", "signer": OTHER},
            BASE | {"verified": False, "signer": OTHER}, BASE | {"order_id": "03", "token_id": "12"},
            BASE | {"order_id": "04"}, BASE | {"order_id": "04", "signer": OTHER}]
    part = "day=20261125/part-000001.parquet"
    ordertape._write_part(tmp_path / part, rows)
    ordertape._commit(tmp_path, 1, [part], {})
    fills = [{"maker": WALLET, "tx": "0xt", "block": 10, "order_id": n, "token": "11", "side": "BUY"}
             for n in ("01", "03", "04")]
    doc = absorb.identities(None, WALLET, tmp_path, 15, first_block=10, filled=fills, scan_complete=True)
    assert doc["signers"] == {SIGNER: 1}
    assert doc["signer_coverage"] == {"chain_orders": 3, "verified_orders": 1, "conflicting_orders": 1,
                                       "status": "conflicting"}
    assert doc["safe_owners"] is None
    assert absorb.signer_links({WALLET: doc, OTHER: {"signers": {SIGNER: 2}}}) == [
        {"signer": SIGNER, "trading_accounts": sorted([WALLET, OTHER])}]


def test_absent_identity_evidence_preserves_unknowns():
    class NoCode:
        def get(self, *_):
            return None
    data = absorb.identities(NoCode(), WALLET, None, 5, first_block=1, filled=[], scan_complete=False)
    assert data["is_contract"] is None
    assert data["signer_coverage"]["status"] == "chain_scan_incomplete"
