"""
UDP traffic client: sends numbered, time-stamped datagrams at a fixed rate.
Uses a non-blocking socket, so like real UDP it never waits for the network:
when the local queue is full the packet is simply lost.

With --reliable it adds selective-repeat recovery on top of UDP:
  * every packet sent is kept in a buffer
  * a second thread listens for NACK messages from the server and resends
    exactly the packets the server says are missing
  * after the last packet it sends FIN ("I sent 0..N") and keeps answering
    NACKs until the server replies DONE ("I have everything") or --max-wait
    seconds pass, so the tail of the stream is recovered too
"""
import argparse
import json
import select
import socket
import struct
import threading
import time

HDR = struct.Struct('!Id')
FIN = b'FIN!'
NACK = b'NACK'
DONE = b'DONE'

p = argparse.ArgumentParser()
p.add_argument('--dst', default='10.0.0.4')
p.add_argument('--port', type=int, default=5001)
p.add_argument('--rate', type=float, default=5, help='Mbps')
p.add_argument('--duration', type=float, default=20, help='seconds')
p.add_argument('--size', type=int, default=1000, help='datagram bytes')
p.add_argument('--name', default='client')
p.add_argument('--out', default='client.json')
p.add_argument('--reliable', action='store_true', help='resend packets the server reports lost')
p.add_argument('--max-wait', type=float, default=60, help='reliable: give up resending after this many seconds')
a = p.parse_args()

if a.size < HDR.size or a.rate <= 0:
    raise SystemExit('size must be >= 12 bytes and rate > 0')

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setblocking(False)
dest = (a.dst, a.port)
interval = a.size * 8 / (a.rate * 1e6)
padding = b'x' * (a.size - HDR.size)

buffer = {}                 # seq -> packet bytes, kept for resending
counts = {'retransmitted': 0, 'nacks': 0, 'resend_failed': 0}
lock = threading.Lock()
stop = threading.Event()
done = threading.Event()          # server confirmed it has every packet


def nack_listener():
    """Answer the server's NACKs by resending the requested packets."""
    pending = []
    while not stop.is_set():
        ready, _, _ = select.select([sock], [], [], 0.1)
        if ready:
            try:
                data, _ = sock.recvfrom(65535)
            except (BlockingIOError, OSError):
                data = b''
            if data[:4] == DONE:
                done.set()
            elif data[:4] == NACK and len(data) >= 6:
                n = struct.unpack('!H', data[4:6])[0]
                seqs = struct.unpack(f'!{n}I', data[6:6 + 4 * n]) if len(data) >= 6 + 4 * n else ()
                counts['nacks'] += 1
                pending.extend(seqs)
        still = []
        for s in pending:
            with lock:
                pkt = buffer.get(s)
            if pkt is None:
                continue
            try:
                sock.sendto(pkt, dest)
                counts['retransmitted'] += 1
            except BlockingIOError:
                still.append(s)               # queue full: try again shortly
            except OSError:
                counts['resend_failed'] += 1
        pending = still


listener = None
if a.reliable:
    listener = threading.Thread(target=nack_listener, daemon=True)
    listener.start()

seq = dropped_local = errors = 0
start = time.time()
next_send = start
while time.time() - start < a.duration:
    pkt = HDR.pack(seq, time.time()) + padding
    if a.reliable:
        with lock:
            buffer[seq] = pkt
    try:
        sock.sendto(pkt, dest)
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

finish_wait = 0.0
if a.reliable and seq:
    t_end = time.time()
    end = t_end + a.max_wait          # announce the end, keep answering NACKs until DONE
    while time.time() < end and not done.is_set():
        try:
            sock.sendto(FIN + struct.pack('!I', seq - 1), dest)
        except OSError:
            pass
        time.sleep(0.5)
    finish_wait = time.time() - t_end
    stop.set()
    listener.join(timeout=1)

result = {'name': a.name, 'sent': seq, 'bytes': seq * a.size, 'duration': elapsed,
          'tx_mbps': seq * a.size * 8 / elapsed / 1e6,
          'dropped_local': dropped_local, 'errors': errors, 'reliable': a.reliable,
          'retransmitted': counts['retransmitted'], 'nacks_received': counts['nacks'],
          'all_confirmed': done.is_set(), 'extra_seconds_to_finish': round(finish_wait, 2)}
json.dump(result, open(a.out, 'w'))
print('CLIENT', result)
