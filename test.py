#!python
# -*- coding: utf-8 -*-


import usb.core
import usb.util
import sys
import array
from time import sleep
import datetime
import threading
import re


def ask_for_status():
    if connected is True:
        try:
            print "asking for status..."
            # Send a one byte long message
            msg = chr(0x01)
            dev.write(0x01, msg)
        except Exception as e:
            print e
            pass
    threading.Timer(5.0, ask_for_status).start()


# Basic script to get and show log/debug text coming from the uC HID device

# note: according to pyUSB doc, one could dev.read data of any arbitrary side
# by providing an array/buffer. But the implementations available under windows
# (libusb and libusbk) don't work when you provide them with an array.
# Libusb raises an error and libusbk fills the array only if it has already
# a given size, and return this size regardless of the number of bytes
# received. to sum it up: I'll have to use fixed size messages.


# threading.Timer(5.0, ask_for_status).start()

while True:

    # find our device
    dev = usb.core.find(idVendor=0x04D8, idProduct=0xF7C9)
    ts0 = datetime.datetime.now()

    # was it found?
    if dev is None:
        print 'Device not found...'
        sleep(3)
    else:
        print "Device found!"
        connected = True
        # set the active configuration. With no arguments, the first
        # configuration will be the active one
        dev.set_configuration()

        # endpoints values:
        # 0x01 for writing and 0x81 for reading

        while connected is True:
            try:
                # receive a 64 byte long (or less) message
                ret = dev.read(0x81, 64, timeout=2000)
                ts1 = datetime.datetime.now()
                # print
                print "+"+str(ts1-ts0),
                ts0 = ts1
                message = ''.join(chr(x) for x in ret)
                print message
                # print ret
                """
                if message[4] == ':':
                    print 'message received!'
                    matchObj = re.match(r'([0-9][0-9])([0-9][0-9]): [0-9]*\-[0-9]* \(([0-9]*)\)', message)
                    short1 = int(matchObj.group(1))
                    short2 = int(matchObj.group(2))
                    counter = int(matchObj.group(3))
                    # Send a 4 bytes long message
                    msg = chr(0x02)+chr(short1)+chr(short2)+chr(counter+1)
                    try:
                        dev.write(0x01, msg)
                    except Exception as e:
                        print e
                """

            except Exception as e:
                # print e
                if "timeout error" in e.__str__():
                    pass
                else:
                    print "deconnected?"
                    connected = False
                    sleep(1)
                    break

        """
        # Send a one byte long message
        msg = chr(0x80)
        dev.write(0x01, msg)

        # Send a one byte long message
        msg = chr(0x81)
        dev.write(0x01, msg)

        # receive a 64 byte long message
        ret = dev.read(0x81, 64)
        print 'ret:', ret
        print type(ret)
        """
