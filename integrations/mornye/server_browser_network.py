"""Mornye gateway transport for networks with changing per-connection egress.

A single provider-origin browser keeps verification and signed API requests
on the same network connection. No listening ports, TLS overrides or Spotify
credentials are involved.
"""
from __future__ import annotations
import asyncio,atexit,json,threading,time,os,signal
from urllib.parse import urlparse,urlencode,parse_qs
import httpx

_services={}
_services_lock=threading.Lock()

class GatewayBrowser:
    def __init__(self,origin):
        self.origin=origin
        self.ready=threading.Event();self.error=None;self.loop=None;self.browser=None;self.profile=None
        self.last_used=0.0
        self.thread=threading.Thread(target=self._run,daemon=True)
        self.thread.start()
        atexit.register(self.close)
    def _run(self):
        self.loop=asyncio.new_event_loop();asyncio.set_event_loop(self.loop)
        # Browser creation is lazy: restarting this service makes no provider
        # requests. The next user request owns startup and authentication.
        self.lock=asyncio.Lock()
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            self.loop.run_until_complete(self._stop())
            self.loop.close()

    async def _ensure_browser(self):
        idle = time.monotonic() - self.last_used
        if self.browser and idle > 15 * 60:
            await self._stop()
        if self.browser:
            try:
                # Check the local renderer before a provider request. A stale
                # renderer must not turn authentication into a five-minute ban.
                result = await asyncio.wait_for(self.evaluate('location.origin'), timeout=5)
                from .solver import _js_value
                if _js_value(result) != self.origin:
                    await self._stop()
            except Exception:
                await self._stop()
        if not self.browser:
            try:
                await asyncio.wait_for(self._start(), timeout=60)
            except BaseException:
                await self._stop()
                raise
        self.last_used=time.monotonic()

    async def _start(self):
        from pydoll.browser.chromium import Chrome
        from pydoll.commands.page_commands import PageCommands
        from .solver import _ensure_xvfb,build_chromium_options,isolate_browser_temp_dir
        _ensure_xvfb()
        options,self.profile=build_chromium_options(hidden=False)
        self.browser=Chrome(options=options)
        isolate_browser_temp_dir(self.browser,self.profile)
        self.tab=await self.browser.start()
        # The provider explicitly supports this bridge for its app WebView.
        # Receiving a grant keeps the page on the provider origin.
        bridge='''window.MornyeGrant = {postMessage(value) {
            if (location.origin === ORIGIN) window.__mornyeServerGrant = String(value);
        }};'''.replace('ORIGIN',json.dumps(self.origin))
        await self.tab._execute_command(PageCommands.add_script_to_evaluate_on_new_document(bridge,run_immediately=True))
        await self.tab.go_to(self.origin+"/v2/challenge?id=mornye-network-start",timeout=40)
    async def _stop(self):
        from .solver import _kill_by_profile_dir,_cleanup_pydoll_temp_dirs,_cleanup_profile_dir
        if self.browser:
            try:await asyncio.wait_for(self.browser.stop(),timeout=8)
            except BaseException:
                if self.profile:_kill_by_profile_dir(self.profile)
            _cleanup_pydoll_temp_dirs(self.browser)
        if self.profile:_cleanup_profile_dir(self.profile)
        self.browser=None;self.profile=None;self.tab=None
    def close(self):
        if not self.loop or not self.thread.is_alive():return
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        if self.thread.is_alive() and self.profile:
            from .solver import _kill_by_profile_dir
            _kill_by_profile_dir(self.profile)
    async def call(self,operation,*args):
        if not await asyncio.to_thread(self.ready.wait,60):raise TimeoutError('Mornye browser startup timed out')
        if self.error:raise RuntimeError('Mornye browser could not start') from self.error
        future=asyncio.run_coroutine_threadsafe(operation(*args),self.loop)
        return await asyncio.wrap_future(future)
    async def fetch(self,method,url,headers,body,timeout):
        async with self.lock:
            await self._ensure_browser()
            payload=json.dumps({'method':method,'url':url,'headers':headers,'body':body,'timeout':timeout},ensure_ascii=True)
            script='''(async function(p) {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), p.timeout * 1000);
                try {
                    const options={method:p.method,headers:p.headers,credentials:'include',signal:controller.signal};
                    if(p.body !== null) options.body=p.body;
                    const response=await fetch(p.url,options);
                    return {status:response.status,url:response.url,
                        headers:Object.fromEntries(response.headers.entries()),body:await response.text()};
                } catch(error) {return {failure:String(error),origin:location.origin};} finally {clearTimeout(timer);}
            })(PAYLOAD);'''.replace('PAYLOAD',payload)
            try:
                result=await asyncio.wait_for(self.evaluate(script, await_promise=True), timeout=timeout+10)
            except Exception as error:
                # Do not replay POSTs or signed requests. Invalidate the broken
                # browser; a later request will start a clean one.
                await self._stop()
                raise RuntimeError('Mornye provider browser stopped responding; its connection was reset.') from error
            finally:
                self.last_used=time.monotonic()
            from .solver import _js_value
            data=_js_value(result)
            if not isinstance(data,dict) or 'status' not in data:
                if isinstance(data,dict):raise RuntimeError('Provider fetch: '+str(data.get('failure'))[:120]+' origin='+str(data.get('origin')))
                detail=result.get('result',{}).get('exceptionDetails',{}).get('exception',{}).get('description','').split('\n')[0]
                raise RuntimeError('Provider browser request failed: '+detail[:120])
            return data
    async def evaluate(self, script, **kwargs):
        return await self.tab._execute_command({"method":"Runtime.evaluate","params":{"expression":script,"returnByValue":True,**({"awaitPromise":True} if kwargs.get("await_promise") else {})}})
    async def inspect(self):
        async with self.lock:
            if not self.browser:
                return {'running':False}
            from .solver import _js_value
            result=await asyncio.wait_for(self.evaluate('(function(){return {origin:location.origin,bridge:typeof window.MornyeGrant?.postMessage,grantPresent:!!window.__mornyeServerGrant,status:document.querySelector("#status")?.textContent || ""};})()'),timeout=5)
            return _js_value(result)

    async def verify(self,url,timeout):
        async with self.lock:
            await self._ensure_browser()
            await self.evaluate('window.__mornyeServerGrant = null')
            await self.tab.go_to(url,timeout=40)
            deadline=time.monotonic()+timeout
            from .solver import _js_value
            while time.monotonic()<deadline:
                try:
                    result=await asyncio.wait_for(self.evaluate('(function(){return {grant:window.__mornyeServerGrant || null,status:document.querySelector("#status")?.textContent || ""};})()'),timeout=5)
                except Exception as error:
                    await self._stop()
                    raise RuntimeError('Mornye provider browser stopped responding during verification.') from error
                data=_js_value(result) or {}
                callback=data.get('grant')
                if callback:
                    parsed=parse_qs(urlparse(callback).query)
                    grant=parsed.get('grant',[None])[0]
                    expected=parse_qs(urlparse(parse_qs(urlparse(url).query)['cb'][0]).query).get('state')
                    if not grant or parsed.get('state')!=expected:
                        raise RuntimeError('Invalid provider verification callback')
                    return grant
                status=data.get('status','')
                if status in {'Network changed during verification','Not found'}:
                    raise RuntimeError(status)
                await asyncio.sleep(0.3)
            await self._stop()
            raise TimeoutError('Provider verification did not complete')

def service_for(origin):
    with _services_lock:
        if origin not in _services:_services[origin]=GatewayBrowser(origin)
        return _services[origin]

class BrowserHTTPClient:
    def __init__(self,headers):
        self.headers=dict(headers);self.cookies=httpx.Cookies()
    async def request(self,method,url,**kwargs):
        parsed=urlparse(url)
        origin=parsed.scheme+'://'+parsed.netloc
        if origin!='https://api.zarz.moe':raise ValueError('Browser gateway origin is not allowed')
        params=kwargs.get('params')
        if params:url+=('&' if parsed.query else '?')+urlencode(params)
        headers={**self.headers,**kwargs.get('headers',{})}
        forbidden={'user-agent','accept-encoding','connection','host','content-length','cookie','origin'}
        headers={key:value for key,value in headers.items() if key.lower() not in forbidden}
        body=kwargs.get('content')
        if isinstance(body,bytes):body=body.decode('utf-8')
        if method.upper() in {'GET','HEAD'}:
            body=None
        if 'json' in kwargs and method.upper() not in {'GET','HEAD'}:
            body=json.dumps(kwargs['json'],separators=(',',':'));headers['Content-Type']='application/json'
        timeout=kwargs.get('timeout',30)
        if isinstance(timeout,httpx.Timeout):timeout=timeout.read or 30
        service=None if os.environ.get('MORNYE_BROWSER_SOCKET') else service_for(origin)
        try:
            data=await rpc_call('fetch',[method.upper(),url,headers,body,float(timeout)]) if os.environ.get('MORNYE_BROWSER_SOCKET') else await service.call(service.fetch,method.upper(),url,headers,body,float(timeout))
        except RuntimeError as error:
            if str(error).startswith('Provider fetch: AbortError:'):
                raise httpx.ReadTimeout('Provider browser request timed out') from error
            raise
        response_headers={key:value for key,value in data['headers'].items() if key.lower() not in {'content-encoding','content-length'}}
        return httpx.Response(data['status'],content=data['body'].encode('utf-8'),headers=response_headers,request=httpx.Request(method,url))
    async def get(self,url,**kwargs):return await self.request('GET',url,**kwargs)
    async def post(self,url,**kwargs):return await self.request('POST',url,**kwargs)
    async def aclose(self):pass # the process owns the shared browser, not one request
    async def verify(self,url,timeout):
        if os.environ.get('MORNYE_BROWSER_SOCKET'):return await rpc_call('verify',[url,timeout])
        parsed=urlparse(url);service=service_for(parsed.scheme+'://'+parsed.netloc)
        return await service.call(service.verify,url,timeout)

async def rpc_call(operation,args):
    deadline=time.monotonic()+30
    while True:
        try:
            reader,writer=await asyncio.open_unix_connection(os.environ['MORNYE_BROWSER_SOCKET'],limit=32*1024*1024)
            break
        except (FileNotFoundError,ConnectionRefusedError):
            if time.monotonic()>=deadline:raise RuntimeError('Mornye gateway service is not running')
            await asyncio.sleep(0.1)
    try:
        writer.write((json.dumps({'operation':operation,'args':args})+'\n').encode())
        await writer.drain()
        response=await asyncio.wait_for(reader.readline(),timeout=180)
        data=json.loads(response)
        if 'error' in data:
            if data.get('type')=='TimeoutError':raise TimeoutError(data['error'])
            raise RuntimeError(data['error'])
        return data['result']
    finally:
        writer.close()
        await writer.wait_closed()

async def while_connected(reader, operation):
    """Cancel queued browser work if its calling CLI has gone away."""
    task=asyncio.create_task(operation)
    disconnect=asyncio.create_task(reader.read(1))
    try:
        done,_=await asyncio.wait({task,disconnect},return_when=asyncio.FIRST_COMPLETED)
        if disconnect in done and reader.at_eof() and task not in done:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            raise ConnectionError('Mornye request caller disconnected')
        return await task
    finally:
        for pending in (task,disconnect):
            if not pending.done():pending.cancel()
        await asyncio.gather(task,disconnect,return_exceptions=True)

async def serve():
    path=os.environ['MORNYE_BROWSER_SOCKET']
    if os.path.exists(path):os.unlink(path)
    service=service_for('https://api.zarz.moe')
    async def handle(reader,writer):
        try:
            request=json.loads(await reader.readline())
            operation=request['operation'];args=request['args']
            if operation=='fetch':
                if urlparse(args[1]).netloc!='api.zarz.moe' or urlparse(args[1]).scheme!='https':raise ValueError('Origin not allowed')
                result=await while_connected(reader,service.call(service.fetch,*args))
            elif operation=='inspect':
                result=await while_connected(reader,service.call(service.inspect))
            elif operation=='verify':
                if urlparse(args[0]).netloc!='api.zarz.moe' or urlparse(args[0]).scheme!='https':raise ValueError('Origin not allowed')
                result=await while_connected(reader,service.call(service.verify,*args))
            else:raise ValueError('Operation not allowed')
            response={'result':result}
        except Exception as error:
            # Errors contain status messages, never request headers or grants.
            response={'error':str(error)[:180],'type':type(error).__name__}
        try:
            writer.write((json.dumps(response)+'\n').encode());await writer.drain()
        except (ConnectionError,BrokenPipeError):pass
        finally:writer.close()
    server=await asyncio.start_unix_server(handle,path=path,limit=32*1024*1024)
    os.chmod(path,0o600)
    stop=asyncio.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig,stop.set)
    try:
        async with server:await stop.wait()
    finally:
        service.close()
        if os.path.exists(path):os.unlink(path)

if __name__=='__main__':asyncio.run(serve())
