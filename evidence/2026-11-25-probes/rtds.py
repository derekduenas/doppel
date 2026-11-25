import asyncio, json, time, sys
import websockets, ssl, certifi
CTX=ssl.create_default_context(cafile=certifi.where())
async def main(sec=25):
    out=open(sys.argv[1],'w'); n=0
    async with websockets.connect("wss://ws-live-data.polymarket.com", max_size=None, ssl=CTX) as ws:
        await ws.send(json.dumps({"action":"subscribe","subscriptions":[{"topic":"activity","type":"trades"}]}))
        end=time.time()+sec
        while time.time()<end:
            try: m=await asyncio.wait_for(ws.recv(), timeout=5)
            except asyncio.TimeoutError: await ws.send("PING"); continue
            out.write(json.dumps({"rx":time.time(),"m":m})+"\n"); n+=1
    print("msgs",n)
asyncio.run(main())
