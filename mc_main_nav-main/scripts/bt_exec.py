#!/usr/bin/env python3
"""Tiny client for the BT engine HTTP API (stdlib only).

  bt_exec.py run tree.xml        # submit, follow until finished, print result
  bt_exec.py validate tree.xml
  bt_exec.py status [run_id]
  bt_exec.py cancel
  bt_exec.py nodes [--custom]    # node palette XML (for the LLM prompt)

Set BT_ENGINE_URL (default http://localhost:8080) and, when the engine is exposed
to the internet, BT_ENGINE_TOKEN.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BT_ENGINE_URL", "http://localhost:8080")
TOKEN = os.environ.get("BT_ENGINE_TOKEN", "")


def call(method, path, body=None, ctype="application/xml"):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", ctype)
    if TOKEN:
        req.add_header("X-BT-Token", TOKEN)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def show(text):
    try:
        print(json.dumps(json.loads(text), indent=2, ensure_ascii=False))
    except ValueError:
        print(text)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd in ("run", "validate"):
        xml = open(args[0]).read()
        code, text = call("POST", "/" + ("execute" if cmd == "run" else "validate"), xml)
        if cmd == "validate" or code >= 300:
            show(text)
            sys.exit(0 if code < 300 else 1)
        run_id = json.loads(text)["run_id"]
        print(f"started {run_id}")
        last = None
        while True:
            _, text = call("GET", f"/status/{run_id}")
            st = json.loads(text)
            running = st.get("running_leaves")
            if running != last:
                print(f"  running: {', '.join(running) or '-'}")
                last = running
            if st["state"] != "running":
                show(text)
                sys.exit(0 if st["state"] == "success" else 1)
            time.sleep(0.2)
    elif cmd == "status":
        show(call("GET", "/status" + (f"/{args[0]}" if args else ""))[1])
    elif cmd == "cancel":
        show(call("POST", "/cancel")[1])
    elif cmd == "nodes":
        print(call("GET", "/nodes" + ("?builtin=0" if "--custom" in args else ""))[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
