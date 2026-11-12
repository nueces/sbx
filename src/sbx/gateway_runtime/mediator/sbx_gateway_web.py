"""Read-only mitmweb: retain bounded metadata snapshots, never live proxy flows."""

from __future__ import annotations

import json
import logging
import sys
from collections import deque
from pathlib import Path

from mitmproxy import connection, http, master, options
from mitmproxy.tools import cmdline
from mitmproxy.tools.main import run
from mitmproxy.tools.web.master import WebMaster
from tornado.httpserver import HTTPServer
from tornado.netutil import bind_sockets
from tornado.web import HTTPError, RequestHandler

from sbx.gateway import _atomic_json

MAX_WEB_FLOWS = 256
MAX_WEB_EVENT_BYTES = 8 * 1024 * 1024


class ReadOnlyHandler:
    def log_exception(self, typ, value, traceback):
        logging.getLogger(__name__).error("Gateway UI request failed (details withheld)")

    def prepare(self):
        if self.request.method not in {"GET", "HEAD"} or self.request.path.startswith(
            ("/commands", "/options/save", "/processes")
        ):
            raise HTTPError(403, "Gateway UI is read-only")
        return super().prepare()


class GatewayWebMaster(WebMaster):
    def __init__(self, opts: options.Options):
        super().__init__(opts)
        # The stock view holds references to requests containing injected credentials.
        self.addons.remove(self.view)
        self.addons.remove(self.events)
        self.app.settings["log_function"] = lambda handler: None
        for rule in self.app.wildcard_router.rules:
            target = rule.target
            if isinstance(target, type) and issubclass(target, RequestHandler):
                rule.target = type(target.__name__, (ReadOnlyHandler, target), {})
        self.web_server: HTTPServer | None = None
        self._snapshot_sizes: deque[int] = deque()
        self._snapshot_bytes = 0

    def gateway_snapshot(self, event: dict) -> None:
        client = connection.Client(peername=("127.0.0.1", 0), sockname=("127.0.0.1", 0))
        server = connection.Server(address=(event["host"], event["port"]))
        snapshot = http.HTTPFlow(client, server)
        try:
            snapshot.request = http.Request.make(event["method"], event["url"])
        except ValueError:
            snapshot.request = http.Request.make("GET", "https://invalid-request.invalid/")
            snapshot.comment = "Invalid request URL; original fields retained below."
        snapshot.request.path = event["request_target"]
        snapshot.request.headers.clear()
        snapshot.response = http.Response.make(event["status"] or 502, b"")
        snapshot.response.headers.clear()
        snapshot.metadata = dict(event)
        # mitmweb's JSON API omits metadata; comments expose the complete evidence in the UI too.
        snapshot.comment = json.dumps(
            {**event, **({"display_error": snapshot.comment} if snapshot.comment else {})},
            ensure_ascii=True,
        )
        size = len(snapshot.comment.encode())
        while self._snapshot_sizes and (
            len(self.view) >= MAX_WEB_FLOWS or self._snapshot_bytes + size > MAX_WEB_EVENT_BYTES
        ):
            self._snapshot_bytes -= self._snapshot_sizes.popleft()
            self.view.remove([self.view[0]])
        self._snapshot_sizes.append(size)
        self._snapshot_bytes += size
        self.view.add([snapshot])

    async def running(self):
        sockets = bind_sockets(0, "127.0.0.1")
        self.options.update(web_port=sockets[0].getsockname()[1], web_open_browser=False)
        self.web_server = HTTPServer(self.app, max_buffer_size=64 * 1024)
        self.web_server.add_sockets(sockets)
        auth = self.addons.get("webauth")
        _atomic_json(Path(self.options.confdir), "web-auth.json", {"url": auth.web_url})
        # Bypass stock WebMaster.running: our listener remains reserved on its assigned port.
        await master.Master.running(self)

    async def done(self):
        if self.web_server is not None:
            self.web_server.stop()
            await self.web_server.close_all_connections()
        await super().done()


if __name__ == "__main__":
    run(GatewayWebMaster, cmdline.mitmweb, sys.argv[1:])
