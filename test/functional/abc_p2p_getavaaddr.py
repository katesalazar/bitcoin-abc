#!/usr/bin/env python3
# Copyright (c) 2022 The Bitcoin developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Test getavaaddr p2p message"""
import time
from decimal import Decimal

from test_framework.avatools import AvaP2PInterface, gen_proof
from test_framework.key import ECKey
from test_framework.messages import (
    NODE_AVALANCHE,
    NODE_NETWORK,
    AvalancheVote,
    AvalancheVoteError,
    msg_getavaaddr,
)
from test_framework.p2p import P2PInterface, p2p_lock
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import MAX_NODES, assert_equal, p2p_port
from test_framework.wallet_util import bytes_to_wif

# getavaaddr time interval in seconds, as defined in net_processing.cpp
# A node will ignore repeated getavaaddr during this interval
GETAVAADDR_INTERVAL = 2 * 60

# Address are sent every 30s on average, with a Poisson filter. Use a large
# enough delay so it's very unlikely we don't get the message within this time.
MAX_ADDR_SEND_DELAY = 5 * 60

# The interval between avalanche statistics computation
AVALANCHE_STATISTICS_INTERVAL = 10 * 60

# The getavaaddr messages are sent every 2 to 5 minutes
MAX_GETAVAADDR_DELAY = 5 * 60


class AddrReceiver(P2PInterface):
    def __init__(self):
        super().__init__()
        self.received_addrs = None

    def get_received_addrs(self):
        with p2p_lock:
            return self.received_addrs

    def on_addr(self, message):
        self.received_addrs = []
        for addr in message.addrs:
            self.received_addrs.append(f"{addr.ip}:{addr.port}")

    def addr_received(self):
        return self.received_addrs is not None


class MutedAvaP2PInterface(AvaP2PInterface):
    def __init__(self):
        super().__init__()
        self.is_responding = False
        self.privkey = None
        self.addr = None
        self.poll_received = 0

    def set_addr(self, addr):
        self.addr = addr

    def on_avapoll(self, message):
        self.poll_received += 1


class AllYesAvaP2PInterface(MutedAvaP2PInterface):
    def __init__(self, privkey):
        super().__init__()
        self.privkey = privkey
        self.is_responding = True

    def on_avapoll(self, message):
        self.send_avaresponse(
            message.poll.round, [
                AvalancheVote(
                    AvalancheVoteError.ACCEPTED, inv.hash) for inv in message.poll.invs], self.privkey)
        super().on_avapoll(message)


class AvaAddrTest(BitcoinTestFramework):
    def set_test_params(self):
        self.setup_clean_chain = False
        self.num_nodes = 1
        self.extra_args = [['-enableavalanche=1',
                            '-avaproofstakeutxoconfirmations=1',
                            '-avacooldown=0', '-whitelist=noban@127.0.0.1']]

    def check_all_peers_received_getavaaddr_once(self, avapeers):
        def received_all_getavaaddr(avapeers):
            with p2p_lock:
                return all([p.last_message.get("getavaaddr")
                           for p in avapeers])
        self.wait_until(lambda: received_all_getavaaddr(avapeers))

        with p2p_lock:
            assert all([p.message_count.get(
                "getavaaddr", 0) == 1 for p in avapeers])

    def getavaaddr_interval_test(self):
        node = self.nodes[0]

        # Init mock time
        mock_time = int(time.time())
        node.setmocktime(mock_time)

        master_privkey, proof = gen_proof(node)
        master_pubkey = master_privkey.get_pubkey().get_bytes().hex()
        proof_hex = proof.serialize().hex()

        # Add some avalanche peers to the node
        for _ in range(10):
            node.add_p2p_connection(AllYesAvaP2PInterface(master_privkey))
            assert node.addavalanchenode(
                node.getpeerinfo()[-1]['id'], master_pubkey, proof_hex)

        # Build some statistics to ensure some addresses will be returned
        def all_peers_received_poll():
            with p2p_lock:
                return all([avanode.poll_received >
                           0 for avanode in node.p2ps])
        self.wait_until(all_peers_received_poll)
        node.mockscheduler(AVALANCHE_STATISTICS_INTERVAL)

        requester = node.add_p2p_connection(AddrReceiver())
        requester.send_message(msg_getavaaddr())
        # Remember the time we sent the getavaaddr message
        getavaddr_time = mock_time

        # Spamming more get getavaaddr has no effect
        for _ in range(10):
            with node.assert_debug_log(["Ignoring repeated getavaaddr from peer"]):
                requester.send_message(msg_getavaaddr())

        # Move the time so we get an addr response
        mock_time += MAX_ADDR_SEND_DELAY
        node.setmocktime(mock_time)
        requester.wait_until(requester.addr_received)

        # Elapse the getavaaddr interval and check our message is now accepted
        # again
        mock_time = getavaddr_time + GETAVAADDR_INTERVAL
        node.setmocktime(mock_time)

        requester.send_message(msg_getavaaddr())

        # We can get an addr message again
        mock_time += MAX_ADDR_SEND_DELAY
        node.setmocktime(mock_time)
        requester.wait_until(requester.addr_received)

    def address_test(self, maxaddrtosend, num_proof, num_avanode):
        self.restart_node(
            0,
            extra_args=self.extra_args[0] +
            [f'-maxaddrtosend={maxaddrtosend}'])
        node = self.nodes[0]

        # Init mock time
        mock_time = int(time.time())
        node.setmocktime(mock_time)

        # Create a bunch of proofs and associate each a bunch of nodes.
        avanodes = []
        for _ in range(num_proof):
            master_privkey, proof = gen_proof(node)
            master_pubkey = master_privkey.get_pubkey().get_bytes().hex()
            proof_hex = proof.serialize().hex()

            for n in range(num_avanode):
                avanode = AllYesAvaP2PInterface(
                    master_privkey) if n % 2 else MutedAvaP2PInterface()
                node.add_p2p_connection(avanode)

                peerinfo = node.getpeerinfo()[-1]
                avanode.set_addr(peerinfo["addr"])

                assert node.addavalanchenode(
                    peerinfo['id'], master_pubkey, proof_hex)
                avanodes.append(avanode)

        responding_addresses = [
            avanode.addr for avanode in avanodes if avanode.is_responding]
        assert_equal(len(responding_addresses), num_proof * num_avanode // 2)

        # Check we have what we expect
        avapeers = node.getavalanchepeerinfo()
        assert_equal(len(avapeers), num_proof)
        for avapeer in avapeers:
            assert_equal(len(avapeer['nodes']), num_avanode)

        # Force the availability score to diverge between the responding and the
        # muted nodes.
        def poll_all_for_block():
            node.generate(1)
            with p2p_lock:
                return all([avanode.poll_received > (
                    10 if avanode.is_responding else 0) for avanode in avanodes])
        self.wait_until(poll_all_for_block)

        # Move the scheduler time 10 minutes forward so that so that our peers
        # get an availability score computed.
        node.mockscheduler(AVALANCHE_STATISTICS_INTERVAL)

        requester = node.add_p2p_connection(AddrReceiver())
        requester.send_and_ping(msg_getavaaddr())

        # Sanity check that the availability score is set up as expected
        peerinfo = node.getpeerinfo()
        muted_addresses = [
            avanode.addr for avanode in avanodes if not avanode.is_responding]
        assert all([p['availability_score'] <
                   0 for p in peerinfo if p["addr"] in muted_addresses])
        assert all([p['availability_score'] >
                   0 for p in peerinfo if p["addr"] in responding_addresses])
        # Requester has no availability_score because it's not an avalanche
        # peer
        assert 'availability_score' not in peerinfo[-1].keys()

        mock_time += MAX_ADDR_SEND_DELAY
        node.setmocktime(mock_time)

        requester.wait_until(requester.addr_received)
        addresses = requester.get_received_addrs()
        assert_equal(len(addresses),
                     min(maxaddrtosend, len(responding_addresses)))

        # Check all the addresses belong to responding peer
        assert all([address in responding_addresses for address in addresses])

    def getavaaddr_outbound_test(self):
        self.log.info(
            "Check we send a getavaaddr message to our avalanche outbound peers")
        node = self.nodes[0]

        # Get rid of previously connected nodes
        node.disconnect_p2ps()

        avapeers = []
        for i in range(16):
            avapeer = P2PInterface()
            node.add_outbound_p2p_connection(
                avapeer,
                p2p_idx=i,
                connection_type="avalanche",
                services=NODE_NETWORK | NODE_AVALANCHE,
            )
            avapeers.append(avapeer)

        self.check_all_peers_received_getavaaddr_once(avapeers)

        # Generate some block to poll for
        node.generate(1)

        # Because none of the avalanche peers is responding, our node should
        # fail out of option shortly and send a getavaaddr message to one of its
        # outbound avalanche peers.
        node.mockscheduler(MAX_GETAVAADDR_DELAY)

        def any_peer_received_getavaaddr():
            with p2p_lock:
                return any([p.message_count.get(
                    "getavaaddr", 0) > 1 for p in avapeers])
        self.wait_until(any_peer_received_getavaaddr)

    def getavaaddr_manual_test(self):
        self.log.info(
            "Check we send a getavaaddr message to our manually connected peers that support avalanche")
        node = self.nodes[0]

        # Get rid of previously connected nodes
        node.disconnect_p2ps()

        def added_node_connected(ip_port):
            added_node_info = node.getaddednodeinfo(ip_port)
            return len(
                added_node_info) == 1 and added_node_info[0]['connected']

        def connect_callback(address, port):
            self.log.debug("Connecting to {}:{}".format(address, port))

        p = AvaP2PInterface()
        p2p_idx = 1
        p.peer_accept_connection(
            connect_cb=connect_callback,
            connect_id=p2p_idx,
            net=node.chain,
            timeout_factor=node.timeout_factor,
            services=NODE_NETWORK | NODE_AVALANCHE,
        )()
        ip_port = f"127.0.01:{p2p_port(MAX_NODES - p2p_idx)}"

        node.addnode(node=ip_port, command="add")
        self.wait_until(lambda: added_node_connected(ip_port))

        assert_equal(node.getpeerinfo()[-1]['addr'], ip_port)
        assert_equal(node.getpeerinfo()[-1]['connection_type'], 'manual')

        p.wait_until(lambda: p.last_message.get("getavaaddr"))

        # Generate some block to poll for
        node.generate(1)

        # Because our avalanche peer is not responding, our node should fail
        # out of option shortly and send another getavaaddr message.
        node.mockscheduler(MAX_GETAVAADDR_DELAY)
        p.wait_until(lambda: p.message_count.get("getavaaddr", 0) > 1)

    def getavaaddr_noquorum(self):
        self.log.info(
            "Check we send a getavaaddr message while our quorum is not established")
        node = self.nodes[0]

        self.restart_node(0, extra_args=self.extra_args[0] + [
            '-avaminquorumstake=100000000',
            '-avaminquorumconnectedstakeratio=0.8',
        ])

        privkey, proof = gen_proof(node)

        avapeers = []
        for i in range(16):
            avapeer = AllYesAvaP2PInterface(privkey)
            node.add_outbound_p2p_connection(
                avapeer,
                p2p_idx=i,
                connection_type="avalanche",
                services=NODE_NETWORK | NODE_AVALANCHE,
            )
            avapeers.append(avapeer)

            peerinfo = node.getpeerinfo()[-1]
            avapeer.set_addr(peerinfo["addr"])

        self.check_all_peers_received_getavaaddr_once(avapeers)

        def total_getavaaddr_msg():
            with p2p_lock:
                return sum([p.message_count.get("getavaaddr", 0)
                            for p in avapeers])

        # Because we have not enough stake to start polling, we keep requesting
        # more addresses
        total_getavaaddr = total_getavaaddr_msg()
        for i in range(5):
            node.mockscheduler(MAX_GETAVAADDR_DELAY)
            self.wait_until(lambda: total_getavaaddr_msg() > total_getavaaddr)
            total_getavaaddr = total_getavaaddr_msg()

        # Connect the nodes via an avahello message
        limitedproofid_hex = f"{proof.limited_proofid:0{64}x}"
        for avapeer in avapeers:
            avakey = ECKey()
            avakey.generate()
            delegation = node.delegateavalancheproof(
                limitedproofid_hex,
                bytes_to_wif(privkey.get_bytes()),
                avakey.get_pubkey().get_bytes().hex(),
            )
            avapeer.send_avahello(delegation, avakey)

        # Move the schedulter time forward to make seure we get statistics
        # computed. But since we did not start polling yet it should remain all
        # zero.
        node.mockscheduler(AVALANCHE_STATISTICS_INTERVAL)

        def wait_for_availability_score():
            peerinfo = node.getpeerinfo()
            return all([p.get('availability_score', None) == Decimal(0)
                       for p in peerinfo])
        self.wait_until(wait_for_availability_score)

        requester = node.add_p2p_connection(AddrReceiver())
        requester.send_and_ping(msg_getavaaddr())

        node.setmocktime(int(time.time() + MAX_ADDR_SEND_DELAY))

        # Check all the peers addresses are returned.
        requester.wait_until(requester.addr_received)
        addresses = requester.get_received_addrs()
        assert_equal(len(addresses), len(avapeers))
        expected_addresses = [avapeer.addr for avapeer in avapeers]
        assert all([address in expected_addresses for address in addresses])

    def run_test(self):
        self.getavaaddr_interval_test()

        # Limited by maxaddrtosend
        self.address_test(maxaddrtosend=3, num_proof=2, num_avanode=8)
        # Limited by the number of good nodes
        self.address_test(maxaddrtosend=100, num_proof=2, num_avanode=8)

        self.getavaaddr_outbound_test()
        self.getavaaddr_manual_test()
        self.getavaaddr_noquorum()


if __name__ == '__main__':
    AvaAddrTest().main()
