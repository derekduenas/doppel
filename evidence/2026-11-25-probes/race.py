import asyncio, json, time, ssl, certifi, urllib.request, sys
CTX=ssl.create_default_context(cafile=certifi.where())
w=int(time.time()//300*300); slug=f"btc-updown-5m-{w}"
ev=json.load(urllib.request.urlopen(urllib.request.Request(f"https://gamma-api.polymarket.com/events?slug={slug}",headers={"user-agent":"research-probe"}),context=CTX,timeout=15))
mk=ev[0]["markets"][0]; toks=json.loads(mk["clobTokenIds"]); print(slug, "rules:", mk.get("description","")[:400].replace("\n"," "))
import websockets
log=open(sys.argv[1],"w")
async def clob():
    async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/market",ssl=CTX,max_size=None) as ws:
        await ws.send(json.dumps({"assets_ids":toks,"type":"market"}))
        end=time.time()+60
        while time.time()<end:
            try: m=await asyncio.wait_for(ws.recv(),5)
            except asyncio.TimeoutError: await ws.send("PING"); continue
            log.write(json.dumps({"src":"clob","rx":time.time(),"m":m})+"\n")
async def rtds():
    async with websockets.connect("wss://ws-live-data.polymarket.com",ssl=CTX,max_size=None) as ws:
        await ws.send(json.dumps({"action":"subscribe","subscriptions":[{"topic":"activity","type":"trades"}]}))
        end=time.time()+60
        while time.time()<end:
            try: m=await asyncio.wait_for(ws.recv(),5)
            except asyncio.TimeoutError: await ws.send("PING"); continue
            log.write(json.dumps({"src":"rtds","rx":time.time(),"m":m})+"\n")
async def main(): await asyncio.gather(clob(),rtds())
asyncio.run(main())
