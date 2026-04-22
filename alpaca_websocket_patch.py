"""Patch alpaca-py 0.21.1 for compatibility with websockets >= 14.

alpaca-py passes `extra_headers` to websockets.connect() but the asyncio
API in websockets 14 renamed that kwarg to `additional_headers`. Also
removes the deprecated `max_queue` parameter. Idempotent — safe to run
multiple times.

Usage: python alpaca_websocket_patch.py /path/to/alpaca/common/websocket.py
"""
import sys
import pathlib
import re


MARKER = "# --- alpaca-py websocket 14 compat patch applied ---"

def patch(path: pathlib.Path) -> None:
    if not path.exists():
        print(f"[alpaca-patch] skip: {path} not found")
        return
    src = path.read_text()
    if MARKER in src:
        print(f"[alpaca-patch] already applied to {path}")
        return

    # 1. Replace extra_headers= call with version-aware kwarg
    if "extra_headers=extra_headers" in src:
        src = src.replace(
            "self._ws = await websockets.connect(\n            self._endpoint,\n            extra_headers=extra_headers,\n            **self._websocket_params,\n        )",
            """import websockets as _ws_mod
        _ws_ver = tuple(int(x) for x in getattr(_ws_mod, "__version__", "0").split(".")[:2])
        _hdr_kwarg = "additional_headers" if _ws_ver >= (14, 0) else "extra_headers"
        self._ws = await websockets.connect(
            self._endpoint,
            **{_hdr_kwarg: extra_headers},
            **self._websocket_params,
        )"""
        )

    # 2. Drop max_queue from the default websocket_params dict — unsupported in 14+
    src = re.sub(
        r'self\._websocket_params\s*=\s*\{[^}]*"max_queue"[^}]*\}',
        'self._websocket_params = {\n            "ping_interval": 10,\n            "ping_timeout": 180,\n        }',
        src,
        count=1,
    )

    src = src.rstrip() + f"\n\n{MARKER}\n"
    path.write_text(src)
    print(f"[alpaca-patch] patched {path}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: alpaca_websocket_patch.py <path>", file=sys.stderr)
        sys.exit(1)
    patch(pathlib.Path(sys.argv[1]))
