"""HTTP client factory: browser User-Agent, no redirect following, host allow-list enforced before every request."""
from __future__ import annotations

import httpx

from dp_it_normativa_grab.errors import CONFIG_ERROR, GrabError

# api: the BFF; export: where the finished export zip is served (x-ipzs-location); www.gazzettaufficiale.it: the print of the GU acts
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("api.normattiva.it", "export.normattiva.it", "www.gazzettaufficiale.it")
TIMEOUT_SEC = 60.0
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def build_client(allowed_hosts: tuple[str, ...] | list[str], transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """A client that refuses (config_error) any request whose host is not in ``allowed_hosts``; redirects are never followed."""
    allowed = frozenset(host.lower() for host in allowed_hosts)

    def check_host(request: httpx.Request) -> None:
        host = request.url.host.lower()
        if host not in allowed:
            msg = f"host {host} is not in the allowed hosts {sorted(allowed)}"
            raise GrabError(CONFIG_ERROR, msg)

    return httpx.Client(
        transport=transport,
        follow_redirects=False,
        timeout=TIMEOUT_SEC,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/zip, */*"},
        event_hooks={"request": [check_host]},
    )
