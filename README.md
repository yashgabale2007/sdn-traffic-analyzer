# Bandwidth Meter and Traffic Analyzer (SDN + UDP Socket Programming)

A UDP traffic measurement application deployed on a Mininet network controlled by a
Ryu SDN controller. It compares application-level measurements with SDN flow
statistics and shows a **dynamic controller** that reacts to congestion, to loss
reported by the application, and to link failures. A static controller serves as the
baseline for comparison.

## 1. Architecture

### Part 1: Bandwidth meter (single bottleneck)

```
h1 (sender.py) --100 Mbps-- s1 --10 Mbps-- h2 (receiver.py)
                             |
                     Ryu monitor.py (flow stats every 1 s)
```

### Part 2: Smart traffic analyzer (multi-client, two paths)

```
h1 ─┐                  ┌── s2 ──┐      primary path (10 Mbps)
h2 ─┼── s1 ────────────┤        ├── s4 ── h4 (server.py)
h3 ─┘   (client.py)    └── s3 ──┘      backup path  (10 Mbps)
                 |
         Ryu controller.py  <── UNIX socket ── server.py (loss reports)
                            <── UNIX socket ── run_tests.py (mode / reset)
```

| Component | Role |
|---|---|
| `client.py` | UDP client: numbered, time-stamped datagrams at a fixed rate; non-blocking socket, error handling |
| `server.py` | Multi-client UDP server; a receiver thread plus a reporter thread; per-client throughput, loss, one-way delay; sends loss reports to the controller |
| `controller.py` | Ryu app: proactive IPv4 routing, flow-stats monitoring, congestion load balancing, app-feedback reaction, link-failure failover; `static` or `dynamic` mode |
| `run_tests.py` | Builds the topology and runs each scenario in both modes, collecting results |
| `plot_results.py` | Static vs dynamic comparison graphs, recovery and reaction times |

**Communication flow:** clients → s1 → (s2 or s3) → s4 → server. Every second the
controller polls s1's flow counters, and the server reports each client's loss to the
controller over a UNIX datagram socket. The controller changes paths with
OpenFlow `FLOW_MOD` messages.

### Dynamic SDN mechanisms (dynamic mode)

1. **Monitoring:** per-client rate from flow statistics every second (`flow_stats.csv`).
2. **Load balancing:** if the primary path carries more than 8 Mbps (80% of capacity),
   the controller moves the largest flow that fits onto the backup path.
3. **Application feedback:** if the server reports more than 5% loss for a client,
   the controller reroutes that client.
4. **Failure recovery:** when a primary link goes down (OpenFlow `PORT_STATUS`), all
   flows switch to the backup path at once.

A 3-second cooldown prevents route flapping. All decisions are logged in `events.csv`.

## 2. Setup (Ubuntu 24.04, ARM64 or x86)

```bash
sudo apt update
sudo apt install -y git mininet openvswitch-switch python3-matplotlib python3-pandas
sudo systemctl enable --now openvswitch-switch

# Ryu needs Python 3.9
sudo add-apt-repository -y ppa:deadsnakes/ppa && sudo apt update
sudo apt install -y python3.9 python3.9-venv python3.9-dev
python3.9 -m venv ~/ryu-env && source ~/ryu-env/bin/activate
pip install "setuptools<58" wheel
pip install --no-build-isolation ryu==4.34
pip install eventlet==0.30.2 "dnspython<2"
ryu-manager --version        # ryu-manager 4.34
```

## 3. How to run

> **Order matters:** `sudo mn -c` kills any running `ryu-manager`, so always clean up
> first, then start the controller, then run the tests.

### Part 1

```bash
sudo mn -c                                                # terminal 2
cd part1_meter && source ~/ryu-env/bin/activate           # terminal 1
ryu-manager --ofp-tcp-listen-port 6653 monitor.py
cd part1_meter && sudo python3 run_experiment.py          # terminal 2
python3 plot.py                                           # results.png
```

### Part 2

```bash
sudo mn -c                                                # terminal 2
cd part2_smart && source ~/ryu-env/bin/activate           # terminal 1
ryu-manager --ofp-tcp-listen-port 6653 controller.py
cd part2_smart && sudo python3 run_tests.py               # terminal 2 (about 3 min)
python3 plot_results.py                                   # comparison.png, timelines.png
```

Run a single case: `sudo python3 run_tests.py --modes dynamic --scenarios link_failure`

## 4. Test cases

| # | Test | Expected result |
|---|---|---|
| T1 | `pingAll` on both topologies | 0% dropped (controller installs routes) |
| T2 | Part 1, load 1–8 Mbps | ~0% loss; SDN rate ≈ app rate (+ header overhead) |
| T3 | Part 1, load 10–15 Mbps | Throughput flat at ~9.6 Mbps; loss ≈ (offered − 9.6) / offered |
| T4 | Part 2 congestion, static | All 15 Mbps on primary → ~35% loss |
| T5 | Part 2 congestion, dynamic | Controller moves one flow to backup within a few seconds → loss drops sharply |
| T6 | Part 2 link failure, static | After the s2–s4 failure, traffic stops → large loss, never recovers |
| T7 | Part 2 link failure, dynamic | `PORT_STATUS` → failover to backup in ~1 s → small loss |
| T8 | Multiple clients | Server tracks h1, h2 and h3 separately at the same time |
| T9 | Controller down | `run_tests.py` stops with a clear message; client counts send errors |

## 5. Results

Part 1 shows that SDN flow counters agree with the application to within a few
percent. The difference comes from header bytes (42 B per 1000 B datagram) and
the 1-second polling interval. Part 2 compares the static and dynamic controllers on
loss, throughput, delay and recovery time (see `part2_smart/results/`).

## 6. Repository layout

```
part1_meter/   monitor.py sender.py receiver.py run_experiment.py plot.py
part2_smart/   controller.py client.py server.py run_tests.py plot_results.py
```
