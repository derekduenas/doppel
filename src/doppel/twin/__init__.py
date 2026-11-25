"""THE TWIN: honest event-driven replay of a strategy genome on Polymarket US Up/Down windows.

tape.py      our recorded PM US tape -> per-window event lists (WS preferred, REST fallback)
features.py  the genome feature vector at each decision second, from data received <= that second
engine.py    per-window simulation: taker IOC with latency, maker quotes with a pessimistic queue
verdict.py   block-bootstrap CI over windows, the genome's numeric kill rule, capacity
cli.py       ``doppel-twin``
"""
