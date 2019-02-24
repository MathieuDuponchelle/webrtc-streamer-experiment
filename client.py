#!/bin/env python3

import random
import ssl
import websockets
import asyncio
import os
import sys
import json
import argparse
import uuid

import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
gi.require_version('GstWebRTC', '1.0')
from gi.repository import GstWebRTC
gi.require_version('GstSdp', '1.0')
from gi.repository import GstSdp

PIPELINE_DESC = '''
webrtcbin name=sendrecv bundle-policy=max-bundle
 videotestsrc is-live=true pattern=ball ! videoconvert ! queue ! vp8enc deadline=1 ! rtpvp8pay !
 queue ! application/x-rtp,media=video,encoding-name=VP8,payload=97 ! sendrecv.
 audiotestsrc is-live=true wave=red-noise ! audioconvert ! audioresample ! queue ! opusenc ! rtpopuspay !
 queue ! application/x-rtp,media=audio,encoding-name=OPUS,payload=96 ! sendrecv.
'''

class WebRTCClient:
    def __init__(self, id_, peer_id, server, disable_ssl):
        self.id_ = id_
        self.media_session_id = uuid.uuid4()
        self.conn = None
        self.pipe = None
        self.webrtc = None
        self.peer_id = peer_id
        self.disable_ssl = disable_ssl
        self.state = 'init'
        self.server = server or 'wss://192.168.1.1:8443'

    async def send_message(self, messageType, payload):
        msg = json.dumps(
            {
                'message': messageType,
                'data': payload
            })
        print("-> %s" % msg)
        await self.conn.send(msg)

    async def connect(self):
        sslctx = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        if self.disable_ssl:
            sslctx.verify_mode = ssl.CERT_NONE
        self.conn = await websockets.connect(self.server, ssl=sslctx)
        print("connected")
        self.state = 'connected'
        await self.send_message('connection',
                                [
                                    {
                                        'appKey':'defaultApp',
                                        'mediaProviders':['WebRTC'],
                                        'clientVersion':'0.0.1'
                                    }
                                ])

    def on_offer_created(self, promise, _, __):
        promise.wait()
        reply = promise.get_reply()
        offer = reply.get_value('offer')
        promise = Gst.Promise.new()
        self.webrtc.emit('set-local-description', offer, promise)
        promise.interrupt()
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.send_message('publishStream',
                                [
                                    {
                                        'mediaSessionId': str(self.media_session_id),
                                        'name': str(self.peer_id),
                                        'published':True,
                                        'hasVideo':True,
                                        'hasAudio': True,
                                        'record': False,
                                        'status': 'PENDING',
                                        'sdp': offer.sdp.as_text(),
                                        'bitrate':0,
                                        'minBitrate':0,
                                        'maxBitrate':0,
                                    }
                                ]))

    def on_negotiation_needed(self, element):
        promise = Gst.Promise.new_with_change_func(self.on_offer_created, element, None)
        element.emit('create-offer', None, promise)

    def on_incoming_decodebin_stream(self, _, pad):
        if not pad.has_current_caps():
            print (pad, 'has no caps, ignoring')
            return

        caps = pad.get_current_caps()
        assert (len(caps))
        s = caps[0]
        name = s.get_name()
        if name.startswith('video'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('videoconvert')
            sink = Gst.ElementFactory.make('autovideosink')
            self.pipe.add(q, conv, sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(sink)
        elif name.startswith('audio'):
            q = Gst.ElementFactory.make('queue')
            conv = Gst.ElementFactory.make('audioconvert')
            resample = Gst.ElementFactory.make('audioresample')
            sink = Gst.ElementFactory.make('autoaudiosink')
            self.pipe.add(q, conv, resample, sink)
            self.pipe.sync_children_states()
            pad.link(q.get_static_pad('sink'))
            q.link(conv)
            conv.link(resample)
            resample.link(sink)

    def on_incoming_stream(self, _, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return

        decodebin = Gst.ElementFactory.make('decodebin')
        decodebin.connect('pad-added', self.on_incoming_decodebin_stream)
        self.pipe.add(decodebin)
        decodebin.sync_state_with_parent()
        self.webrtc.link(decodebin)

    def start_pipeline(self):
        print("start pipeline")
        self.pipe = Gst.parse_launch(PIPELINE_DESC)
        self.webrtc = self.pipe.get_by_name('sendrecv')
        self.webrtc.connect('on-negotiation-needed', self.on_negotiation_needed)
        self.webrtc.connect('pad-added', self.on_incoming_stream)
        self.pipe.set_state(Gst.State.PLAYING)

    async def handle_sdp(self, message):
        if self.state != 'publish':
            return
        assert (self.webrtc)
        msg = json.loads(message)
        if msg['message'] == 'setRemoteSDP':
            sdp = msg['data'][1]
            print ('Received answer:\n%s' % sdp)
            res, sdpmsg = GstSdp.SDPMessage.new()
            GstSdp.sdp_message_parse_buffer(bytes(sdp.encode()), sdpmsg)
            answer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.ANSWER, sdpmsg)
            promise = Gst.Promise.new()
            self.webrtc.emit('set-remote-description', answer, promise)
            promise.interrupt()

    async def pong(self):
        await self.send_message('pong', [None])

    def publish(self):
        if self.state == 'publish':
            return
        self.state = 'publish'
        self.start_pipeline()

    async def loop(self):
        assert self.conn
        async for message in self.conn:
            print('<- %s' % message)
            jsonmsg = json.loads(message)
            if jsonmsg['message'] == 'getUserData':
                pass
            elif jsonmsg['message'] == 'getVersion':
                pass
            elif jsonmsg['message'] == 'ping':
                await self.pong()
                self.publish()
            elif jsonmsg['message'] == 'setRemoteSDP':
                await self.handle_sdp(message)
        return 0


def check_plugins():
    needed = ["opus", "vpx", "nice", "webrtc", "dtls", "srtp", "rtp",
              "rtpmanager", "videotestsrc", "audiotestsrc"]
    missing = list(filter(lambda p: Gst.Registry.get().find_plugin(p) is None, needed))
    if len(missing):
        print('Missing gstreamer plugins:', missing)
        return False
    return True


if __name__=='__main__':
    Gst.init(None)
    if not check_plugins():
        sys.exit(1)
    parser = argparse.ArgumentParser()
    parser.add_argument('peerid', help='String ID of the peer to connect to')
    parser.add_argument('--server', help='Signalling server to connect to, eg "wss://127.0.0.1:8443"')
    parser.add_argument('--disable-ssl', action='store_true', help='Disable SSL validation')
    args = parser.parse_args()
    our_id = random.randrange(10, 10000)
    c = WebRTCClient(our_id, args.peerid, args.server, args.disable_ssl)
    asyncio.get_event_loop().run_until_complete(c.connect())
    res = asyncio.get_event_loop().run_until_complete(c.loop())
    sys.exit(res)
