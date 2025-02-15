# -*- coding: utf-8 -*-
"""
Node discovery and network formation are implemented via a kademlia-like protocol.
The major differences are that packets are signed, node ids are the public keys, and
DHT-related features are excluded. The FIND_VALUE and STORE packets are not implemented.
The parameters necessary to implement the protocol are a
bucket size of 16 (denoted k in Kademlia),
concurrency of 3 (denoted alpha in Kademlia),
and 8 bits per hop (denoted b in Kademlia) for routing.
The eviction check interval is 75 milliseconds,
request timeouts are 300ms, and
the idle bucket-refresh interval is 3600 seconds.

Aside from the previously described exclusions, node discovery closely follows system
and protocol described by Maymounkov and Mazieres.
"""

from utils import big_endian_to_int
from crypto import sha3
import operator
import time
import random
import slogging
log = slogging.get_logger('p2p.discovery.kademlia')


k_b = 8  # 8 bits per hop

k_bucket_size = 16
k_request_timeout = 3 * 300 / 1000.      # timeout of message round trips
k_idle_bucket_refresh_interval = 3600    # ping all nodes in bucket if bucket was idle
k_find_concurrency = 3                   # parallel find node lookups
k_pubkey_size = 512
k_id_size = 256
k_max_node_id = 2 ** k_id_size - 1


def random_nodeid():
    return random.randint(0, k_max_node_id)


class Node(object):

    def __init__(self, pubkey):
        assert len(pubkey) == 64 and isinstance(pubkey, str)
        self.pubkey = pubkey
        if k_id_size == 512:
            self.id = big_endian_to_int(pubkey)
        else:
            assert k_id_size == 256
            self.id = big_endian_to_int(sha3(pubkey))

    def distance(self, other):
        return self.id ^ other.id

    def id_distance(self, id):
        return self.id ^ id

    def __eq__(self, other):
        return self.pubkey == other.pubkey

    def __ne__(self, other):
        return not bool(self.pubkey == other.pubkey)

    def __repr__(self):
        return '<Node(%s)>' % self.pubkey[:4].encode('hex')


class KBucket(object):

    """
    Each k-bucket is kept sorted by time last seen—least-recently seen node at the head,
    most-recently seen at the tail. For small values of i, the k-buckets will generally
    be empty (as no appro- priate nodes will exist). For large values of i, the lists can
    grow up to size k, where k is a system-wide replication parameter.
    k is chosen such that any given k nodes are very unlikely to fail within an hour of
    each other (for example k = 20).
    """
    k = k_bucket_size

    def __init__(self, start, end):
        self.start = start
        self.end = end
        self.nodes = []
        self.replacement_cache = []
        self.last_updated = time.time()

    @property
    def range(self):
        return self.start, self.end

    @property
    def midpoint(self):
        return self.start + (self.end - self.start) // 2

    def distance(self, node):
        return self.midpoint ^ node.id

    def id_distance(self, id):
        return self.midpoint ^ id

    def nodes_by_id_distance(self, id):
        assert isinstance(id, (int, long))
        return sorted(self.nodes, key=operator.methodcaller('id_distance', id))

    @property
    def should_split(self):
        depth = self.depth
        return self.is_full and (depth % k_b != 0 and depth != k_id_size)

    def split(self):
        "split at the median id"

        splitid = self.midpoint
        lower = KBucket(self.start, splitid)
        upper = KBucket(splitid + 1, self.end)
        # distribute nodes
        for node in self.nodes:
            bucket = lower if node.id <= splitid else upper
            bucket.add_node(node)
        # distribute replacement nodes
        for node in self.replacement_cache:
            bucket = lower if node.id <= splitid else upper
            bucket.replacement_cache.append(node)

        return lower, upper

    def remove_node(self, node):
        if node not in self.nodes:
            return
        self.nodes.remove(node)

    def in_range(self, node):
        return self.start <= node.id <= self.end

    @property
    def is_full(self):
        return len(self) == k_bucket_size

    def add_node(self, node):
        """
        If the sending node already exists in the recipient’s k- bucket,
        the recipient moves it to the tail of the list.

        If the node is not already in the appropriate k-bucket
        and the bucket has fewer than k entries,
        then the recipient just inserts the new sender at the tail of the list.

        If the  appropriate k-bucket is full, however,
        then the recipient pings the k-bucket’s least-recently seen node to decide what to do.

        on success: return None
        on bucket full: return least recently seen Node for eviction check

        """
        self.last_updated = time.time()
        if node in self.nodes:  # already exists
            self.nodes.remove(node)
            self.nodes.append(node)
        elif len(self) < self.k:  # add if fewer than k entries
            self.nodes.append(node)
        else:  # bucket is full
            return self.head

    @property
    def head(self):
        "least recently seen"
        return self.nodes[0]

    @property
    def tail(self):
        "last recently seen"
        return self.nodes[-1]

    @property
    def depth(self):
        """
        depth is the prefix shared by all nodes in bucket
        i.e. the number of shared leading bits
        """
        def to_binary(x):  # left padded bit representation
            b = bin(x)[2:]
            return '0' * (k_id_size - len(b)) + b

        if len(self.nodes) < 2:
            return k_id_size

        bits = [to_binary(n.id) for n in self.nodes]
        for i in range(k_id_size):
            if len(set(b[:i] for b in bits)) != 1:
                return i - 1
        raise Exception

    def __contains__(self, node):
        return node in self.nodes

    def __len__(self):
        return len(self.nodes)


class RoutingTable(object):

    def __init__(self, node):
        self.this_node = node
        self.buckets = [KBucket(0, k_max_node_id)]

    def split_bucket(self, bucket):
        a, b = bucket.split()
        index = self.buckets.index(bucket)
        self.buckets[index] = a
        self.buckets.insert(index + 1, b)

    @property
    def idle_buckets(self):
        one_hour_ago = time.time() - k_idle_bucket_refresh_interval
        return [b for b in self.buckets if b.last_updated < one_hour_ago]

    @property
    def not_full_buckets(self):
        return [b for b in self.buckets if len(b) < k_bucket_size]

    def remove_node(self, node):
        self.bucket_by_node(node).remove_node(node)

    def add_node(self, node):
        assert node != self.this_node
        # log.debug('add_node', node=node)
        bucket = self.bucket_by_node(node)
        eviction_candidate = bucket.add_node(node)
        if eviction_candidate:  # bucket is full
            # log.debug('bucket is full', node=node, eviction_candidate=eviction_candidate)
            # split if the bucket has the local node in its range
            # or if the depth is not congruent to 0 mod k_b
            depth = bucket.depth
            if bucket.in_range(self.this_node) or (depth % k_b != 0 and depth != k_id_size):
                # log.debug('splitting bucket')
                self.split_bucket(bucket)
                return self.add_node(node)  # retry
            # nothing added, ping eviction_candidate
            return eviction_candidate
        return None  # successfully added to not full bucket

    def bucket_by_node(self, node):
        for bucket in self.buckets:
            if node.id < bucket.end:
                assert node.id >= bucket.start
                return bucket
        raise Exception

    def buckets_by_id_distance(self, id):
        assert isinstance(id, (int, long))
        return sorted(self.buckets, key=operator.methodcaller('id_distance', id))

    def buckets_by_distance(self, node):
        assert isinstance(node, Node)
        return self.buckets_by_id_distance(node.id)

    def __contains__(self, node):
        return node in self.bucket_by_node(node)

    def __len__(self):
        return sum(len(b) for b in self.buckets)

    def __iter__(self):
        for b in self.buckets:
            for n in b.nodes:
                yield n

    def neighbours(self, node, k=k_bucket_size):
        """
        sorting by bucket.midpoint does not work in edge cases
        build a short list of k * 2 nodes and sort and shorten it
        """
        assert isinstance(node, (Node, long, int))
        if isinstance(node, Node):
            node = node.id
        nodes = []
        for bucket in self.buckets_by_id_distance(node):
            for n in bucket.nodes_by_id_distance(node):
                if n is not node:
                    nodes.append(n)
                    if len(nodes) == k * 2:
                        break
        return sorted(nodes, key=operator.methodcaller('id_distance', node))[:k]

    def neighbours_within_distance(self, id, distance):
        """
        naive correct version simply compares all nodes
        """
        assert isinstance(id, long)
        nodes = list(n for n in self if n.id_distance(id) <= distance)
        return sorted(nodes, key=operator.methodcaller('id_distance', id))


class WireInterface(object):

    """
    defines the methods used by KademliaProtocol
    """

    def send_ping(self, node):
        "returns pingid"

    def send_pong(self, node, id):
        pass

    def send_find_node(self, nodeid):
        pass

    def send_neighbours(self, node, neigbours):
        pass


class FindNodeTask(object):

    """
    initiating a find_node and the consulting the buckets via neighbours() does not
    return the find_node result, as these first need to be pinged and might not end up
    in the bucket
    """

    def __init__(self, proto, targetid, via_node=None, timeout=k_request_timeout, callback=None):
        assert isinstance(proto, KademliaProtocol)
        assert isinstance(targetid, long)
        assert not via_node or isinstance(via_node, Node)
        self.proto = proto
        self.targetid = targetid
        self.via_node = via_node
        self.timeout = time.time() + timeout
        self.callback = callback

        if via_node:
            self.wire.send_find_node(via_node, targetid)
        else:
            self._query_neighbours(targetid)
        # FIXME, should we return the closest node (allow callbacks on find_request)


class KademliaProtocol(object):

    def __init__(self, node, wire):
        assert isinstance(node, Node)  # the local node
        assert isinstance(wire, WireInterface)
        self.this_node = node
        self.wire = wire
        self.routing = RoutingTable(node)
        self._expected_pongs = dict()  # pingid -> (timeout, node, replacement_node)
        self._find_requests = dict()  # nodeid -> timeout
        self._deleted_pingids = set()

    def bootstrap(self, nodes):
        assert isinstance(nodes, list)
        for node in nodes:
            if node == self.this_node:
                continue
            self.routing.add_node(node)
            self.find_node(self.this_node.id, via_node=node)

    def update(self, node, pingid=None):
        """
        When a Kademlia node receives any message (request or reply) from another node,
        it updates the appropriate k-bucket for the sender’s node ID.

        If the sending node already exists in the recipient’s k- bucket,
        the recipient moves it to the tail of the list.

        If the node is not already in the appropriate k-bucket
        and the bucket has fewer than k entries,
        then the recipient just inserts the new sender at the tail of the list.

        If the  appropriate k-bucket is full, however,
        then the recipient pings the k-bucket’s least-recently seen node to decide what to do.

        If the least-recently seen node fails to respond,
        it is evicted from the k-bucket and the new sender inserted at the tail.

        Otherwise, if the least-recently seen node responds,
        it is moved to the tail of the list, and the new sender’s contact is discarded.

        k-buckets effectively implement a least-recently seen eviction policy,
        except that live nodes are never removed from the list.
        """
        assert isinstance(node, Node)
        log.debug('in update', remoteid=node, localid=self.this_node)

        # check node is not self

        # if ping was expected
            # if it is not timed out
                # add to bucket
                    # optinally set replacement

        # check for not full buckets
            # ping nodes from replacement cache

        # check for inactive buckets
            # ping nodes

        # prune timed out find_list

        # prune timed out expected_ping list
            # ping replacements

        if node == self.this_node:
            log.debug('node is self', remoteid=node)
            return

        def _expected_pongs():
            return set(v[1] for v in self._expected_pongs.values())

        if pingid and (pingid not in self._expected_pongs):
            assert pingid not in self._expected_pongs
            log.debug('surprising pong', remoteid=node,
                      expected=_expected_pongs(), pingid=pingid.encode('hex')[:8])
            if pingid in self._deleted_pingids:
                log.debug('surprising pong was deleted')
            else:
                for key in self._expected_pongs:
                    if key.endswith(node.pubkey):
                        log.debug('waiting for ping from node, but echo mismatch', node=node,
                                  expected_echo=key[:len(node.pubkey)][:8].encode('hex'),
                                  received_echo=pingid[:len(node.pubkey)][:8].encode('hex'))
            return

        # check for timed out pings and eventually evict them
        for _pingid, (timeout, _node, replacement) in self._expected_pongs.items():
            if time.time() > timeout:
                log.debug('deleting timedout node', remoteid=_node,
                          pingid=_pingid.encode('hex')[:8])
                self._deleted_pingids.add(_pingid)  # FIXME this is for testing
                del self._expected_pongs[_pingid]
                self.routing.remove_node(_node)
                if replacement:
                    log.debug('adding replacement', remoteid=replacement)
                    self.update(replacement)
                    return
                if _node == node:  # prevent node from being added later
                    return

        # if we had registered this node for eviction test
        if pingid in self._expected_pongs:
            timeout, _node, replacement = self._expected_pongs[pingid]
            log.debug('received expected pong', remoteid=node)
            if replacement:
                log.debug('adding replacement to cache', remoteid=replacement)
                self.routing.bucket_by_node(replacement).replacement_cache.append(replacement)
            del self._expected_pongs[pingid]

        # add node
        eviction_candidate = self.routing.add_node(node)
        if eviction_candidate:
            log.debug('could not add', remoteid=node, pinging=eviction_candidate)
            # protocol should ping bucket head and evict if there is no response
            self.ping(eviction_candidate, replacement=node)
        else:
            log.debug('added', remoteid=node)

        # check for not full buckets and ping replacements
        for bucket in self.routing.not_full_buckets:
            for node in bucket.replacement_cache:
                self.ping(node)

        # check idle buckets
        """
        idle bucket refresh:
        for each bucket which hasn't been touched in 3600 seconds
            pick a random value in the range of the bucket and perform discovery for that value
        """
        for bucket in self.routing.idle_buckets:
            rid = random.randint(bucket.start, bucket.end)
            self.find_node(rid)

        # check and removed timedout find requests
        for nodeid, timeout in self._find_requests.items():
            if time.time() > timeout:
                del self._find_requests[nodeid]

        log.debug('updated', num_nodes=len(self.routing), num_buckets=len(self.routing.buckets))

    def _mkpingid(self, echoed, node):
        assert node.pubkey
        pid = echoed + node.pubkey
        log.debug('mkpingid', echoed=echoed.encode('hex'), node=node.pubkey.encode('hex'))
        return pid

    def ping(self, node, replacement=None):
        """
        successful pings should lead to an update
        if bucket is not full
        elif least recently seen, does not respond in time
        """
        assert isinstance(node, Node)
        assert node != self.this_node
        log.debug('pinging', remote=node, local=self.this_node)
        echoed = self.wire.send_ping(node)
        pingid = self._mkpingid(echoed, node)
        assert pingid
        timeout = time.time() + k_request_timeout
        log.debug('set wait for pong from', remote=node, local=self.this_node,
                  pingid=pingid.encode('hex')[:4])
        self._expected_pongs[pingid] = (timeout, node, replacement)

    def recv_ping(self, remote, echo):
        "udp addresses determined by socket address of revd Ping packets"  # ok
        "tcp addresses determined by contents of Ping packet"  # not yet
        assert isinstance(remote, Node)
        log.debug('recv ping', remote=remote, local=self.this_node)
        if remote == self.this_node:
            log.warn('recv ping from self?!')
            return
        self.update(remote)
        self.wire.send_pong(remote, echo)

    def recv_pong(self, remote, echoed):
        "tcp addresses are only updated upon receipt of Pong packet"
        assert remote != self.this_node
        pingid = self._mkpingid(echoed, remote)
        log.debug('recv pong', remote=remote, pingid=pingid.encode('hex')[:8], local=self.this_node)
        # update address (clumsy fixme)
        if hasattr(remote, 'address'):  # not available in tests
            nnodes = self.routing.neighbours(remote)
            if nnodes and nnodes[0] == remote:
                nnodes[0].address = remote.address  # updated tcp address
        # update rest
        self.update(remote, pingid)

    def _query_neighbours(self, targetid):
        for n in self.routing.neighbours(targetid)[:k_find_concurrency]:
            self.wire.send_find_node(n, targetid)

    def find_node(self, targetid, via_node=None):
        assert isinstance(targetid, long)
        assert not via_node or isinstance(via_node, Node)
        self._find_requests[targetid] = time.time() + k_request_timeout
        if via_node:
            self.wire.send_find_node(via_node, targetid)
        else:
            self._query_neighbours(targetid)
        # FIXME, should we return the closest node (allow callbacks on find_request)

    def recv_neighbours(self, remote, neighbours):
        """
        if one of the neighbours is closer than the closest known neighbour
            if not timed out
                query closest node for neighbours
        add all nodes to the list
        """
        assert isinstance(neighbours, list)
        log.debug('recv neighbours', remoteid=remote, num=len(
            neighbours), local=self.this_node, neighbours=neighbours)
        neighbours = [n for n in neighbours if n != self.this_node]
        neighbours = [n for n in neighbours if n not in self.routing]

        # we don't map requests to responses, thus forwarding to all FIXME
        for nodeid, timeout in self._find_requests.items():
            assert isinstance(nodeid, long)
            closest = sorted(neighbours, key=operator.methodcaller('id_distance', nodeid))
            if time.time() < timeout:
                closest_known = self.routing.neighbours(nodeid)
                closest_known = closest_known[0] if closest_known else None
                assert closest_known != self.this_node
                # send find_node requests to k_find_concurrency closests
                for close_node in closest[:k_find_concurrency]:
                    if not closest_known or \
                            close_node.id_distance(nodeid) < closest_known.id_distance(nodeid):
                        log.debug('forwarding find request', closest=close_node,
                                  closest_known=closest_known)
                        self.wire.send_find_node(close_node, nodeid)

        # add all nodes to the list
        for node in neighbours:
            if node != self.this_node:
                self.ping(node)

    def recv_find_node(self, remote, targetid):
        assert isinstance(remote, Node)
        assert isinstance(targetid, long)
        self.update(remote)
        found = self.routing.neighbours(targetid)
        log.debug('recv find_node', remoteid=remote, found=len(found))
        self.wire.send_neighbours(remote, found)
