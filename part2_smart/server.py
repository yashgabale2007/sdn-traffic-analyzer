"""
Multi-client UDP server (runs on h4).

Threads:
  * receiver thread - reads datagrams from all clients on one UDP socket
  * reporter thread - every second computes per-client throughput, loss and
                      one-way delay, writes a timeline and sends a loss report
                      to the SDN controller (application -> SDN feedback)
  * nack thread     - only with --reliable: every 0.2 s finds the sequence
                      numbers that are missing for each client and sends the
                      client a NACK ("please resend these"), like selective repeat

Reliable mode messages (all UDP):
  data   : 4-byte seq + 8-byte send time + padding            client -> server
  FIN    : b'FIN!' + 4-byte last seq ("I have sent 0..last")  client -> server
  NACK   : b'NACK' + 2-byte count + count x 4-byte seq        server -> client
  DONE   : b'DONE'  ("I have every packet 0..last")             server -> client

With --reliable the server keeps running until every client is complete
(or --duration runs out), so the final loss is 0% whenever a path exists.
"""
import argparse
import json
import socket
import struct
import threading
import time

HDR = struct.Struct('!Id')          # sequence number, send timestamp
FIN = b'FIN!'
NACK = b'NACK'
DONE = b'DONE'
NACK_MAX = 100                      # sequence numbers per NACK message (paces the resends)
NACK_EVERY = 0.2                    # seconds between NACK rounds
NACK_RETRY = 0.5                    # wait this long before asking for the same packet again

p = argparse.ArgumentParser()
p.add_argument('--port', type=int, default=5001)
p.add_argument('--duration', type=float, default=30)
p.add_argument('--timeline', default='timeline.csv')
p.add_argument('--summary', default='server.json')
p.add_argument('--feedback', default='/tmp/sdn_feedback.sock')
p.add_argument('--no-feedback', action='store_true')
p.add_argument('--reliable', action='store_true', help='ask clients to resend lost packets')
p.add_argument('--resend-log', default='', help='reliable: per-second log of NACKs and recovered packets')
p.add_argument('--expect', type=int, default=0, help='reliable: stop early once this many clients are complete')
a = p.parse_args()

lock = threading.Lock()
stats = {}                          # client ip -> counters
stop = threading.Event()


def new_client(addr):
    return {'addr': addr, 'received': 0, 'bytes': 0, 'max_seq': -1, 'delay_sum': 0.0,
            'iv_recv': 0, 'iv_bytes': 0, 'iv_delay': 0.0, 'iv_start_seq': -1,
            'malformed': 0, 'duplicates': 0, 'recovered': 0,
            'got': set(), 'low': 0, 'nacked': {}, 'fin': None, 'nack_msgs': 0,
            'complete': False, 'complete_at': None, 'last_rx': time.time(),
            'iv_asked': 0, 'iv_lo': None, 'iv_hi': None, 'iv_recovered': 0, 'done_logged': False}


def receiver(sock):
    while not stop.is_set():
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError as e:
            print('receive error:', e)
            continue
        now = time.time()
        with lock:
            st = stats.setdefault(addr[0], new_client(addr))
            st['addr'] = addr
            st['last_rx'] = now
            if data[:4] == FIN and len(data) >= 8:
                st['fin'] = struct.unpack('!I', data[4:8])[0]
                continue
            if len(data) < HDR.size:
                st['malformed'] += 1
                continue
            seq, sent_at = HDR.unpack_from(data)
            if seq < st['low'] or seq in st['got']:
                st['duplicates'] += 1
                continue
            st['got'].add(seq)
            if seq in st['nacked']:
                del st['nacked'][seq]
                st['recovered'] += 1         # arrived after we asked for it again
                st['iv_recovered'] += 1
            delay = max(0.0, now - sent_at)
            st['received'] += 1
            st['bytes'] += len(data)
            st['delay_sum'] += delay
            if seq > st['max_seq']:          # new data: counts for this second's stats
                st['max_seq'] = seq
                st['iv_recv'] += 1
                st['iv_delay'] += delay
            st['iv_bytes'] += len(data)


def nacker(sock):
    """Selective repeat: tell each client exactly which packets are missing."""
    while not stop.wait(NACK_EVERY):
        now = time.time()
        jobs = []
        with lock:
            for st in stats.values():
                while st['low'] in st['got']:
                    st['got'].discard(st['low'])   # everything below `low` is complete
                    st['low'] += 1
                if st['fin'] is not None and st['low'] > st['fin']:
                    if not st['complete']:
                        st['complete'], st['complete_at'] = True, now
                    jobs.append((st['addr'], None))        # tell the client: all received
                    continue
                top = st['max_seq'] if st['fin'] is None else max(st['max_seq'], st['fin'] + 1)
                missing = []
                for s in range(st['low'], top):
                    if s not in st['got'] and now - st['nacked'].get(s, 0) > NACK_RETRY:
                        missing.append(s)
                        if len(missing) == NACK_MAX:
                            break
                for s in missing:
                    st['nacked'][s] = now
                if missing:
                    st['iv_asked'] += len(missing)
                    st['iv_lo'] = missing[0] if st['iv_lo'] is None else min(st['iv_lo'], missing[0])
                    st['iv_hi'] = missing[-1] if st['iv_hi'] is None else max(st['iv_hi'], missing[-1])
                    st['nack_msgs'] += 1
                    jobs.append((st['addr'], missing))
        for addr, missing in jobs:
            if missing is None:
                msg = DONE
            else:
                msg = NACK + struct.pack('!H', len(missing)) + struct.pack(f'!{len(missing)}I', *missing)
            try:
                sock.sendto(msg, addr)
            except OSError:
                pass                                # path down: we will ask again later


def reporter(writer, out):
    rlog = None
    if a.reliable and a.resend_log:
        rlog = open(a.resend_log, 'w')
        rlog.write('time,client,asked,first_seq,last_seq,recovered,still_missing,done\n')
    fb = None
    if not a.no_feedback:
        fb = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    warned = False
    last = time.time()
    while not stop.wait(1.0):
        now = time.time()
        span = now - last
        last = now
        with lock:
            rows = []
            for ip, st in stats.items():
                expected = st['max_seq'] - st['iv_start_seq']
                loss = 0.0
                if expected > 0:
                    loss = max(0.0, (expected - st['iv_recv']) / expected * 100)
                elif st['iv_recv'] == 0 and st['received'] > 0 and st['fin'] is None:
                    loss = 100.0       # a known client sent nothing that arrived this second
                mbps = st['iv_bytes'] * 8 / span / 1e6
                delay = st['iv_delay'] / st['iv_recv'] * 1000 if st['iv_recv'] else 0.0
                rows.append((ip, mbps, loss, delay))
                st['iv_start_seq'] = st['max_seq']
                st['iv_recv'] = st['iv_bytes'] = 0
                st['iv_delay'] = 0.0
                if rlog and (st['iv_asked'] or st['iv_recovered'] or (st['complete'] and not st['done_logged'])):
                    top = max(st['max_seq'], st['fin'] if st['fin'] is not None else -1) + 1
                    still = max(0, top - st['low'] - len(st['got']))
                    done = 1 if st['complete'] and not st['done_logged'] else 0
                    st['done_logged'] = st['done_logged'] or bool(done)
                    rlog.write(f"{now:.3f},{ip},{st['iv_asked']},{st['iv_lo'] if st['iv_lo'] is not None else ''},"
                               f"{st['iv_hi'] if st['iv_hi'] is not None else ''},{st['iv_recovered']},{still},{done}\n")
                st['iv_asked'] = st['iv_recovered'] = 0
                st['iv_lo'] = st['iv_hi'] = None
        if rlog:
            rlog.flush()
        for ip, mbps, loss, delay in rows:
            writer.write(f'{now:.3f},{ip},{mbps:.3f},{loss:.2f},{delay:.2f}\n')
            if fb:
                msg = json.dumps({'type': 'report', 'client': ip,
                                  'mbps': round(mbps, 3), 'loss': round(loss, 2)})
                try:
                    fb.sendto(msg.encode(), a.feedback)
                except OSError as e:
                    if not warned:
                        print('controller feedback unavailable:', e)
                        warned = True
        out.flush()


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    sock.bind(('0.0.0.0', a.port))
    sock.settimeout(0.5)

    out = open(a.timeline, 'w')
    out.write('time,client,mbps,loss_pct,delay_ms\n')

    threads = [threading.Thread(target=receiver, args=(sock,), daemon=True),
               threading.Thread(target=reporter, args=(out, out), daemon=True)]
    if a.reliable:
        threads.append(threading.Thread(target=nacker, args=(sock,), daemon=True))
    for t in threads:
        t.start()
    print(f'server listening on UDP {a.port} for {a.duration}s'
          f'{" (reliable mode: NACK + resend)" if a.reliable else ""}')
    start = time.time()
    try:
        while time.time() - start < a.duration:
            time.sleep(0.5)
            if a.reliable and a.expect:
                with lock:
                    done = [st for st in stats.values() if st['complete']]
                    quiet = all(time.time() - st['last_rx'] > 2 for st in stats.values())
                if len(done) >= a.expect and quiet:
                    time.sleep(1.5)                 # keep sending DONE a few more times
                    break
    except KeyboardInterrupt:
        pass
    stop.set()
    for t in threads:
        t.join(timeout=2)
    out.close()

    with lock:
        summary = {ip: {'received': st['received'], 'bytes': st['bytes'],
                        'max_seq': st['max_seq'], 'malformed': st['malformed'],
                        'duplicates': st['duplicates'], 'recovered': st['recovered'],
                        'nack_messages': st['nack_msgs'], 'complete': st['complete'],
                        'first_try': st['received'] - st['recovered'],
                        'avg_delay_ms': st['delay_sum'] / st['received'] * 1000
                        if st['received'] else 0.0}
                   for ip, st in stats.items()}
    json.dump({'clients': summary, 'reliable': a.reliable}, open(a.summary, 'w'), indent=2)
    print('SERVER', summary)


if __name__ == '__main__':
    main()
