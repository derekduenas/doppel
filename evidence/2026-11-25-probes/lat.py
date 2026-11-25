import json, sys, ssl, certifi, urllib.request, statistics as st, random, collections
CTX=ssl.create_default_context(cafile=certifi.where())
rows=[]
for l in open(sys.argv[1]):
    r=json.loads(l)
    if not r["m"]: continue
    m=json.loads(r["m"]); p=m["payload"]
    rows.append((r["rx"], m["timestamp"]/1000, p["timestamp"], p["transactionHash"], p["proxyWallet"], p.get("fee"), p["price"], p["size"], p["slug"]))
print("trades",len(rows),"unique tx",len({x[3] for x in rows}),"wallets",len({x[4] for x in rows}))
print("rx - msg.timestamp ms  p50/p90:", [round(1000*q,0) for q in st.quantiles([a-b for a,b,*_ in rows],n=10)[4:9:4]])
print("msg.ts - payload.ts(s) p50:", st.median([b-c for a,b,c,*_ in rows]))
fees=[x[5] for x in rows]; print("fee>0 share", sum(1 for f in fees if f and f>0)/len(fees))
cats=collections.Counter(x[8].split('-')[0] for x in rows); print(cats.most_common(8))
def rpc(method, params):
    req=urllib.request.Request("https://polygon.drpc.org", data=json.dumps({"jsonrpc":"2.0","id":1,"method":method,"params":params}).encode(), headers={"content-type":"application/json","user-agent":"research-probe"})
    return json.load(urllib.request.urlopen(req, context=CTX, timeout=15))["result"]
sample=random.Random(1).sample(rows, 25); d=[]
for rx,mts,pts,tx,*_ in sample:
    rc=rpc("eth_getTransactionReceipt",[tx])
    if not rc: continue
    b=rpc("eth_getBlockByNumber",[rc["blockNumber"],False]); bt=int(b["timestamp"],16)
    d.append(mts-bt)
print("n",len(d),"msg.ts - block.ts (s) min/med/max:", min(d), st.median(d), max(d))
