"""Sealed replay, sparse state, capture ordering and conservative coverage boundaries."""
import hashlib

import orjson
import pytest

from doppel.lab.inspection_book import read_book
from doppel.tape.segment import SegmentWriter
from test_ordermatch import MS, T0, book, pc, trade

STREAM = 'clob_btc_5m'
A, B = 'a', 'b'
KEY = (A, .07, 'BUY')


def tape(root, rows):
    writer = SegmentWriter(root, STREAM)
    for ms, row in rows:
        if isinstance(row, str):
            writer.append('', tw=(MS + ms) * 10**6, meta={'ctl': row})
        else:
            writer.append(orjson.dumps(row), tw=(MS + ms) * 10**6)
    writer.close()
    return root / STREAM / '20261125/18.dtape.zst'


def read(root, end=T0 + 300):
    return read_book(root, STREAM, T0, end, {KEY: {60}}, {'tx'}, {A: B})


def test_sparse_projection_marks_incremental_and_snapshot_sources_and_causal_ordinals(tmp_path):
    p = tape(tmp_path, [book(1, A), book(2, B), book(3, A), book(4, A, bids=[(.07,60)]),
                        pc(5, (A,.07,0,'BUY')), pc(6, (A,.07,4,'BUY')),
                        (7,[pc(7, (A,.07,64.02,'BUY'))[1], trade(7,A,.07,13,'TX')[1]]),
                        trade(8,A,.07,4,'not_requested'), trade(9,B,.93,60,'tx')])
    r = read(tmp_path)
    assert [e.src for e in r.events[KEY]] == ['book', 'price_change']
    assert [e.venue_ms - MS for e in r.empty[KEY]] == [1,3,5,6]
    assert r.empty[KEY][-1].src == 'before_price_change'
    assert [e.venue_ms-MS for e in r.rises[KEY]] == [4,6]  # every size, including unrequested
    assert r.events[KEY][-1].delta == pytest.approx(60.02)
    assert r.events[KEY][-1].seq == r.prints['tx'][0].seq
    assert r.events[KEY][-1].ordinal < r.prints['tx'][0].ordinal
    assert [p.asset for p in r.prints['tx']] == [A,B] and set(r.prints)=={'tx'}
    assert r.prints['tx'][0].span_id == r.spans[A][0].span_id
    assert r.sources[0]['sha256'] == hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.parametrize('reason',['disconnect','unsubscribe','backpressure_drop','new_control'])
def test_reset_starts_new_span_only_at_snapshot(tmp_path, reason):
    tape(tmp_path,[book(1,A),pc(2,(A,.07,60,'BUY')),(3,reason), pc(4,(A,.07,120,'BUY')),
                   trade(5,A,.07,3,'tx'), book(6,A,bids=[(.07,60)]), pc(7,(A,.07,120,'BUY'))])
    r=read(tmp_path)
    assert [(s.start_ms-MS,s.end_ms-MS) for s in r.spans[A]] == [(1,2),(6,7)]
    assert r.prints['tx'][0].span_id is None
    assert [e.venue_ms-MS for e in r.events[KEY]] == [2,7]


def test_venue_reversal_and_unclocked_trade_never_bridge_a_gap(tmp_path):
    tape(tmp_path,[book(1,A), pc(3,(A,.07,9,'BUY')), (4,pc(2,(A,.07,69,'BUY'))[1]),
                   trade(5,A,.07,3,'tx'), book(6,A), (7,trade(7,A,.07,3,'tx')[1] | {'timestamp':None}),
                   book(8,A)])
    r=read(tmp_path)
    assert len(r.spans[A])==2 and r.events[KEY]==[]
    assert len(r.prints['tx'])==1 and r.prints['tx'][0].span_id is None
    assert r.counts['reset:asset_clock_reversal']==1 and r.counts['unclocked_prints']==1


def test_print_clocks_do_not_advance_reverse_or_reinitialize_depth(tmp_path):
    tape(tmp_path, [book(1, A), pc(4, (A, .07, 5, 'BUY')),
                    (5, trade(2, A, .07, 5, 'tx')[1]),
                    (6, trade(90, A, .07, 5, 'tx')[1]),
                    pc(7, (A, .07, 65, 'BUY')), trade(12, A, .07, 5, 'tx'),
                    (13, 'disconnect'), (15, trade(6, A, .07, 5, 'tx')[1])])
    r = read(tmp_path)
    s, = r.spans[A]
    assert (s.start_ms - MS, s.end_ms - MS) == (1, 7)
    assert s.end_tw == (MS + 7) * 10**6
    assert [p.span_id for p in r.prints['tx']] == [s.span_id, s.span_id, s.span_id, None]
    assert [e.venue_ms - MS for e in r.events[KEY]] == [7]
    assert r.counts['reset:asset_clock_reversal'] == 0


def test_missing_hour_is_explicit_and_forces_snapshot_reinit(tmp_path):
    tape(tmp_path,[book(1,A),pc(2,(A,.07,60,'BUY')),pc(7200001,(A,.07,120,'BUY')),
                   book(7200002,A),pc(7200003,(A,.07,60,'BUY'))])
    r=read(tmp_path,end=T0+7201)
    assert r.missing == ['20261125/19.dtape.zst']
    assert len(r.spans[A])==2 and len(r.events[KEY])==2
    assert r.events[KEY][0].span_id != r.events[KEY][1].span_id


def test_corrupt_and_unsealed_files_fail_even_when_outside_output_cutoff(tmp_path):
    p=tape(tmp_path,[book(1,A),pc(900000,(A,.07,60,'BUY'))]); p.write_bytes(p.read_bytes()[:-1])
    with pytest.raises(ValueError,match='unsealed or changed'):
        read(tmp_path)
    manifest=p.parent.parent/'manifest.jsonl'; seal=orjson.loads(manifest.read_bytes()); raw=p.read_bytes()
    seal.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest());manifest.write_bytes(orjson.dumps(seal)+b'\n')
    with pytest.raises(ValueError,match='truncated'):
        read(tmp_path)


def test_valid_seal_does_not_hide_crc_damage_or_sequence_gap(tmp_path):
    import zstandard
    from doppel.tape.segment import read_segment

    path = tape(tmp_path, [book(1,A),pc(2,(A,.07,60,'BUY')),pc(3,(A,.07,120,'BUY')),
                           book(4,A),pc(5,(A,.07,60,'BUY'))])
    records = list(read_segment(path)); manifest = path.parent.parent/'manifest.jsonl'

    def seal():
        raw = zstandard.ZstdCompressor().compress(b''.join(orjson.dumps(r)+b'\n' for r in records))
        path.write_bytes(raw)
        row = orjson.loads(manifest.read_bytes())
        row.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        manifest.write_bytes(orjson.dumps(row)+b'\n')

    for r in records[2:]:
        r['seq'] += 1
    seal()
    proj = read(tmp_path)
    assert proj.counts['reset:sequence_discontinuity'] == 1
    assert len(proj.spans[A]) == 2
    assert [m.venue_ms-MS for m in proj.events[KEY]] == [2,5]
    records[-1]['crc'] += 1
    seal()
    with pytest.raises(ValueError,match='CRC mismatch'):
        read(tmp_path)
