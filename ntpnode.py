#!/usr/bin/python3

# Copyright (C) 2022  Miroslav Lichvar <mlichvar@redhat.com>
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import argparse
import collections
import enum
import ipaddress
import logging
import random
import select
import socket
import struct
import sys
import time

from dataclasses import dataclass

class Ntp5Leap(enum.IntEnum):
    NORMAL = 0
    INSERT = 1
    DELETE = 2
    UNKNOWN = 3

class Ntp4Leap(enum.IntEnum):
    NORMAL = 0
    INSERT = 1
    DELETE = 2
    UNSYNCHRONIZED = 3

class NtpMode(enum.IntEnum):
    CLIENT = 3
    SERVER = 4

class Ntp5Timescale(enum.IntEnum):
    UTC = 0
    TAI = 1
    UT1 = 2
    SMEARED_UTC = 3

class Ntp5Flag(enum.IntEnum):
    SYNCHRONIZED = 0x1
    INTERLEAVED = 0x2
    AUTH_NAK = 0x4

class NtpEF(enum.IntEnum):
    PADDING = 0xf501
    MAC = 0xf502
    REFERENCE_IDS_REQ = 0xf503
    REFERENCE_IDS_RESP = 0xf504
    SERVER_INFO = 0xf505
    CORRECTION = 0xf506
    REFERENCE_TS = 0xf507
    MONOTONIC_RX_TS = 0xf508
    SECONDARY_RX_TS = 0xf509
    DRAFT_ID = 0xf5ff

OUR_DRAFT_ID = "draft-ietf-ntp-ntpv5-02+"

REFERENCE_IDS_OCTETS = 4096 // 8

class Ntp4MagicRefTs(enum.IntEnum):
    NTP5 = struct.unpack("!Q", b"NTP5DRFT")[0]

def read_clock(precision):
    return int((time.time() + 0x83aa7e80) * 4294967296) ^ \
           int(random.getrandbits(32 + precision))

@dataclass
class NtpMessage:
    # Both NTPv5 and NTPv4
    version: int
    mode: NtpMode
    stratum: int
    poll: int
    precision: int
    root_delay: float
    root_disp: float
    receive_ts: int
    transmit_ts: int

    # NTPv5-specific
    leap5: Ntp5Leap
    timescale: Ntp5Timescale
    era: int
    flags: int
    server_cookie: int
    client_cookie: int

    # NTPv4-specific
    leap4: Ntp4Leap
    reference_id: int
    reference_ts: int
    origin_ts: int

    # Extension fields
    server_info: int = None # bit field
    reference_ids_req: tuple = None # (offset, length)
    reference_ids_resp: bytes = None
    reference_ts_: int = None
    secondary_rx_ts: dict = None # timescale -> (era, timestamp)
    draft_id: str = None

    @classmethod
    def decode(cls, message):
        if len(message) < 48 or len(message) % 4 != 0:
            raise ValueError("Invalid length {}".format(len(message)))

        lvm = message[0]

        version = (lvm >> 3) & 7
        mode = NtpMode(lvm & 7)

        if version == 5:
            leap5 = Ntp5Leap(lvm >> 6)
            leap4 = None
            _, stratum, poll, precision, timescale, era, flags, \
                root_delay, root_disp, server_cookie, client_cookie, receive_ts, transmit_ts = \
                     struct.unpack("!BBbbBBHIIQQQQ", message[:48])
            timescale = Ntp5Timescale(timescale)
            root_delay = root_delay / 2**28
            root_disp = root_disp / 2**28
            reference_id = reference_ts = origin_ts = None
        elif version == 4:
            leap4 = Ntp4Leap(lvm >> 6)
            leap5 = None
            _, stratum, poll, precision, root_delay, root_disp, reference_id, \
                reference_ts, origin_ts, receive_ts, transmit_ts = \
                     struct.unpack("!BBbbIIIQQQQ", message[:48])
            timescale = Ntp5Timescale.UTC
            era = flags = server_cookie = client_cookie = None
            root_delay = root_delay / 2**16
            root_disp = root_disp / 2**16
        else:
            raise ValueError("Invalid version {}".format(version))

        extensions = message[48:]
        server_info = reference_ids_req = reference_ids_resp = reference_ts_ = None
        secondary_rx_ts = draft_id = None

        while len(extensions) > 0:
            # Ignore NTPv4 MAC
            if version == 4 and len(extensions) <= 24:
                break
            (ef_type, ef_len) = struct.unpack("!HH", extensions[:4])
            if ef_len > len(extensions) or ef_len < 4 or \
                    (version == 4 and (ef_len < 16 or ef_len % 4 != 0)):
                raise ValueError("Invalid format")

            if ef_type == NtpEF.REFERENCE_IDS_REQ:
                reference_ids_req = (struct.unpack("!H", extensions[4:6])[0], ef_len - 4)
            elif ef_type == NtpEF.REFERENCE_IDS_RESP:
                reference_ids_resp = extensions[4:ef_len]
            elif ef_type == NtpEF.SERVER_INFO and ef_len == 8:
                server_info = struct.unpack("!H", extensions[4:6])[0]
            elif ef_type == NtpEF.REFERENCE_TS and ef_len == 12:
                reference_ts_ = struct.unpack("!Q", extensions[4:12])[0]
            elif ef_type == NtpEF.SECONDARY_RX_TS and ef_len == 16:
                sec_scale, sec_era, _, sec_ts = struct.unpack("!BBHQ", extensions[4:16])
                if sec_scale in (Ntp5Timescale.UTC, ):
                    if secondary_rx_ts is None:
                        secondary_rx_ts = {}
                    secondary_rx_ts[Ntp5Timescale(sec_scale)] = (sec_era, sec_ts)
            elif ef_type == NtpEF.DRAFT_ID:
                try:
                    draft_id = extensions[4:ef_len].decode('ascii')
                except UnicodeDecodeError:
                    pass

            extensions = extensions[(ef_len + 3) & 0xfffc:]

        if version == 5:
            if draft_id is None:
                raise ValueError("Missing draft ID")
            if draft_id != OUR_DRAFT_ID:
                raise ValueError("Unknown draft ID {}".format(draft_id))

        return cls(version, mode, stratum, poll, precision, root_delay, root_disp,
                   receive_ts, transmit_ts, leap5, timescale, era, flags,
                   server_cookie, client_cookie, leap4, reference_id, reference_ts, origin_ts,
                   server_info, reference_ids_req, reference_ids_resp, reference_ts_,
                   secondary_rx_ts, draft_id)

    def get_rint(self, value):
        if self.version == 5:
            return min(int(value * 2**28), 0xffffffff)
        else:
            return min(int(value * 2**16), 0xffffffff)

    def encode_ef(self, ef_type, ef_body):
        pad_len = 0 if len(ef_body) % 4 == 0 else 4 - len(ef_body) % 4
        return struct.pack("!HH", ef_type, 4 + len(ef_body)) + ef_body + b"\x00" * pad_len

    def encode(self, target_len=0):
        stratum = self.stratum if self.stratum < 16 else 0
        if self.version == 5:
            header = struct.pack("!BBbbBBHIIQQQQ",
                                 (self.leap5 << 6) | (self.version << 3) | self.mode, stratum,
                                 self.poll, self.precision, self.timescale, self.era, self.flags,
                                 self.get_rint(self.root_delay), self.get_rint(self.root_disp),
                                 self.server_cookie, self.client_cookie, self.receive_ts, self.transmit_ts)
        elif self.version == 4:
            header = struct.pack("!BBbbIIIQQQQ",
                                 (self.leap4 << 6) | (self.version << 3) | self.mode, stratum,
                                 self.poll, self.precision, self.get_rint(self.root_delay),
                                 self.get_rint(self.root_disp), self.reference_id, self.reference_ts,
                                 self.origin_ts, self.receive_ts, self.transmit_ts)
        else:
            assert False

        message = header

        if self.server_info is not None:
            message += self.encode_ef(NtpEF.SERVER_INFO,
                                      struct.pack("!HH", self.server_info, 0))
        if self.reference_ids_req is not None:
            message += self.encode_ef(NtpEF.REFERENCE_IDS_REQ,
                    struct.pack("!H", self.reference_ids_req[0]) + (self.reference_ids_req[1] - 2) * " ".encode())
        if self.reference_ids_resp is not None:
            message += self.encode_ef(NtpEF.REFERENCE_IDS_RESP, self.reference_ids_resp)
        if self.reference_ts_ is not None:
            message += self.encode_ef(NtpEF.REFERENCE_TS, struct.pack("!Q", self.reference_ts_))
        if self.secondary_rx_ts is not None:
            for timescale, (era, ts) in self.secondary_rx_ts.items():
                message += self.encode_ef(NtpEF.SECONDARY_RX_TS,
                                          struct.pack("!BBHQ", timescale, era, 0, ts))
        if self.draft_id is not None:
            message += self.encode_ef(NtpEF.DRAFT_ID, self.draft_id.encode('ascii'))

        if len(message) < target_len and self.version == 5:
            assert len(message) + 4 <= target_len
            message += self.encode_ef(NtpEF.PADDING, b'\x00' * (target_len - len(message) - 4))

        return message

@dataclass
class NtpSample:
    offset: float
    delay: float
    disp: float
    root_delay: float
    root_disp: float
    stratum: int

class NtpClient:
    def __init__(self, dispersion_rate, version, interleaved, refids_fragments):
        self.dispersion_rate = dispersion_rate
        self.precision = -20
        if version in (4, 5):
            self.version = version
            self.auto_version = False
        else:
            self.version = 4
            self.auto_version = True
        self.interleaved = interleaved
        self.refids_fragments = refids_fragments
        self.timescale = Ntp5Timescale.UTC

        self.missed_responses = 0

        self.reference_ids = 0
        self.next_refids_fragment = 0
        self.complete_refids = False

        self.last_request = None
        self.prev_request = None
        self.prev_response = None
        self.last_transmit_ts = None

        self.sample = None

    def make_request(self):
        # Do not make an interleaved request if too many responses were missed
        interleaved = self.interleaved and self.prev_response is not None and \
                self.prev_response.version == self.version and \
                self.missed_responses <= 4

        timescale = era = flags = server_cookie = client_cookie = None
        reference_id = reference_ts = origin_ts = None
        server_info = reference_ids_req = reference_ts_ = secondary_rx_ts = draft_id = None

        receive_ts = transmit_ts = 0

        if self.version == 5:
            timescale = self.timescale
            flags = 0
            era = 0
            if self.interleaved:
                flags |= Ntp5Flag.INTERLEAVED
            if interleaved:
                server_cookie = self.prev_response.server_cookie
            else:
                server_cookie = 0
            client_cookie = random.getrandbits(64)
            server_info = 0
            reference_ids_req = (self.next_refids_fragment * (REFERENCE_IDS_OCTETS // self.refids_fragments),
                                 (REFERENCE_IDS_OCTETS // self.refids_fragments))
            reference_ts_ = 0
            secondary_rx_ts = {timescale: (0, 0)}
            draft_id = OUR_DRAFT_ID
        elif self.version == 4:
            reference_id = origin_ts = 0
            reference_ts = Ntp4MagicRefTs.NTP5 if self.auto_version else 0
            transmit_ts = random.getrandbits(64)
            if interleaved:
                origin_ts = self.prev_response.receive_ts
                receive_ts = random.getrandbits(64)
        else:
            assert False

        return NtpMessage(self.version, NtpMode.CLIENT, 0, 0, 0, 0, 0,
                          receive_ts, transmit_ts, Ntp5Leap.NORMAL, timescale, era, flags,
                          server_cookie, client_cookie, Ntp4Leap.NORMAL, reference_id,
                          reference_ts, origin_ts,
                          server_info=server_info, reference_ids_req=reference_ids_req,
                          reference_ts_=reference_ts_, secondary_rx_ts=secondary_rx_ts,
                          draft_id=draft_id)

    def send_request(self, sock):
        self.missed_responses += 1

        # Downgrade to NTPv4 if server stopped responding
        if self.auto_version and self.version == 5 and self.missed_responses > 8:
            self.version = 4

        self.prev_request = self.last_request
        self.last_request = self.make_request()

        self.prev_transmit_ts = self.last_transmit_ts
        self.last_transmit_ts = read_clock(self.precision)

        message = self.last_request.encode()
        sock.send(message)
        logging.info("Sent NTPv{} request ({}) to {}".format(self.last_request.version, len(message),
                                                             sock.getpeername()))
        logging.debug("  {}".format(self.last_request))

    def merge_refids_fragment(self, fragment):
        start = self.next_refids_fragment * (REFERENCE_IDS_OCTETS // self.refids_fragments)
        end = min(REFERENCE_IDS_OCTETS, start + (REFERENCE_IDS_OCTETS // self.refids_fragments))
        mask = (((1 << (8 * start)) - 1) << (8 * (REFERENCE_IDS_OCTETS - start))) | \
                ((1 << (8 * (REFERENCE_IDS_OCTETS - end))) - 1)
        self.reference_ids = (self.reference_ids & mask) | \
                             (fragment << ((REFERENCE_IDS_OCTETS - end) * 8))
        if end < REFERENCE_IDS_OCTETS:
            self.next_refids_fragment += 1
        else:
            self.next_refids_fragment = 0
            self.complete_refids = True

    def receive_response(self, sock):
        try:
            message = sock.recv(1024)
        except Exception as e:
            logging.error("Could not receive response from {}: {}".format(sock.getpeername(), e))
            return

        receive_ts = read_clock(self.precision)

        try:
            response = NtpMessage.decode(message)
        except ValueError as e:
            logging.error(e)
            return

        logging.info("Received NTPv{} response ({}) from {}".format(response.version, len(message),
                                                                  sock.getpeername()))
        logging.debug("  {}".format(response))

        # Ignore unexpected responses
        if self.missed_responses == 0 or response.mode != NtpMode.SERVER:
            return

        if response.version == 5:
            if response.client_cookie != self.last_request.client_cookie:
                logging.info("  Bogus response")
                return
            interleaved = response.flags & Ntp5Flag.INTERLEAVED != 0
        elif response.version == 4:
            if response.origin_ts == self.last_request.receive_ts:
                interleaved = True
            elif response.origin_ts == self.last_request.transmit_ts:
                interleaved = False
            else:
                logging.info("  Bogus response")
                return
            self.reference_ids = 0
        else:
            return

        self.reference_id = response.reference_id

        if (response.version == 5 and not response.flags & Ntp5Flag.SYNCHRONIZED) or \
                (response.version == 4 and not response.leap4 != Ntp4Leap.UNSYNCHRONIZED) or \
                response.stratum == 0 or \
                response.root_delay / 2 + response.root_disp > 16:
            logging.info("  Unsynchronized response")
            return

        if response.timescale != self.timescale:
            logging.info("  Unsupported timescale")
            return

        if response.version == 5:
            if response.reference_ids_resp is not None:
                self.merge_refids_fragment(int.from_bytes(response.reference_ids_resp, byteorder='big'))
            else:
                # Server cannot be synchronized to other servers (no loop)
                self.reference_ids = 0

        if interleaved:
            T1 = self.prev_transmit_ts
            T2 = self.prev_response.receive_ts
            T3 = response.transmit_ts
            T4 = self.prev_receive_ts
        else:
            T1 = self.last_transmit_ts
            T2 = response.receive_ts
            T3 = response.transmit_ts
            T4 = receive_ts

        offset = 0.5 * ((T2 - T1) + (T3 - T4)) / 2**32
        delay = abs((T4 - T1) - (T3 - T2)) / 2**32

        self.sample = NtpSample(offset, delay, delay * self.dispersion_rate,
                                delay + response.root_delay, delay * self.dispersion_rate +
                                response.root_disp, response.stratum)

        logging.info("  {} mode offset={:+.6f} delay={:.6f} rdist={:.6f} stratum={}".format(
                       "Interleaved" if interleaved else "Basic", self.sample.offset, self.sample.delay,
                       self.sample.root_delay / 2 + self.sample.root_disp, self.sample.stratum))

        # Update client state
        self.prev_response = response
        self.prev_receive_ts = receive_ts
        self.missed_responses = 0

        # Upgrade to NTPv5 is supported by the server
        if self.auto_version and self.version == 4 and response.reference_ts == Ntp4MagicRefTs.NTP5:
            self.version = 5

class NtpServer:
    def __init__(self, dispersion_rate, local_reference):
        # Map of server cookie -> transmit timestamp
        self.saved_timestamps = {}

        # Queue of cookies to be removed from the map to limit its size
        self.timestamp_queue = collections.deque()
        self.max_timestamps = 1000

        self.dispersion_rate = dispersion_rate
        self.precision = -20
        self.root_delay = 0.0
        self.root_disp = 0.0
        self.reference_ts = read_clock(self.precision)

        self.own_reference_id = 0
        for i in range(10):
            self.own_reference_id |= 1 << random.randint(0, REFERENCE_IDS_OCTETS * 8 - 1)
        self.reference_ids = self.own_reference_id

        if local_reference:
            self.set_reference(1, 0x7f7f0001, 0, 0, 0.0, 0.0)
        else:
            self.set_reference(0, 0, 0, 0, 0.0, 0.0)

    def set_reference(self, stratum, reference_id, reference_ids, reference_ts,
                      root_delay, root_disp):
        if stratum > 0:
            self.leap4 = Ntp4Leap.NORMAL
            self.leap5 = Ntp5Leap.NORMAL
            self.flags = Ntp5Flag.SYNCHRONIZED
        else:
            self.leap4 = Ntp4Leap.UNSYNCHRONIZED
            self.leap5 = Ntp5Leap.UNKNOWN
            self.flags = 0
        self.stratum = stratum
        self.reference_id = reference_id
        self.reference_ids = self.own_reference_id | reference_ids
        self.reference_ts = reference_ts
        self.root_delay = root_delay
        self.root_disp = root_disp

    def make_response(self, request, receive_ts, transmit_ts):
        timescale = era = flags = server_cookie = client_cookie = None
        reference_id = reference_ts = origin_ts = None
        server_info = reference_ids_resp = reference_ts_ = secondary_rx_ts = draft_id = None

        root_disp = self.root_disp
        if self.stratum > 1:
            root_disp += abs(transmit_ts - self.reference_ts) / 2**32 * self.dispersion_rate

        if request.version == 5:
            timescale = Ntp5Timescale.UTC
            flags = self.flags
            era = 0

            if request.flags & Ntp5Flag.INTERLEAVED:
                if request.server_cookie != 0 and request.server_cookie in self.saved_timestamps:
                    flags |= Ntp5Flag.INTERLEAVED
                    transmit_ts = self.saved_timestamps[request.server_cookie]
                server_cookie = receive_ts
            else:
                server_cookie = 0
            client_cookie = request.client_cookie

            if request.server_info is not None:
                server_info = (1 << 4 - 1) | (1 << 5 - 1)
            if request.reference_ids_req is not None:
                reference_ids_resp = self.reference_ids.to_bytes(REFERENCE_IDS_OCTETS, byteorder='big') \
                        [request.reference_ids_req[0]:sum(request.reference_ids_req)]
            if request.reference_ts_ is not None:
                reference_ts_ = self.reference_ts
            if request.secondary_rx_ts is not None:
                secondary_rx_ts = {}
                if Ntp5Timescale.UTC in request.secondary_rx_ts:
                    secondary_rx_ts[Ntp5Timescale.UTC] = (era, receive_ts)
            if request.draft_id is not None:
                draft_id = OUR_DRAFT_ID[:len(request.draft_id)]

        elif request.version == 4:
            if request.receive_ts != request.transmit_ts and \
                    request.origin_ts in self.saved_timestamps:
                # Provide each saved timestamp at most once to avoid broken
                # clients getting stuck in interleaved mode if they don't
                # support it
                transmit_ts = self.saved_timestamps.pop(request.origin_ts)
                origin_ts = request.receive_ts
            else:
                origin_ts = request.transmit_ts

            reference_id = self.reference_id
            if request.reference_ts == Ntp4MagicRefTs.NTP5:
                reference_ts = Ntp4MagicRefTs.NTP5
            else:
                reference_ts = self.reference_ts

        return NtpMessage(request.version, NtpMode.SERVER, self.stratum, request.poll, self.precision,
                          self.root_delay, root_disp, receive_ts, transmit_ts,
                          self.leap5, timescale, era, flags, server_cookie, client_cookie,
                          self.leap4, reference_id, reference_ts, origin_ts,
                          server_info=server_info, reference_ids_resp=reference_ids_resp,
                          reference_ts_=reference_ts_, secondary_rx_ts=secondary_rx_ts,
                          draft_id=draft_id)

    def save_timestamps(self, receive_ts, transmit_ts):
        assert(receive_ts not in self.saved_timestamps)
        assert(len(self.saved_timestamps) <= self.max_timestamps)
        assert(len(self.saved_timestamps) <= len(self.timestamp_queue))

        self.saved_timestamps[receive_ts] = transmit_ts

        self.timestamp_queue.append(receive_ts)
        if len(self.timestamp_queue) > self.max_timestamps:
            self.saved_timestamps.pop(self.timestamp_queue[0], 0)
            self.timestamp_queue.popleft()

    def receive_request(self, sock):
        message, address = sock.recvfrom(1024)
        receive_ts = read_clock(self.precision)

        # Avoid conflict with a previous receive timestamp, e.g. after
        # a backward step of the clock
        while receive_ts in self.saved_timestamps:
            receive_ts += 1

        try:
            request = NtpMessage.decode(message)
        except ValueError as e:
            logging.debug(e)
            return

        request_len = len(message)

        logging.info("Received NTPv{} request ({}) from {}".format(request.version, request_len, address))
        logging.debug("  {}".format(request))

        if request.mode != NtpMode.CLIENT:
            return

        while True:
            pre_transmit_ts = read_clock(self.precision)
            # Make sure the transmit and receive timestamps are different
            if pre_transmit_ts != receive_ts:
                break

        response = self.make_response(request, receive_ts, pre_transmit_ts)

        # This should be a more accurate transmit timestamp of the response
        transmit_ts = read_clock(self.precision)

        message = response.encode(target_len=request_len)

        if len(message) > request_len:
            logging.error("Not sending response longer than request!")
            return
        try:
            sock.sendto(message, address)
        except Exception as e:
            logging.error("Could not sent response to {}: {}".format(address, e))
            logging.debug("  {}".format(response))
            return

        logging.info("Sent NTPv{} response ({}) to {}".format(response.version, len(message), address))
        logging.debug("  {}".format(response))

        self.save_timestamps(receive_ts, transmit_ts)

    def stop(self):
        self.sock.close()

class NtpNode:
    def __init__(self, local_reference, own_port, max_distance, dispersion_rate, no_refid_loop,
                 servers, version, poll, interleaved, refids_fragments):
        self.max_distance = max_distance
        self.poll = poll
        self.no_refid_loop = no_refid_loop

        self.next_poll = time.monotonic()
        self.clients = {}
        self.own_addresses = set()
        self.selection_delays = {}
        self.selected_sources = []

        for server in servers:
            if ':' in server:
                hostname = server.split(':')[0]
                port = int(server.split(':')[1])
            else:
                hostname = server
                port = 123
            for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, port, family=socket.AF_INET):
                sock = socket.socket(family, socket.SOCK_DGRAM)
                sock.connect(sockaddr)
                self.clients[sock] = NtpClient(dispersion_rate, version, interleaved, refids_fragments)
                self.own_addresses.add(sock.getsockname()[0])
                self.selection_delays[sockaddr] = 0
                break
            else:
                raise ValueError("Invalid hostname")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", own_port))

        self.server_sockets = [sock]
        self.server = NtpServer(dispersion_rate, local_reference)

    def select_sources(self):
        logging.info("Selecting sources:")

        selected_sources = []
        for sock, client in self.clients.items():
            address = sock.getpeername()

            if self.selection_delays[address] > 0:
                self.selection_delays[address] -= 1

            if client.sample is None:
                logging.info("  {}: Not selected (missing sample)".format(address))
            elif client.sample.root_delay / 2 + client.sample.root_disp > self.max_distance:
                logging.info("  {}: Not selected (distance too large)".format(address))
            elif client.version == 5 and not client.complete_refids:
                logging.info("  {}: Not selected (waiting for complete refids)".format(address))
            elif self.server.own_reference_id & client.reference_ids == self.server.own_reference_id or \
                 (not self.no_refid_loop and client.reference_id is not None and \
                  str(ipaddress.IPv4Address(client.reference_id)) in self.own_addresses):
                logging.info("  {}: Not selected (synchronization loop)".format(address))
                self.selection_delays[address] = random.randint(1, 4)
            elif self.selection_delays[address] > 0:
                logging.info("  {}: Not selected (recently in loop)".format(address))
            else:
                selected_sources.append((address, client.sample, client.reference_ids))

            client.sample = None

        selected_sources.sort(key=lambda s: s[1].root_delay / 2 + s[1].root_disp + 0.001 * s[1].stratum)
        self.selected_sources = [s[0] for s in selected_sources]

        if len(selected_sources) > 0:
            selected_reference_ids = 0
            for i, (address, sample, reference_ids) in enumerate(selected_sources):
                logging.info("  {}: Selected #{}".format(address, i + 1))
                selected_reference_ids |= reference_ids
            self.server.set_reference(sample.stratum + 1,
                                      int(ipaddress.IPv4Address(selected_sources[0][0][0])),
                                      selected_reference_ids,
                                      read_clock(self.server.precision),
                                      sample.root_delay,
                                      sample.root_disp)

    def get_descriptors(self):
        return list(self.clients.keys()) + self.server_sockets

    def get_timeout(self):
        return max(0.0, self.next_poll - time.monotonic())

    def process_events(self, wait=True):
        timeout = self.get_timeout() if wait else 0.0
        rlist, _, _ = select.select(self.get_descriptors(), [], [], timeout)

        for sock in rlist:
            if sock in self.server_sockets:
                self.server.receive_request(sock)
            elif sock in self.clients:
                self.clients[sock].receive_response(sock)

        if self.get_timeout() <= 0.0:
            self.select_sources()

            for sock, client in self.clients.items():
                client.send_request(sock)

            self.next_poll = time.monotonic() + 2**self.poll


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Experimental NTPv5+NTPv4 client and server.")
    parser.add_argument("servers", nargs="*", help="specify servers")
    parser.add_argument("-p", "--port", dest="port", metavar="PORT", type=int,
                        default=10123, help="specify port of this server (default 10123)")
    parser.add_argument("-v", "--version", dest="version", metavar="VERSION", type=int,
                        default=0, help="specify client NTP version (default negotiation)")
    parser.add_argument("-i", "--poll", dest="poll", type=int,
                        default=2, help="specify polling interval in log2 seconds (default 2)")
    parser.add_argument("-f", "--refids-fragments", dest="refids_fragments", metavar="NUMBER", type=int,
                        default=4, help="specify number of Bloom filter fragments (default 4)")
    parser.add_argument("-r", "--dispersion-rate", dest="dispersion_rate", metavar="RATE", type=float,
                        default=15e-6, help="specify dispersion rate (default 15e-6)")
    parser.add_argument("-m", "--max-distance", dest="max_distance", metavar="DIST", type=float,
                        default=1.0, help="specify maximum acceptable root distance (default 1.0)")
    parser.add_argument("-l", "--local", dest="local_reference", action="store_const", const=True,
                        default=False, help="enable local reference")
    parser.add_argument("-x", "--xleave", dest="interleaved", action="store_const", const=True,
                        default=False, help="send requests in interleaved mode")
    parser.add_argument("-n", "--no-refid", dest="no_refid_loop", action="store_const", const=True,
                        default=False, help="suppress NTPv4 reference ID loop check")
    parser.add_argument("-d", "--debug", dest="debug", action="count",
                        default=0, help="increase debug level")

    args = parser.parse_args()

    logging.basicConfig(format="%(message)s")
    logging.getLogger().setLevel(logging.DEBUG if args.debug > 0  else logging.INFO)

    node = NtpNode(args.local_reference, args.port, args.max_distance, args.dispersion_rate, args.no_refid_loop,
                   args.servers, args.version, args.poll, args.interleaved, args.refids_fragments)

    while True:
        node.process_events()
