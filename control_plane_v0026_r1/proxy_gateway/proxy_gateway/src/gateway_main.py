# -*- coding: utf-8 -*-

# proxy_gateway/src/gateway_main.py

"""Standalone direct-WSS and HTTP-only HTTPS Proxy-Gateway entry point."""

import asyncio
import os

from src.core.gateway_runtime import create_gateway_runtime
from src.system.bootstrap_system_interface import configure_logging, load_gateway_config


def _validate_listener_files(config):
    https = config["https"]
    wss = config["wss"]
    if not https.get("enabled", True) or not wss.get("enabled", True):
        raise ValueError("both WSS and HTTPS listeners are required")
    if int(https["port"]) == int(wss["port"]) and str(https["host"]) == str(wss["host"]):
        raise ValueError("WSS and HTTPS listeners must use different ports")
    if not https.get("tls_enabled", True):
        if not https.get("allow_insecure_http", False):
            raise ValueError("plain HTTP requires an explicit unsafe test flag")
    else:
        for key in ("cert_file", "key_file"):
            if not os.path.isfile(https[key]):
                raise ValueError("HTTPS TLS file does not exist: " + str(https[key]))
    for key in ("certfile", "keyfile"):
        if not os.path.isfile(wss["tls"][key]):
            raise ValueError("WSS TLS file does not exist: " + str(wss["tls"][key]))


async def _wait_listener_startup(runtime, https_server, tasks):
    timeout = float(runtime["context"]["config"]["https"].get(
        "startup_timeout_s", 15.0))
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for task in tasks:
            if task.done():
                if task.cancelled():
                    raise RuntimeError("Proxy listener was cancelled during startup")
                error = task.exception()
                if error:
                    raise error
                raise RuntimeError("Proxy listener stopped during startup")
        https_ready = bool(getattr(https_server, "started", False))
        runtime["set_https_ready"](https_ready)
        if https_ready and runtime["wss"]["snapshot"]().get("ready"):
            return True
        await asyncio.sleep(0.05)
    raise RuntimeError("Proxy WSS/HTTPS listener startup timed out")


async def _serve_proxy(config, logger):
    import uvicorn

    runtime = create_gateway_runtime(config, logger=logger)
    https = config["https"]
    server_config = uvicorn.Config(
        runtime["application"],
        interface="asgi3",
        host=https["host"],
        port=int(https["port"]),
        ssl_certfile=https["cert_file"] if https.get("tls_enabled", True) else None,
        ssl_keyfile=https["key_file"] if https.get("tls_enabled", True) else None,
        workers=1,
        loop="asyncio",
        http="h11",
        ws="none",
        limit_concurrency=int(https.get("limit_concurrency", 256)),
        backlog=int(https.get("backlog", 128)),
        timeout_keep_alive=5,
        server_header=False,
        date_header=True,
        access_log=False,
        log_config=None,
        proxy_headers=False,
    )
    https_server = uvicorn.Server(server_config)
    wss_task = None
    https_task = None
    await asyncio.to_thread(runtime["start"])
    try:
        wss_task = asyncio.create_task(
            runtime["wss"]["serve"](), name="ProxyDirectWSS")
        https_task = asyncio.create_task(
            https_server.serve(), name="ProxyHTTPOnlyUvicorn")
        await _wait_listener_startup(
            runtime, https_server, (wss_task, https_task))
        logger.info(
            "Proxy started: site=%s instance=%s wss_port=%s https_port=%s",
            config["site_id"], config["instance_id"],
            config["wss"]["port"], config["https"]["port"],
        )
        done, _pending = await asyncio.wait(
            (wss_task, https_task), return_when=asyncio.FIRST_COMPLETED)
        failure = None
        for task in done:
            if task.cancelled():
                continue
            exception = task.exception()
            if exception:
                failure = exception
                break
        if failure:
            raise failure
    finally:
        runtime["set_https_ready"](False)
        runtime["wss"]["stop"]()
        https_server.should_exit = True
        tasks = tuple(
            task for task in (wss_task, https_task) if task is not None)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.to_thread(runtime["stop"])
        logger.info("Proxy stopped")


def main():
    """Load configuration and run both listeners in one AsyncIO lifecycle."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    config_path = os.environ.get(
        "PROXY_CONFIG_PATH",
        os.path.join(project_root, "_config", "application", "gateway_config.json"),
    )
    config = load_gateway_config(config_path)
    logger = configure_logging(config.get("logging") or {})
    _validate_listener_files(config)
    asyncio.run(_serve_proxy(config, logger))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
