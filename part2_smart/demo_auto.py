#!/usr/bin/env python3
"""
Automatic, narrated live demo of Part 2 (no Mininet commands to type).

It builds the two-path network, then for the LINK FAILURE scenario runs
  1. the STATIC controller  (baseline: never reacts)
  2. the DYNAMIC controller (fails over to the backup path)
showing the switch's flow rules, the server's live per-second results and the
controller's decisions, and finishes with a comparison table.

Start the controller first (Tab 1):
    ryu-manager --ofp-tcp-listen-port 6653 controller.py
Then (Tab 2):
    sudo python3 demo_auto.py               # link failure, static vs dynamic
    sudo python3 demo_auto.py --congestion  # also the congestion scenario
    sudo python3 demo_auto.py --auto        # no "press Enter" pauses
    sudo python3 demo_auto.py --cli         # open the mininet> prompt at the end
"""
import argparse
import json
import os
import re
import time
from functools import partial

from mininet.cli import CLI
from mininet.link import TCLink
from mininet.log import setLogLevel
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController

from run_tests import TwoPathTopo, send_ctl

HERE = os.path.dirname(os.path.abspath(__file__))
EVENTS = os.path.join(HERE, 'events.csv')
NAMES = {'10.0.0.1': 'h1', '10.0.0.2': 'h2', '10.0.0.3': 'h3'}
PATHS = {'4': 'port 4 = PRIMARY path (via s2)', '5': 'port 5 = BACKUP path (via s3)'}
B, CY, YE, GR, RE, X = '\033[1m', '\033[96m', '\033[93m', '\033[92m', '\033[91m', '\033[0m'
AUTO = False


def say(text):
    print(f'\n{CY}{B}>>> {text}{X}')


def note(text):
    print(f'    {text}')


def pause():
    if not AUTO:
        input(f'{YE}    [press Enter to continue]{X}')


def show_flows(net):
    """Print s1's three per-client path rules (priority 20) in plain words."""
    out = net.get('s1').cmd('ovs-ofctl -O OpenFlow13 --no-names dump-flows s1')
    print(f'{B}    Flow table of s1 (per-client path rules, priority=20):{X}')
    for line in out.splitlines():
        if 'priority=20' not in line:
            continue
        src = re.search(r'nw_src=([\d.]+)', line)
        port = re.search(r'output:(\d+)', line)
        pkts = re.search(r'n_packets=(\d+)', line)
        if src and port:
            ip, p = src.group(1), port.group(1)
            colour = GR if p == '4' else YE
            print(f'    {colour}{NAMES.get(ip, ip)} ({ip}) -> {PATHS.get(p, "port " + p)}{X}'
                  f'   packets so far: {pkts.group(1) if pkts else "?"}')


def show_events(since):
    """Print the controller's decisions logged after `since`."""
    if not os.path.exists(EVENTS):
        return
    for line in open(EVENTS).read().splitlines()[1:]:
        parts = line.split(',', 2)
        if len(parts) == 3 and float(parts[0]) >= since and 'reset' not in parts[2]:
            print(f'    {YE}{B}CONTROLLER: {parts[1].upper()} {parts[2]}{X}')


def live(path, seconds, t0, seen):
    """Show the server's per-second results for `seconds`."""
    end = time.time() + seconds
    while time.time() < end:
        if os.path.exists(path):
            text = open(path).read()
            lines = text.splitlines()
            if not text.endswith('\n'):
                lines = lines[:-1]                  # skip a half-written line
            for row in lines[1 + len(seen):]:
                seen.append(row)
                t, ip, mbps, loss, delay = row.split(',')
                mbps, loss = float(mbps), float(loss)
                colour = RE if mbps < 0.05 or loss > 5 else GR
                flag = '   <- NOTHING ARRIVING' if mbps < 0.05 else ''
                print(f'    {colour}t={float(t) - t0:5.1f}s  {NAMES.get(ip, ip)}  '
                      f'{mbps:5.2f} Mbps  loss {loss:5.1f}%  delay {float(delay):6.1f} ms{flag}{X}')
        time.sleep(0.5)


def results(net, tag, rates):
    srv_path = f'/tmp/demo_{tag}_srv.json'
    for _ in range(30):
        if os.path.exists(srv_path):
            break
        time.sleep(0.5)
    server = json.load(open(srv_path))['clients']
    total_sent = total_recv = 0
    print(f'{B}    {"client":8s}{"sent":>8s}{"received":>10s}{"loss":>9s}{X}')
    for h in rates:
        c = json.load(open(f'/tmp/demo_{tag}_{h}.json'))
        r = server.get(net.get(h).IP(), {}).get('received', 0)
        loss = (c['sent'] - r) / c['sent'] * 100 if c['sent'] else 0
        total_sent += c['sent']
        total_recv += r
        print(f'    {h:8s}{c["sent"]:8d}{r:10d}{loss:8.1f}%')
    total = (total_sent - total_recv) / total_sent * 100 if total_sent else 0
    print(f'{B}    {"TOTAL":8s}{total_sent:8d}{total_recv:10d}{total:8.1f}%{X}')
    return total


def run(net, mode, name, rates, dur, fail_at):
    tag = f'{mode}_{name}'
    for f in [f'/tmp/demo_{tag}_tl.csv', f'/tmp/demo_{tag}_srv.json'] + \
             [f'/tmp/demo_{tag}_{h}.json' for h in rates]:
        if os.path.exists(f):
            os.remove(f)

    net.configLinkStatus('s2', 's4', 'up')
    if not send_ctl({'type': 'mode', 'mode': mode}):
        raise SystemExit(1)
    time.sleep(3)

    say(f'{mode.upper()} controller - {name.replace("_", " ")} test')
    if mode == 'static':
        note('Static = the baseline: every client stays on the primary path, no reactions.')
    else:
        note('Dynamic = the smart controller: monitors every second, reroutes on')
        note('congestion or loss reports, and fails over when a link goes down.')
    note(f'Clients: ' + ', '.join(f'{h} {r} Mbps' for h, r in rates.items()) +
         f' = {sum(rates.values())} Mbps total, for {dur} s.')
    show_flows(net)
    pause()

    tl = f'/tmp/demo_{tag}_tl.csv'
    net.get('h4').cmd(f'python3 {HERE}/server.py --duration {dur + 3} --timeline {tl} '
                      f'--summary /tmp/demo_{tag}_srv.json > /dev/null 2>&1 &')
    time.sleep(1)
    t0 = time.time()
    for h, r in rates.items():
        net.get(h).cmd(f'python3 {HERE}/client.py --rate {r} --duration {dur} --name {h} '
                       f'--out /tmp/demo_{tag}_{h}.json > /dev/null 2>&1 &')
    say('Traffic started. Live results from the server on h4 (one line per client per second):')
    seen = []

    if fail_at:
        live(tl, fail_at, t0, seen)
        say('BREAKING the primary link s2-s4 now!')
        net.configLinkStatus('s2', 's4', 'down')
        fail_time = time.time()
        live(tl, 2, t0, seen)
        show_events(fail_time)
        show_flows(net)
        if mode == 'static':
            note(f'{RE}The rules still point to port 4, a dead link: the static controller did nothing.{X}')
        else:
            note(f'{GR}The rules now point to port 5: the controller moved everyone to the backup path.{X}')
        live(tl, max(0, dur - fail_at - 2), t0, seen)
    else:
        live(tl, 6, t0, seen)
        show_events(t0)
        show_flows(net)
        if mode == 'dynamic':
            note(f'{GR}The controller saw the primary path overloaded and moved a client to the backup.{X}')
        else:
            note(f'{RE}Everyone is still on the overloaded primary path.{X}')
        live(tl, max(0, dur - 6), t0, seen)

    time.sleep(3)
    say(f'Result: {mode} controller, {name.replace("_", " ")}')
    total = results(net, tag, rates)
    net.configLinkStatus('s2', 's4', 'up')
    pause()
    return total


def main():
    global AUTO
    ap = argparse.ArgumentParser()
    ap.add_argument('--auto', action='store_true', help='no pauses')
    ap.add_argument('--congestion', action='store_true', help='also run the congestion test')
    ap.add_argument('--cli', action='store_true', help='open the mininet> prompt at the end')
    args = ap.parse_args()
    AUTO = args.auto

    say('Building the network: h1,h2,h3 -> s1 -> (s2 primary | s3 backup) -> s4 -> h4 server')
    net = Mininet(topo=TwoPathTopo(), link=TCLink, autoSetMacs=True,
                  switch=partial(OVSSwitch, protocols='OpenFlow13'),
                  controller=partial(RemoteController, ip='127.0.0.1', port=6653))
    net.start()
    net.staticArp()
    time.sleep(3)
    say('Checking that every host can reach every other host (pingall)')
    if net.pingAll() > 0:
        print(f'{RE}Ping failed. Start the controller first: '
              f'ryu-manager --ofp-tcp-listen-port 6653 controller.py{X}')
        net.stop()
        return
    pause()

    summary = []
    tests = [('link_failure', {'h1': 4, 'h2': 3, 'h3': 2}, 20, 8)]
    if args.congestion:
        tests.append(('congestion', {'h1': 6, 'h2': 5, 'h3': 4}, 20, None))
    try:
        for name, rates, dur, fail_at in tests:
            for mode in ('static', 'dynamic'):
                summary.append((name, mode, run(net, mode, name, rates, dur, fail_at)))

        say('COMPARISON: static (baseline) vs dynamic SDN controller')
        print(f'{B}    {"test":14s}{"controller":12s}{"total loss":>11s}{X}')
        for name, mode, loss in summary:
            colour = GR if mode == 'dynamic' else RE
            print(f'    {colour}{name:14s}{mode:12s}{loss:10.1f}%{X}')
        note('The dynamic controller keeps traffic flowing; the static one cannot react.')

        if args.cli:
            say('Opening the mininet> prompt (type exit to finish)')
            CLI(net)
    finally:
        net.stop()


if __name__ == '__main__':
    setLogLevel('warning')
    main()
