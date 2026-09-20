# Machines and deployment

Three machines, all on the same Wi-Fi (`192.168.50.0/24`) and the same `ROS_DOMAIN_ID` (59).

| Name | SSH | What | Address | Hardware |
|---|---|---|---|---|
| dev laptop | – (this repo) | development, simulator, RViz | 192.168.50.109 | x86_64, Ubuntu |
| **mini PC** | `ssh mcpc` | `bt_engine` (HTTP API + palette page, **port 8090** there), `table_map` (obstacle UI, port 8081), `bt_mocks` while modules are missing | 192.168.50.125 | x86_64, Ubuntu 24.04, 16 cores, 30 GB RAM |
| **robot Pi** | `ssh mcpi` | `pose_bridge` + Nav2 + `diff_nav` | 192.168.68.53 | Raspberry Pi 5, aarch64, Ubuntu 24.04, 4 cores, 7.7 GB RAM |

Both hosts are in `~/.ssh/config` with key login (no password), the user is in the `docker` group,
and the repo lives at `~/mc_main_nav` on each. The Pi also runs a teammate's `hackathon-vision-client`.

**Those 192.168.50.x addresses are only valid on that Wi-Fi.** Move the laptop to another network and
`ssh mcpc` / `ssh mcpi` stop working. The mini PC is also on Tailscale, so it stays reachable from
anywhere:

```bash
ssh wildbot@100.113.211.14        # mcpc over Tailscale (same host keys as the LAN address)
rsync -a ... ./ wildbot@100.113.211.14:~/mc_main_nav/
```

Worth adding to `~/.ssh/config` so it survives network changes:

```
Host mcpc-ts
    HostName 100.113.211.14
    User wildbot
```

**The robot Pi is on a different subnet** (`192.168.68.0/22`) from the laptop (`192.168.50.0/24`), so
`ssh mcpi` cannot reach it directly. The mini PC has a leg on **both** networks (`192.168.68.51` and
`192.168.50.125`), so it is used as a jump host. `~/.ssh/config` on the laptop:

```
Host mcpi
    HostName 192.168.68.53
    User ducker
    ProxyJump mcpc
```

With that, `ssh mcpi`, `./mc ssh pi` and `./mc deploy pi` all work again from the laptop. **The Pi is
not on Tailscale**, so if the mini PC is off, the Pi is unreachable from outside its own network;
installing Tailscale there too (same userspace trick as the mini PC, see below) would fix that.

## Copying the repo to a machine

From the repo root on the laptop:

```bash
rsync -a --delete \
  --exclude 'ws/build/' --exclude 'ws/install/' --exclude 'ws/log/' \
  --exclude '__pycache__/' --exclude 'data/' --exclude '*.AppImage' \
  ./ mcpc:~/mc_main_nav/          # or mcpi:
```

Build outputs stay per machine (different processors), so they are never copied.

## Mini PC (`mcpc`)

```bash
ssh mcpc
cd ~/mc_main_nav
docker compose -f docker/compose.yaml build dev                    # image mc-main-nav:pc
docker compose -f docker/compose.yaml run --rm dev ./scripts/build.sh
RESTART_POLICY=unless-stopped ENGINE_PORT=8090 \
  CAMERA_BASE_URL=http://192.168.50.125:8080 \
  MOCK_DISABLE=navigate_to_object,rotate_in_place,is_at_object \
  docker compose -f docker/compose.yaml up -d engine mocks map
```

**Port 8080 on the mini PC is taken** by the VLM teammate's `vlm-server-tracker` container, so the engine
runs on **8090** there (`ENGINE_PORT`). On the laptop the default 8080 is free.

That same VLM server on 8080 is the **camera service** the search nodes talk to, so
`CAMERA_BASE_URL=http://192.168.50.125:8080`. Its API:

| | |
|---|---|
| start a search | `POST /api/query` with `{"text": "the white paper cup"}` |
| has it been seen | `GET /api/status` -> `recent[]`, newest last, each with `status: "FOUND"` |

Those are the engine's defaults (`camera.start_path`, `camera.start_field`, `camera.status_path`), so
only `CAMERA_BASE_URL` has to be set. **`camera.max_age_s` (3 s) matters**: `recent` keeps old
sightings for ever, so without an age check a hit from twenty minutes ago still reads as FOUND.
Set it via the environment, not `ros2 param set`, which is lost on restart.

From anywhere on the network afterwards:
- BT engine + node palette: **http://192.168.50.125:8090/**
- Obstacle map UI: **http://192.168.50.125:8081/**
- `BT_ENGINE_URL=http://192.168.50.125:8090 ./scripts/bt_exec.py run examples/find_and_grab_cup.xml`

Drop `MOCK_DISABLE` to have the mock answer the navigation calls too (useful with the Pi switched off).

**Set the fake world with `MOCK_OBJECTS`, not `ros2 param set`.** Runtime parameters are lost whenever the
container restarts, and then every tree asking for those objects fails with "not found in view":

```bash
MOCK_OBJECTS='cup:0.40:0.20,box:0.70:0.50,bottle:0.20:0.60,plate:0.55:0.15' \
  docker compose -f docker/compose.yaml up -d --force-recreate mocks
```

`MOCK_OBJECTS` places objects where you want them. Any **other** name the tree asks for is accepted
too: the mock invents a stable position on the table for it, so the LLM can name anything. Set
`accept_any_object:=false` to restrict it to the listed objects instead.

Closing the gripper picks up whatever object the robot drove to, so a tree that ends in
`TrackObject` + `SetGripper position="100"` is the whole pickup: approach on the camera, then close the
jaws. `GraspObject` and `IsObjectHeld` were **removed from the palette** (C6) — the actions and the mock's
servers still exist, so they can come back when a manipulation module and real gripper feedback do.

## Robot Pi (`mcpi`)

The robot launch (`diff_nav nav.launch.py`) now also starts `gripper_server`, which serves
`/set_gripper` for real: it publishes **one `std_msgs/Int16` on `/gripper`** carrying a
**0 (open) .. 100 (closed)** position that the behavior tree picks, and then waits
`settle_seconds` (2.0) for the jaws before the tree continues. The publisher is
latched, so a driver that subscribes late still gets the command. Tune it live:
`ros2 param set /gripper_server settle_seconds 3.0`.

**Add `set_gripper` to `MOCK_DISABLE` on the mini PC** once the robot's gripper is running, or two
servers answer the same action and you get whichever wins the race (rclpy only warns). The mock
**watches `/gripper`** instead, so its fake world keeps tracking the jaws whoever serves the action.

```bash
ssh mcpi
cd ~/mc_main_nav
docker compose -f docker/compose.yaml --profile robot build nav    # image mc-main-nav:robot
docker compose -f docker/compose.yaml --profile robot run --rm build-robot   # robot_interfaces + diff_nav only
docker compose -f docker/compose.yaml --profile robot up -d nav
docker compose -f docker/compose.yaml --profile robot up -d sim-base  # fake robot + camera, no hardware needed
```

The first image build on the Pi takes roughly 20 minutes (Nav2 from apt, natively on arm64). Building
the workspace takes about 40 seconds.

## Powering the mini PC off and on

Nothing needs stopping by hand. The three containers carry `restart: unless-stopped`, so Docker
brings them back when the daemon starts, and the `@reboot` cron entry restores the Tailscale funnel
90 s after boot. Leave them running and shut the machine down normally.

After powering it back on, one check tells you everything is up (run it from the laptop, and note
**`--doh-url`**: from a machine on the tailnet the name resolves to the node itself and would pass
even if the public endpoint were dead):

```bash
curl -s -o /dev/null -w "%{http_code}\n" --doh-url https://1.1.1.1/dns-query \
  https://mcpc.taile84e23.ts.net/health        # expect 200, within ~2 min of boot
ssh mcpc 'docker ps --format "{{.Names}}\t{{.Status}}"'
```

If the funnel has not come back, `ssh mcpc 'cd ~/mc_main_nav && ./scripts/expose.sh'` does it, and
the 2-minute keep-alive would have repaired it anyway. If the containers are missing (e.g. they were
stopped by hand before the reboot, which defeats `unless-stopped`), start them with the
`docker compose up -d` line above — the `CAMERA_BASE_URL`, tokens and `MOCK_OBJECTS` all have to be
on that command line, since none of it is stored in the repo.

## Checking that the machines see each other

ROS 2 finds nodes by itself when the machines share a network and `ROS_DOMAIN_ID`. Check from either side:

```bash
docker compose -f docker/compose.yaml exec engine bash -c \
  'source /opt/ros/humble/setup.bash && ros2 node list'
```

You should see the other machine's nodes (`/diff_nav`, `/pose_bridge`, Nav2's servers from the Pi;
`/bt_engine`, `/mock_robot`, `/table_map` from the mini PC). **If each machine only sees its own nodes,**
the Wi-Fi is blocking the multicast that ROS 2 uses to find nodes (common on guest or hotel networks, where
it's called client isolation). The fix is to list the machines' addresses explicitly with
[`docker/cyclonedds.xml`](../docker/cyclonedds.xml):

```bash
CYCLONEDDS_URI=file:///home/main/mc_main_nav/docker/cyclonedds.xml \
  docker compose -f docker/compose.yaml up -d engine mocks map     # on every machine
```

On the hackathon Wi-Fi (2026-09-18) multicast worked and this was not needed.

## Verified end to end (2026-09-18)

Laptop CLI → engine on the mini PC → mocks there → Nav2 + `diff_nav` + fake robot on the Pi:

```bash
BT_ENGINE_URL=http://192.168.50.125:8090 ./scripts/bt_exec.py run examples/nav_demo.xml
```

`success` in 17 s, including the deliberate "unknown object" failure inside the tree. Only one engine
should run on a ROS domain at a time: two `/bt_engine` nodes would both talk to the same robot.

## Reaching the engine from outside the network

The LLM runs on a server elsewhere, so the engine and the obstacle UI are published through
Cloudflare quick tunnels from the mini PC. **Anyone who learns a URL can reach it**, so the API is
protected by a token; only the two web pages and `/health` are open, so a browser can load them and
ask for the token.

```bash
# on the mini PC: start the services with a token
TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))'); echo "$TOKEN"
ENGINE_PORT=8090 BT_ENGINE_TOKEN=$TOKEN TABLE_MAP_TOKEN=$TOKEN \
  MOCK_DISABLE=navigate_to_object,rotate_in_place,is_at_object \
  docker compose -f docker/compose.yaml up -d --force-recreate engine mocks map

./scripts/expose.sh             # Tailscale Funnel, stable URLs (--stop to close)
./scripts/expose.sh cloudflare  # fallback: random trycloudflare.com URLs, no account
```

**Stable public URLs (Tailscale Funnel, machine `mcpc` on tailnet `taile84e23.ts.net`):**

| | URL |
|---|---|
| engine API + palette page | **https://mcpc.taile84e23.ts.net** |
| obstacle map UI | **https://mcpc.taile84e23.ts.net:8443** |

`tailscaled` runs in **userspace mode as the normal user** (`~/bin/tailscaled --tun=userspace-networking`),
so the mini PC needed no root. It does not start automatically after a reboot; run `./scripts/expose.sh`
again, and if it says "not logged in", run the `tailscale ... up` line it prints and approve the link.

From the LLM server:

```bash
BT_ENGINE_URL=https://<engine>.trycloudflare.com BT_ENGINE_TOKEN=<token> \
  ./scripts/bt_exec.py run tree.xml
# or plain HTTP: every API call carries  -H "X-BT-Token: <token>"   (or ?token=<token>)
```

In a browser, the palette page and the obstacle UI ask for the token once and remember it per browser.

Notes:
- Funnel serves 443 (engine) and 8443 (map UI); those are the only ports Funnel allows besides 10000.
- The Cloudflare fallback gives random URLs that change on every restart. Use it only if Funnel is down.
- Without `BT_ENGINE_TOKEN` / `TABLE_MAP_TOKEN` there is no token check at all — fine on the LAN, never
  with a tunnel running.
- **Only the Tailscale Funnel URLs are live.** The Cloudflare URLs handed out earlier
  (`*.trycloudflare.com`) were stopped when Funnel took over, and are dead — if someone says "the
  service is not up", check which URL they are using first.
- **Nothing on the mini PC starts by itself after a reboot** unless it is set up to. The services now
  carry `restart: ${RESTART_POLICY:-no}`, so deploy them with `RESTART_POLICY=unless-stopped` and
  Docker brings them back after a crash or a reboot (dev machines leave it `no`). `tailscaled` is the
  userspace one and has no service unit, so an `@reboot` cron entry starts it and the funnels.
- A cron entry on the mini PC runs `./scripts/expose.sh ensure` every 2 minutes: it **probes the real
  public URL** and repairs the funnel when that fails (log: `~/tunnels/keepalive.log`). Remove it with
  `crontab -e` if unwanted.
- **Never test a Funnel URL from a machine on the tailnet.** MagicDNS resolves `mcpc.taile84e23.ts.net`
  to `100.113.211.14` and the request goes over the VPN, so it succeeds even when the public endpoint
  is completely dead. Force public DNS instead:
  ```bash
  curl --doh-url https://1.1.1.1/dns-query https://mcpc.taile84e23.ts.net/health
  ```
- **Failure seen on 2026-09-19:** outside clients got `SSL routines::unexpected eof while reading`
  (schannel: `failed to receive handshake`) while `tailscale funnel status` cheerfully said "Funnel on".
  The cause was a **default-route flap**: the mini PC is dual-homed (`enp100s0` 192.168.68.51 metric
  100, `wlp98s0` 192.168.50.125 metric 600) and the default flips between them for ~2 s about every
  40 minutes, which kills tailscaled's control connection (`Received error: PollNetMap: ... use of
  closed network connection`). It reconnects, but the Funnel registration does not come back with it,
  so Tailscale's ingress servers stop forwarding and close the TLS connection themselves — the node
  logs nothing at all, because the connections never reach it. Fix:
  ```bash
  tailscale funnel --https=443 off && tailscale funnel --bg --https=443 http://localhost:8090
  ```
  then wait ~15 s for the ingress servers to pick it up. A TLS handshake error is never a token
  problem: a bad token returns a normal 401 JSON, and `/health` needs no token at all.
- Verified 2026-09-18 over both the Cloudflare tunnel and Tailscale Funnel: `/nodes` without a token is
  401, with the token 200, the pages load, and `nav_demo.xml` ran through the public URL to the robot Pi
  and succeeded.

## RViz

RViz runs on a machine with a screen (the laptop), and watches the robot over the network:

```bash
docker compose -f docker/compose.yaml --profile gui run --rm rviz
```
