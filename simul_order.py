#!/usr/bin/python
# -*- coding: utf-8 -*-


import zmq
import msgpack
import sys
from time import sleep


topic = 'basecamp.muta.orders'
params = {}
"""
params["vr1"] = "3.24V"
params["vr2"] = "15.35C"
params["vr3"] = "3mn"
params["vr4"] = "3870"
params["vr5"] = "12%"
"""
params["UpF"] = "2mn"
messageparams = ['scout#2', params]
print "params:", params
messagedata = msgpack.packb(messageparams)
# Basecamp ZMQ setup
context = zmq.Context()
# Basecamp send channel
socket_send = context.socket(zmq.PUB)
socket_send.connect("tcp://127.0.0.1:5000")
print("ZMQ connect: PUB on tcp://127.0.0.1:5000 (send)")
print 'sending message...'
socket_send.send("%s %s" % (topic, messagedata))
