"""Part 1 - UDP sender: numbered packets at a chosen rate (non-blocking, like real UDP)."""
import argparse, json, socket, struct, time

p = argparse.ArgumentParser()
p.add_argument('--dst', default='10.0.0.2')
p.add_argument('--port', type=int, default=5001)
p.add_argument('--rate', type=float, default=5, help='Mbps')
p.add_argument('--duration', type=float, default=10, help='seconds')
p.add_argument('--size', type=int, default=1000, help='payload bytes')
p.add_argument('--out', default='tx.json')
a = p.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setblocking(False)
interval = a.size * 8 / (a.rate * 1e6)
padding = b'x' * (a.size - 12)

seq, dropped_local, start = 0, 0, time.time()
next_send = start
while time.time() - start < a.duration:
    try:
        sock.sendto(struct.pack('!Id', seq, time.time()) + padding, (a.dst, a.port))
    except BlockingIOError:
        dropped_local += 1
    seq += 1
    next_send += interval
    delay = next_send - time.time()
    if delay > 0:
        time.sleep(delay)
elapsed = time.time() - start

time.sleep(0.5)
for _ in range(5):
    try:
        sock.sendto(struct.pack('!Id', 0xFFFFFFFF, 0), (a.dst, a.port))
    except BlockingIOError:
        pass
    time.sleep(0.1)

result = {'sent': seq, 'bytes': seq * a.size, 'duration': elapsed,
          'tx_mbps': seq * a.size * 8 / elapsed / 1e6, 'dropped_local': dropped_local}
json.dump(result, open(a.out, 'w'))
print('SENDER', result)
