"""
UDP traffic client: sends numbered, time-stamped datagrams at a fixed rate.
Uses a non-blocking socket, so like real UDP it never waits for the network:
when the local queue is full the packet is simply lost.
"""
import argparse
import json
import socket
import struct
import time

HDR = struct.Struct('!Id')

p = argparse.ArgumentParser()
p.add_argument('--dst', default='10.0.0.4')
p.add_argument('--port', type=int, default=5001)
p.add_argument('--rate', type=float, default=5, help='Mbps')
p.add_argument('--duration', type=float, default=20, help='seconds')
p.add_argument('--size', type=int, default=1000, help='datagram bytes')
p.add_argument('--name', default='client')
p.add_argument('--out', default='client.json')
a = p.parse_args()

if a.size < HDR.size or a.rate <= 0:
    raise SystemExit('size must be >= 12 bytes and rate > 0')

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setblocking(False)
interval = a.size * 8 / (a.rate * 1e6)
padding = b'x' * (a.size - HDR.size)

seq = dropped_local = errors = 0
start = time.time()
next_send = start
while time.time() - start < a.duration:
    try:
        sock.sendto(HDR.pack(seq, time.time()) + padding, (a.dst, a.port))
    except BlockingIOError:
        dropped_local += 1            # local queue full
    except OSError:
        errors += 1                   # e.g. network unreachable
    seq += 1
    next_send += interval
    delay = next_send - time.time()
    if delay > 0:
        time.sleep(delay)
elapsed = time.time() - start

result = {'name': a.name, 'sent': seq, 'bytes': seq * a.size, 'duration': elapsed,
          'tx_mbps': seq * a.size * 8 / elapsed / 1e6,
          'dropped_local': dropped_local, 'errors': errors}
json.dump(result, open(a.out, 'w'))
print('CLIENT', result)
