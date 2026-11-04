"""Run inside price-worker: controlled local targets, no inference or public requests."""
import asyncio
import json
import os

from playwright.async_api import async_playwright
from price_worker.guards import install_guards, pinned_resolver_rules


async def main():
    connections=[]
    async def accept(reader, writer):
        connections.append(True)
        writer.close()
        await writer.wait_closed()
    server=await asyncio.start_server(accept,'127.0.0.1',0)
    port=server.sockets[0].getsockname()[1]
    async with server, async_playwright() as playwright:
        browser=await playwright.chromium.launch(headless=True,chromium_sandbox=True,
            args=['--host-resolver-rules='+await pinned_resolver_rules(),'--disable-quic',
                  '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
        context=await browser.new_context(service_workers='block',accept_downloads=False)
        await install_guards(context)
        page=await context.new_page()
        await page.goto('about:blank')
        result=await page.evaluate('''async port => {
          const httpBlocked = await fetch(`http://127.0.0.1:${port}/private`).then(()=>false,()=>true);
          const wsBlocked = await new Promise(resolve => {
            const socket = new WebSocket(`ws://127.0.0.1:${port}/private`);
            socket.onopen=()=>{socket.close();resolve(false)};
            socket.onerror=()=>resolve(true);socket.onclose=()=>resolve(true);
            setTimeout(()=>{socket.close();resolve(true)},500);
          });
          return {httpBlocked,wsBlocked,webrtcDisabled:typeof RTCPeerConnection==='undefined'};
        }''',port)
        await page.evaluate("window.open('about:blank')")
        await asyncio.sleep(0.2)
        result.update(privateConnections=len(connections),maxOnePage=len(context.pages)<=1,nonRoot=os.getuid()!=0)
        print(json.dumps(result))
        assert result['httpBlocked'] and result['wsBlocked'] and result['webrtcDisabled']
        assert result['privateConnections']==0 and result['maxOnePage'] and result['nonRoot']
        await context.close()
        await browser.close()


asyncio.run(main())
