"""Part 1 - UDP receiver: counts packets/bytes and calculates throughput."""
import argparse, json, socket, struct, time

p = argparse.ArgumentParser()
p.add_argument('--port', type=int, default=5001)
p.add_argument('--out', default='rx.json')
a = p.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
sock.bind(('0.0.0.0', a.port))
sock.settimeout(30)

received, nbytes, first, last, max_seq = 0, 0, None, None, -1
while True:
    try:
        data, _ = sock.recvfrom(65535)
    except socket.timeout:
        break
    seq = struct.unpack('!I', data[:4])[0]
    if seq == 0xFFFFFFFF:
        break
    now = time.time()
    if first is None:
        first = now
        sock.settimeout(3)
    last = now
    received += 1
    nbytes += len(data)
    max_seq = max(max_seq, seq)

duration = (last - first) if first and last > first else 1
result = {'received': received, 'bytes': nbytes, 'duration': duration,
          'rx_mbps': nbytes * 8 / duration / 1e6, 'max_seq': max_seq}
json.dump(result, open(a.out, 'w'))
print('RECEIVER', result)
