"""Read only this project's loopback dashboard; never print config or control tokens."""

import json
from http.cookiejar import CookieJar
from urllib.request import HTTPCookieProcessor, ProxyHandler, build_opener

if __name__ == "__main__":
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    with opener.open("http://127.0.0.1:8080/api/bootstrap", timeout=5) as response:
        payload = json.load(response)
    snapshot = payload["snapshot"]
    print(
        json.dumps(
            {
                key: snapshot.get(key)
                for key in (
                    "mode",
                    "busy",
                    "phase",
                    "active_task",
                    "blocker",
                    "query_backend",
                    "query_count",
                )
            },
            ensure_ascii=False,
        )
    )
