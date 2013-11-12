import zmq
import time
import binascii
import os
import struct
import zhelper
import uuid
import zbeacon
from zre_msg import *
from zre_peer import *
from zre_group import *
from uuid import UUID

BEACON_VERSION = 1
ZRE_DISCOVERY_PORT = 5120
REAP_INTERVAL = 1.0  # Once per second

class ZreNode(object):

    def __init__(self, ctx):
        self._ctx = ctx
        self.verbose = False
        self._pipe = zhelper.zthread_fork(self._ctx, ZreNodeAgent)

    # def __del__(self):

    # Receive next message from node
    def recv(self):
        return self._pipe.recv()

    # Set node tracing on or off
    def set_verbose(self, verbose=True):
        self.verbose = verbose

    # Join a group
    def join(self, group):
        self._pipe.send_unicode("JOIN", flags=zmq.SNDMORE)
        self._pipe.send_unicode(group)

    # Leave a group
    def leave(self, group):
        self._pipe.send_unicode("LEAVE", flags=zmq.SNDMORE)
        self._pipe.send_unicode(group)

    # Send message to single peer; peer ID is first frame in message
    def whisper(self, msg):
        self._pipe.send_unicode("WHISPER", flags=zmq.SNDMORE)
        self._pipe.send_unicode(msg)

    # Send message to a group of peers
    def shout(self, msg):
        self._pipe.send_unicode("SHOUT", flags=zmq.SNDMORE)
        self._pipe.send_unicode(msg)

    # Return node handle, for polling
    # TOOO: rename this to socket because that's what it is
    def get_handle(self):
        return self._pipe

    # Set node header value
    def set_header(self, name, format, *args):
        self._pipe.send_unicode("SET", flags=zmq.SNDMORE)
        self._pipe.send_unicode( name, flags=zmq.SNDMORE)
        self._pipe.send_unicode(value, flags=zmq.SNDMORE)

class ZreNodeAgent(object):

    def __init__(self, ctx, pipe):
        self._ctx = ctx
        self._pipe = pipe
        self.inbox = ctx.socket(zmq.ROUTER)
        self.port = self.inbox.bind_to_random_port("tcp://*")
        self.status = 0
        if self.port < 0:
            print("ERROR setting up agent port")
        self.poller = zmq.Poller()
        self.identity = uuid.uuid4()
        print("myID: %s"% self.identity)
        self.beacon = zbeacon.ZBeacon(self._ctx, ZRE_DISCOVERY_PORT)
        # TODO: how do we set the header of the beacon?
        # line 299 zbeacon.c
        self.beacon.set_noecho()
        # construct a header
        transmit = struct.pack('cccb16sIb', b'Z',b'R',b'E', 
                               BEACON_VERSION, self.identity.bytes, 
                               self.port, 1)
        self.beacon.publish(transmit)
        # construct the header filter 
        # (to discard none zre messages)
        filter = struct.pack("ccc", b'Z',b'R',b'E')
        self.beacon.subscribe(filter)

        self.host = self.beacon.get_hostname()
        self.peers = {}
        self.peer_groups = {}
        self.own_groups = {}
        # TODO what is this used for?
        self.headers = {}
        self.run()

    # def __del__(self):
        # destroy beacon
    
    # Send message to all peers
    def peer_send(self, peer, msg):
        peer.send(msg)

    # Here we handle the different control messages from the front-end
    def recv_from_api(self):
        cmds = self._pipe.recv_multipart()
        command = cmds.pop(0).decode('UTF-8')
        if command == "WHISPER":
            # Get peer to send message to
            peer = cmds.pop(0).decode('UTF-8')
            # Send frame on out to peer's mailbox, drop message
            # if peer doesn't exist (may have been destroyed)
            if self.peers[peer]:
                self.peers[peer].send_multipart(cmds, copy=False)
        elif command == "SHOUT":
            # Get group to send message to
            grpname = cmds.pop(0).decode('UTF-8')
            if self.peer_groups[grpname]:
                self.peer_groups[grpname].send_multipart(cmds, copy=False)
        elif command == "JOIN":
            grpname = cmds.pop(0).decode('UTF-8')
            grp = self.own_groups.get(grpname)
            if not grp:
                # Only send if we're not already in group
                grp = ZreGroup(grpname)
                self.own_groups[grpname] = grp
                msg = ZreMsg(ZreMsg.JOIN)
                msg.set_group(grpname)
                self.status += 1
                msg.set_status(self.status)
                for peer in self.peers:
                    peer.send(msg)
                print("Node is joining group %s" % grpname)
        elif command == "LEAVE":
            grpname = cmds.pop(0).decode('UTF-8')
            grp = self.own_groups.get(grpname)
            if grp:
                # Only send if we're actually in group
                msg = ZreMsg(ZreMsg.LEAVE)
                msg.set_group(grpname)
                self.status += 1
                msg.set_status(self.status)
                for peer in self.peers:
                    peer.send(msg)
                self.own_groups.pop(grpname)
                print("Node is leaving group %s" % grpname)
        else:
            print('Unkown Node API command: %s' %command)
            
    def peer_purge(self, peer):
        self.peers.pop(peer)

    # Find or create peer via its UUID string
    def require_peer(self, identity, ipaddr, port):
        #  Purge any previous peer on same endpoint
        # TODO match a uuid to a peer
        p = self.peers.get(identity)
        if not p:
            # TODO: Purge any previous peer on same endpoint
            p = ZrePeer(self._ctx, identity)
            self.peers[identity] = p
            print("Require_peer: %s" %identity)
            p.connect(self.identity, "%s:%u" %(ipaddr, port))
            m = ZreMsg(ZreMsg.HELLO)
            m.set_ipaddress(self.host)
            m.set_mailbox(self.port)
            m.set_groups(self.own_groups.keys())
            m.set_status(self.status)
            p.send(m)
    
            # Now tell the caller about the peer
            self._pipe.send_unicode("ENTER", flags=zmq.SNDMORE);
            self._pipe.send(identity.bytes)
        return p

    # Find or create group via its name
    def require_peer_group(self, groupname):
        grp = self.peer_groups.get(groupname)
        if not grp:
            grp = ZreGroup(groupname)
            self.peer_groups[groupname] = grp 
        return grp

    def join_peer_group(self, peer, name):
        grp = self.require_peer_group(name)
        grp.join(peer)
        # Now tell the caller about the peer joined group
        self._pipe.send_unicode("JOIN", flags=zmq.SNDMORE)
        self._pipe.send(peer.get_identity().bytes, flags=zmq.SNDMORE)
        self._pipe.send_unicode(name)
        return grp

    # Here we handle messages coming from other peers
    def recv_from_peer(self):
        zmsg = ZreMsg()
        zmsg.recv(self.inbox)
        #msgs = self.inbox.recv_multipart()
        # Router socket tells us the identity of this peer
        id = zmsg.get_address()
        # On HELLO we may create the peer if it's unknown
        # On other commands the peer must already exist
        p = self.peers.get(id)
        #print(p, id)
        if zmsg.id == ZreMsg.HELLO:
            p = self.require_peer(id, zmsg.get_ipaddress(), zmsg.get_mailbox())
            p.set_ready(True)
            #print("Hallo %s"%p)

        # Ignore command if peer isn't ready
        if not p or not p.get_ready():
            print("Peer %s isn't ready" %p)
            return
        if not p.check_message(zmsg):
            print("W: [%s] lost messages from %s" %(self.identity, identity))
        if zmsg.id == ZreMsg.HELLO:
            # Join peer to listed groups
            for grp in zmsg.get_groups():
                self.join_peer_group(p, grp)
            # Hello command holds latest status of peer
            p.set_status(zmsg.get_status())
            # Store peer headers for future reference
            p.set_headers(zmsg.get_headers())
        elif zmsg.id == ZreMsg.WHISPER:
            # Pass up to caller API as WHISPER event
            self._pipe.send_unicode("WHISPER", zmq.SNDMORE)
            self._pipe.send_unicode(p.get_identity(), zmq.SNDMORE)
            self._pipe.send(zmsg.content)
        elif zmsg.id == ZreMsg.SHOUT:
            # Pass up to caller API as WHISPER event
            self._pipe.send_unicode("SHOUT", zmq.SNDMORE)
            self._pipe.send_unicode(p.get_identity(), zmq.SNDMORE)
            self._pipe.send_unicode(zmsg.get_group(), zmq.SNDMORE)
            self._pipe.send(zmsg.content)
        elif zmsg.id == ZreMsg.PING:
            p.send(ZreMsg(id=ZreMsg.PING_OK))
        elif zmsg.id == ZreMsg.JOIN:
            self.join_peer_group(p, zmsg.get_group())
            #assert (zre_msg_status (msg) == zre_peer_status (peer))
        elif zmsg.id == ZreMsg.LEAVE:
            self.leave_peer_group(zmsg.get_group())
        p.refresh()
        
        # line 619
    def recv_beacon(self):
        msgs = self.beacon.get_socket().recv_multipart()
        ipaddress = msgs.pop(0)
        frame = msgs.pop(0)
        beacon = struct.unpack('cccb16sIb', frame)
        # Ignore anything that isn't a valid beacon
        if beacon[3] != BEACON_VERSION:
            print("Invalid ZRE Beacon version: %s" %beacon[3])
            return
        peer_id = uuid.UUID(bytes=beacon[4])
        #print("peerId: %s", peer_id)
        port = beacon[5]
        peer = self.require_peer(peer_id, ipaddress.decode('UTF-8'), port)
        peer.refresh()

    #  Remove peer from group, if it's a member
    def peer_delete(self, peer, group):
        group.leave(peer)

    def peer_ping(self, peer):
        p = self.peers.get(peer)
        if time.time() > p.expired_at:
            self._pipe.send_unicode("EXIT", flags=zmq.SNDMORE)
            self._pipe.send(p.get_identity().bytes)
            # If peer has really vanished, expire it (delete)
            self.peer_purge(peer)
            for grp in self.peer_groups.values():
                self.peer_delete(peer, grp)

        elif time.time() > p.evasive_at:
            # If peer is being evasive, force a TCP ping.
            # TODO: do this only once for a peer in this state;
            # it would be nicer to use a proper state machine
            # for peer management.
            msg = ZreMsg(ZreMsg.PING)
            p.send(msg)

    def run(self):
        self.poller.register(self._pipe, zmq.POLLIN)
        self.poller.register(self.inbox, zmq.POLLIN)
        self.poller.register(self.beacon.get_socket(), zmq.POLLIN)

        reap_at = time.time() + REAP_INTERVAL
        while(True):
            timeout = reap_at - time.time();
            if timeout < 0:
                timeout = 0

            items = dict(self.poller.poll(timeout*1000))

            #print(items)
            if self._pipe in items and items[self._pipe] == zmq.POLLIN:
                self.recv_from_api()
                #print("PIPED:")
            if self.inbox in items and items[self.inbox] == zmq.POLLIN:
                self.recv_from_peer()
                #print("NODE?:")
            if self.beacon.get_socket() in items and items[self.beacon.get_socket()] == zmq.POLLIN:
                self.recv_beacon()
            if time.time() >= reap_at:
                reap_at = time.time() + REAP_INTERVAL
                # Ping all peers and reap any expired ones
                for peer_id in self.peers.copy().keys():
                    self.peer_ping(peer_id)

def chat_task(ctx, pipe):
    n = ZreNode(ctx)
    n.join("CHAT")

    poller = zmq.Poller()
    poller.register(pipe, zmq.POLLIN)
    poller.register(n.get_handle(), zmq.POLLIN)
    while(True):
        items = dict(poller.poll())
        if pipe in items and items[pipe] == zmq.POLLIN:
            message = pipe.recv()
            print("CHAT_TASK: %s" % message)
        if n.get_handle() in items and items[n.get_handle()] == zmq.POLLIN:
            cmds = n.get_handle().recv_multipart()
            print("NODE_MSG: ", cmds)



if __name__ == '__main__':
    ctx = zmq.Context()
    chat_pipe = zhelper.zthread_fork(ctx, chat_task)
    while True:
        try:
            msg = input()
            chat_pipe.send_unicode(msg)
        except (KeyboardInterrupt, SystemExit):
            break
    print("FINISHED")