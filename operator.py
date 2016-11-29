#!/usr/bin/python
# -*- coding: utf-8 -*-


"""
MUTA operator

USB Interface between Basecamp and MUTA network's main operator

Copyright (2015-2016) Nicolas Barthe,
distributed as open source under the terms
of the GNU General Public License (see COPYING.txt)
"""


import usb.core
import usb.util
import sys
from time import sleep
import datetime
import re
import csv
import json
import os
import ConfigParser
import logging
import logging.handlers
import portalocker
import zmq
import msgpack
import random
from influxdb import InfluxDBClient


# --------------------------------------------------------------------------


def byteify(input):
    if isinstance(input, dict):
        return {byteify(key):byteify(value) for key,value in input.iteritems()}
    elif isinstance(input, list):
        return [byteify(element) for element in input]
    elif isinstance(input, unicode):
        return input.encode('utf-8')
    else:
        return input


def load_UID_auth():
    """
    if needed, load/reload the content of the authorized_units file
    """
    global authorized_units, authorized_units_file, authorized_units_ts
    try:
        statbuf = os.stat(authorized_units_file)
        if (authorized_units_ts != datetime.datetime.fromtimestamp(statbuf.st_mtime)):
            log.debug("loading authorized units list from: " + authorized_units_file)
            csvfile = open(authorized_units_file, 'rt')
            csv_reader = csv.reader(csvfile, delimiter='|', quotechar='"')
            # getting headers
            m_headers = next(csv_reader)
            authorized_units = {}
            # getting row_contents
            for row in csv_reader:
                if len(row) > 0:
                    [m_UID, m_alias, m_description, m_default_params] = row
                    m_default_params = byteify(json.loads(m_default_params))
                    authorized_units[m_UID] = [m_alias, m_description, m_default_params]
                    # print '%08x' % int(m_UID, 16)
            csvfile.close()
            authorized_units_ts = datetime.datetime.fromtimestamp(statbuf.st_mtime)
    except OSError:
        log.warning("warning: error checking:", authorized_units_file)
        return None


def check_UID_auth(UID):
    """
    check if a given UID is declared in the authorized_units.csv file
    """
    global authorized_units
    if UID in authorized_units.keys():
        return authorized_units[UID]


def update_network_description_file():
    """
    used when network description changes (any data/value in fact)
    dumps the updated description to the network description csv file
    """
    global network_description_file, network_description, network_description_headers
    try:
        log.debug("updating network description to: " + network_description_file)
        csvfile = open(network_description_file, 'wt')
        portalocker.lock(csvfile, portalocker.LOCK_EX)
        csvfile.write(network_description_headers + '\n')
        csv_writer = csv.writer(csvfile, delimiter='|', quotechar='\'', lineterminator='\n')
        for short_addr in sorted(network_description.keys()):
            m_RO_values_json = json.dumps(network_description[short_addr]['RO_values'], sort_keys=True)
            m_RW_values_json = json.dumps(network_description[short_addr]['RW_values'], sort_keys=True)
            m_pending_updates_json = json.dumps(network_description[short_addr]['pending_updates'], sort_keys=True)
            csv_writer.writerow([short_addr, network_description[short_addr]['UID'], network_description[short_addr]['alias'], network_description[short_addr]['description'],
                                network_description[short_addr]['sleeping'], network_description[short_addr]['last_seen_ts'], m_RO_values_json, m_RW_values_json, m_pending_updates_json])
        csvfile.close()
    except OSError:
        log.error("error while writing to:" + network_description_file + '!')
        return False
    return True


# --------------------------------------------------------------------------

# TinyPack encoding/decoding

NETWORK_REGISTER = 0x02
PING = 0x03
UPDATE = 0x04

UINT8_TYPE = 0
UINT16_TYPE = 1
UFIXED16_TYPE = 2
BOOLEAN_TYPE = 3

NO_UNIT = 0
DEGREES_UNIT = 1
VOLT_UNIT = 2
PERCENT_UNIT = 3
MINUTES_UNIT = 4
SECONDS_UNIT = 5
HOURS_UNIT = 6
DAYS_UNIT = 7

BOOL_FALSE = 0xf0
BOOL_TRUE = 0xff

power_list = ["20mW", "10mW", "5mW", "2.5mW", "1.2mW", "0.6mW", "0.3mW", "0.15mW"]


def merge_dicts(x, y):
    "Given two dicts, merge them into a new dict as a shallow copy."
    z = x.copy()
    z.update(y)
    return z


def encode_value(key, value, writable):
    key = unicode(key, 'utf8')
    value = unicode(value, 'utf8')
    # print "key=", key
    # print "value=", value
    res = []
    # encode the label
    if len(key) != 3:
        log.error("key is not a 3-char-long string!")
        return None
    for i in key:
        res.append(ord(i))
    # print "res_=", res
    # using regexp to guess the unit & type of the value
    while True:
        # boolean / true
        matchObj = re.match("^true$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1)
            m_unit = NO_UNIT
            m_value1 = BOOL_TRUE
            m_type = BOOLEAN_TYPE
            break
        # boolean / false
        matchObj = re.match("^false$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1)
            m_unit = NO_UNIT
            m_value1 = BOOL_FALSE
            m_type = BOOLEAN_TYPE
            break
        # uint8 & uint16/no_unit
        matchObj = re.match("^([0-9]*)$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1)
            m_unit = NO_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 65535):
                log.error("integer value needs more that 2 bytes to be encoded: %i", m_value1)
                return None
            elif (m_value1 > 255):
                m_type = UINT16_TYPE
                m_value2 = m_value1 & 0x00ff
                m_value1 = (m_value1 & 0xff00) >> 8
            else:
                m_type = UINT8_TYPE
            break
        # fixed16/no_unit
        matchObj = re.match("^([0-9]*)\.([0-9]*)$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1), matchObj.group(2)
            m_type = UFIXED16_TYPE
            m_unit = NO_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value1)
                return None
            m_value2 = int(matchObj.group(2))
            if (m_value2 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value2)
                return None
            break
        # uint8/%
        matchObj = re.match("^([0-9]*)\%$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1)
            m_type = UINT8_TYPE
            m_unit = PERCENT_UNIT
            m_value1 = int(matchObj.group(1))
            break
        # fixed16/%
        matchObj = re.match("^([0-9]*)\.([0-9]*)\%$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1), matchObj.group(2)
            m_type = UFIXED16_TYPE
            m_unit = PERCENT_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value1)
                return None
            m_value2 = int(matchObj.group(2))
            if (m_value2 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value2)
                return None
            break
        # uint8 & uint16/V
        matchObj = re.match("^([0-9]*)V$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1)
            m_unit = VOLT_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 65535):
                log.error("integer value needs more that 2 bytes to be encoded: %i", m_value1)
                return None
            elif (m_value1 > 255):
                m_type = UINT16_TYPE
                m_value2 = m_value1 & 0x00ff
                m_value1 = (m_value1 & 0xff00) >> 8
            else:
                m_type = UINT8_TYPE
            break
        # fixed16/V
        matchObj = re.match("^([0-9]*)\.([0-9]*)V$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1), matchObj.group(2)
            m_type = UFIXED16_TYPE
            m_unit = VOLT_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value1)
                return None
            m_value2 = int(matchObj.group(2))
            if (m_value2 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value2)
                return None
            break
        # uint8 & uint16/°C
        matchObj = re.match(u"^([0-9]*)\C$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1)
            m_unit = DEGREES_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 65535):
                log.error("integer value needs more that 2 bytes to be encoded: %i", m_value1)
                return None
            elif (m_value1 > 255):
                m_type = UINT16_TYPE
                m_value2 = m_value1 & 0x00ff
                m_value1 = (m_value1 & 0xff00) >> 8
            else:
                m_type = UINT8_TYPE
            break
        # fixed16/°C
        matchObj = re.match(u"^([0-9]*)\.([0-9]*)\C$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1), matchObj.group(2)
            m_type = UFIXED16_TYPE
            m_unit = DEGREES_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value1)
                return None
            m_value2 = int(matchObj.group(2))
            if (m_value2 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value2)
                return None
            break
        # uint8 & uint16/mn
        matchObj = re.match("^([0-9]*)mn$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1)
            m_unit = MINUTES_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 65535):
                log.error("integer value needs more that 2 bytes to be encoded: %i", m_value1)
                return None
            elif (m_value1 > 255):
                m_type = UINT16_TYPE
                m_value2 = m_value1 & 0x00ff
                m_value1 = (m_value1 & 0xff00) >> 8
            else:
                m_type = UINT8_TYPE
            break
        # fixed16/mn
        matchObj = re.match("^([0-9]*)\.([0-9]*)mn$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1), matchObj.group(2)
            m_type = UFIXED16_TYPE
            m_unit = MINUTES_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value1)
                return None
            m_value2 = int(matchObj.group(2))
            if (m_value2 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value2)
                return None
            break
        # uint8 & uint16/seconds
        matchObj = re.match("^([0-9]*)s$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1)
            m_unit = SECONDS_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 65535):
                log.error("integer value needs more that 2 bytes to be encoded: %i", m_value1)
                return None
            elif (m_value1 > 255):
                m_type = UINT16_TYPE
                m_value2 = m_value1 & 0x00ff
                m_value1 = (m_value1 & 0xff00) >> 8
            else:
                m_type = UINT8_TYPE
            break
        # fixed16/seconds
        matchObj = re.match("^([0-9]*)\.([0-9]*)s$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1), matchObj.group(2)
            m_type = UFIXED16_TYPE
            m_unit = SECONDS_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value1)
                return None
            m_value2 = int(matchObj.group(2))
            if (m_value2 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value2)
                return None
            break
        # uint8 & uint16/hours
        matchObj = re.match("^([0-9]*)h$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1)
            m_unit = HOURS_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 65535):
                log.error("integer value needs more that 2 bytes to be encoded: %i", m_value1)
                return None
            elif (m_value1 > 255):
                m_type = UINT16_TYPE
                m_value2 = m_value1 & 0x00ff
                m_value1 = (m_value1 & 0xff00) >> 8
            else:
                m_type = UINT8_TYPE
            break
        # fixed16/hours
        matchObj = re.match("^([0-9]*)\.([0-9]*)h$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1), matchObj.group(2)
            m_type = UFIXED16_TYPE
            m_unit = HOURS_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value1)
                return None
            m_value2 = int(matchObj.group(2))
            if (m_value2 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value2)
                return None
            break
        # uint8 & uint16/days
        matchObj = re.match("^([0-9]*)d$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1)
            m_unit = DAYS_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 65535):
                log.error("integer value needs more that 2 bytes to be encoded: %i", m_value1)
                return None
            elif (m_value1 > 255):
                m_type = UINT16_TYPE
                m_value2 = m_value1 & 0x00ff
                m_value1 = (m_value1 & 0xff00) >> 8
            else:
                m_type = UINT8_TYPE
            break
        # fixed16/days
        matchObj = re.match("^([0-9]*)\.([0-9]*)d$", value, re.M | re.I)
        if matchObj:
            # print "found:", matchObj.group(1), matchObj.group(2)
            m_type = UFIXED16_TYPE
            m_unit = DAYS_UNIT
            m_value1 = int(matchObj.group(1))
            if (m_value1 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value1)
                return None
            m_value2 = int(matchObj.group(2))
            if (m_value2 > 255):
                log.error("uint8 part value needs more that 1 byte to be encoded: %i", m_value2)
                return None
            break
        # not recognized!
        log.error("error: type/unit not guessed for %s !" % value)
        return None
    # encode type, unit and value(s)
    if writable is True:
        m_writable = 1
    else:
        m_writable = 0
    res.append((m_type << 5) + (m_writable << 4) + m_unit)
    if m_type == UFIXED16_TYPE or m_type == UINT16_TYPE:
        res.append(m_value1)
        res.append(m_value2)
    else:
        res.append(m_value1)
    return res


def format_for_influxdb(m_dict):
    # remove UpF, not interesting for influxdb
    m_dict.pop("UpF", None)
    res = {}
    # print m_dict
    for key in m_dict:
        key = unicode(key, 'utf8')
        value = unicode(m_dict[key], 'utf8')
        # print "key=", key
        # print "value=", value
        # using regexp to guess the unit & type of the value
        while True:
            # boolean / true
            matchObj = re.match("^true$", value, re.M | re.I)
            if matchObj:
                value = 1
                break
            # boolean / false
            matchObj = re.match("^false$", value, re.M | re.I)
            if matchObj:
                value = 0
                break
            # uint8 & uint16/no_unit
            matchObj = re.match("^([0-9]*)$", value, re.M | re.I)
            if matchObj:
                value = int(value)
                break
            # fixed16/no_unit
            matchObj = re.match("^([0-9]*)\.([0-9]*)$", value, re.M | re.I)
            if matchObj:
                value = float(value)
                break
            # uint8/%
            matchObj = re.match("^([0-9]*)\%$", value, re.M | re.I)
            if matchObj:
                value = int(matchObj.group(1))
                break
            # fixed16/%
            matchObj = re.match("^([0-9]*\.[0-9]*)\%$", value, re.M | re.I)
            if matchObj:
                value = float(matchObj.group(1))
                break
            # uint8 & uint16/V
            matchObj = re.match("^([0-9]*)V$", value, re.M | re.I)
            if matchObj:
                value = int(matchObj.group(1))
                break
            # fixed16/V
            matchObj = re.match("^([0-9]*\.[0-9]*)V$", value, re.M | re.I)
            if matchObj:
                value = float(matchObj.group(1))
                break
            # uint8 & uint16/°C
            matchObj = re.match(u"^([0-9]*)\C$", value, re.M | re.I)
            if matchObj:
                value = int(matchObj.group(1))
                break
            # fixed16/°C
            matchObj = re.match(u"^([0-9]*\.[0-9]*)\C$", value, re.M | re.I)
            if matchObj:
                value = float(matchObj.group(1))
                break

            # note that all day/mn/sec durations are converted to float(days)
            # before being written to influxdb
            # so any duration is expressed as a float of days!

            # uint8 & uint16/mn
            matchObj = re.match("^([0-9]*)mn$", value, re.M | re.I)
            if matchObj:
                # print "found:", matchObj.group(1)
                value = float(matchObj.group(1))/(24.0*60)
                value = float("{0:.4f}".format(value))
                break
            # fixed16/mn
            matchObj = re.match("^([0-9]*\.[0-9]*)mn$", value, re.M | re.I)
            if matchObj:
                value = float(matchObj.group(1))/(24.0*60)
                value = float("{0:.4f}".format(value))
                break
            # uint8 & uint16/seconds
            matchObj = re.match("^([0-9]*)s$", value, re.M | re.I)
            if matchObj:
                value = float(matchObj.group(1))/(24.0*3600)
                value = float("{0:.4f}".format(value))
                break
            # fixed16/seconds
            matchObj = re.match("^([0-9]*\.[0-9]*)s$", value, re.M | re.I)
            if matchObj:
                value = float(matchObj.group(1))/(24.0*3600)
                value = float("{0:.4f}".format(value))
                break
            # uint8 & uint16/hours
            matchObj = re.match("^([0-9]*)h$", value, re.M | re.I)
            if matchObj:
                value = float(matchObj.group(1))/24.0
                value = float("{0:.4f}".format(value))
                break
            # fixed16/hours
            matchObj = re.match("^([0-9]*\.[0-9]*)h$", value, re.M | re.I)
            if matchObj:
                value = float(matchObj.group(1))/24.0
                value = float("{0:.4f}".format(value))
                break
            # uint8 & uint16/days
            matchObj = re.match("^([0-9]*)d$", value, re.M | re.I)
            if matchObj:
                value = float(matchObj.group(1))
                value = float("{0:.4f}".format(value))
                print "*** value:", value
                break
            # fixed16/days
            matchObj = re.match("^([0-9]*\.[0-9]*)d$", value, re.M | re.I)
            if matchObj:
                value = float(matchObj.group(1))
                value = float("{0:.4f}".format(value))
                break
            # not recognized!
            log.error("error: format_for_influxdb, type/unit not guessed for %s !" % value)
            return None
        # print "out: key,value = ", key, value
        if key == "Pwr":
            # convert power index to mW value
            power_index = [20.0, 10.0, 5.0, 2.5, 1.2, 0.6, 0.3, 0.15]
            value = power_index[value]
        res[key] = value
    return res


def decode_values(values):
    """
    Decode TinyPack encoded variables/values and return as dicts
    """
    res_RO = {}
    res_RW = {}
    index = 0
    # print values
    while (index < len(values)):
        label = ''.join(chr(x) for x in [values[index + 0], values[index + 1], values[index + 2]])
        # print "label:", label
        variable_type = (values[index + 3] & 0b11100000) >> 5
        # print values[index + 3] & 0b11100000
        # print "variable_type:", variable_type
        writable = (values[index + 3] & 0b00010000) >> 4
        # print "writable:", writable
        units_type = values[index + 3] & 0b00000111
        # print "units_type:", units_type
        # compute the value
        if (variable_type == UINT8_TYPE):
            value = values[index + 4]
            index = index + 5
        elif (variable_type == UINT16_TYPE):
            # print "byte1=%02x byte2=%02x" % (values[index + 4], values[index + 5])
            value = values[index + 4] + values[index + 5] * 256
            index = index + 6
        elif (variable_type == UFIXED16_TYPE):
            value = float(values[index + 5]) + float(values[index + 4]) / 100.0
            index = index + 6
        elif (variable_type == BOOLEAN_TYPE):
            if (values[index + 4] == BOOL_TRUE):
                value = True
                index = index + 5
            else:
                value = False
                index = index + 5
        # print "value:", value
        # add the unit
        if (units_type == NO_UNIT):
            value = str(value)
        elif (units_type == DEGREES_UNIT):
            value = str(value) + 'C'
        elif (units_type == VOLT_UNIT):
            value = str(value) + 'V'
        elif (units_type == PERCENT_UNIT):
            value = str(value) + '%'
        elif (units_type == MINUTES_UNIT):
            value = str(value) + 'mn'
        elif (units_type == SECONDS_UNIT):
            value = str(value) + 's'
        elif (units_type == HOURS_UNIT):
            value = str(value) + 'h'
        elif (units_type == DAYS_UNIT):
            value = str(value) + 'd'
        # print "decoded value+unit:", value
        if writable:
            res_RW[label] = value
        else:
            res_RO[label] = value
    # print "res_RO:", res_RO
    # print "res_RW:", res_RW
    return [res_RO, res_RW]

"""
test = encode_value("Vby", "3280", True)
print test
print decode_values(test)
exit(0)
"""


def decode_message(payload):
    """
    Decode a MUTA message and return the result as a dict
    """
    res = {}
    if payload[0] == NETWORK_REGISTER:
        res['type'] = 'NETWORK_REGISTER'
        res['UID'] = "%02x%02x%02x%02x" % (payload[1], payload[2], payload[3], payload[4])
        res['sleeping'] = (payload[5] == BOOL_TRUE)
    elif payload[0] == PING:
        res['type'] = 'PING'
    elif payload[0] == UPDATE:
        res['type'] = 'UPDATE'
        if payload[1] == BOOL_FALSE:
            res['ack_required'] = False
        elif payload[1] == BOOL_TRUE:
            res['ack_required'] = True
        else:
            log.error('Error decoding message, UPDATE/ack_required field value (%02x) is invalid' % (payload[1]))
            return res
        values = payload[2:]
        (res_RO, res_RW) = decode_values(values)
        res['res_RO'] = res_RO
        res['res_RW'] = res_RW
        return res
    return res

# --------------------------------------------------------------------------
#   main stuff
# --------------------------------------------------------------------------

# read config file
config = ConfigParser.ConfigParser()
config.read("config.ini")
log_file = config.get("log", "file")
vendor_id = int(config.get("usb", "vendor_id"), 16)
product_id = int(config.get("usb", "product_id"), 16)
authorized_units_file = config.get("files", "authorized_units")
network_description_file = config.get("files", "network_description")
pending_updates_file = config.get("files", "pending_updates")
network_description_headers = config.get("headers", "network_description")
zmq_reports_topic = config.get("zmq", "zmq_reports_topic")
zmq_orders_topic = config.get("zmq", "zmq_orders_topic")
influxdb_host = config.get("influxdb", "influxdb_host")
influxdb_port = config.get("influxdb", "influxdb_port")

# create logger
log = logging.getLogger('MUTA_operator')
log.setLevel(logging.INFO)
# create file handler
# fh = logging.FileHandler('/var/tmp/muta.log')
fh = logging.handlers.RotatingFileHandler(
    log_file, maxBytes=8000000, backupCount=5)
fh.setLevel(logging.DEBUG)
# create console handler
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
# create formatter and add it to the handlers
formatter = logging.Formatter('%(asctime)s - %(levelname)s: %(message)s')
fh.setFormatter(formatter)
ch.setFormatter(formatter)
# add the handlers to the logger
log.addHandler(fh)
log.addHandler(ch)

# ZMQ setup
context = zmq.Context()
# muta reports channel
socket_send = context.socket(zmq.PUB)
socket_send.connect("tcp://127.0.0.1:5000")
log.info("ZMQ connect: PUB on tcp://127.0.0.1:5000 sending on %s" % zmq_reports_topic)
# muta_orders channel
socket_receive = context.socket(zmq.SUB)
socket_receive.connect("tcp://127.0.0.1:5001")
socket_receive.setsockopt(zmq.SUBSCRIBE, zmq_orders_topic)
log.info("ZMQ connect: SUB on tcp://127.0.0.1:5001, listening to %s" % zmq_orders_topic)

# influxdb setup
client = InfluxDBClient(influxdb_host, influxdb_port)
client.switch_database('basecamp')
log.info("influxdb will be contacted on "+str(influxdb_host)+":"+str(influxdb_port))
influx_json_body = [
    {
        "measurement": "muta",
        "tags": {
            "unit": "",
        },
        "time": "",
        "fields": {}
    }
]

log.warning("MUTA operator interface is (re)starting!")

authorized_units = {}
authorized_units_ts = datetime.datetime.fromtimestamp(0)  # 1900

while True:
    # find our device
    dev = usb.core.find(idVendor=vendor_id, idProduct=product_id)
    ts0 = datetime.datetime.now()

    # was it found?
    if dev is None:
        log.warning('Device not found...')
        sleep(3)
    else:
        log.info('Device found!')
        # generate the random key associated with this session of the muta network
        # will be used as part of the encryption key by every unit
        random.seed()
        random_key0 = random.getrandbits(8)
        random_key1 = random.getrandbits(8)
        log.info("random bytes for this session: 0x%02X%02X" % (random_key0, random_key1))
        connected = True
        # for linux
        if dev.is_kernel_driver_active(0):
            try:
                    dev.detach_kernel_driver(0)
                    log.info("kernel driver detached")
            except usb.core.USBError as e:
                    sys.exit("Could not detach kernel driver: %s" % str(e))
        else:
            log.info("no kernel driver attached")
        # set the active USB configuration. With no arguments, the first
        # configuration will be the active one
        dev.set_configuration()
        # USB HID endpoints values:
        # 0x01 for writing and 0x81 for reading
        load_UID_auth()  # reload if needed
        network_description = {}  # clear network_description
        update_network_description_file()
        UID_to_short_table = {}  # allows translation from UID to short_adress, let's clear it too
        alias_to_short_table = {}  # allows translation from alias to short_adress, let's clear it too

        # wait for the initial NETWORK_REGISTER from the operator
        while connected is True:
            try:
                # receive a 64 byte long (or less) message
                ret = dev.read(0x81, 64, timeout=2000)  # 2sec timeout & then send a RESET command
                # message = ''.join(chr(x) for x in ret)
                # print message
                if chr(ret[0]) == 'M':
                    # M+short_address(2 bytes)+RSSI(1 byte)+payload
                    # this is a binary message, not text
                    m_ts = str(datetime.datetime.now())[0:-7]
                    m_short_address = [ret[1], ret[2]]
                    m_short_address_str = "%02x%02x" % (ret[1], ret[2])
                    # m_rssi = ret[3]
                    m_payload = ret[3:]
                    res = decode_message(m_payload)
                    log.info("message received from %s: %s" % (m_short_address_str, res))
                    # check content
                    if (res['type'] != 'NETWORK_REGISTER') or (m_short_address_str != "0000"):
                        log.error('Error, expected a NETWORK_REGISTER from operator')
                        log.warning('sending RESET command to operator')
                        # tring to reset the operator
                        dev.write(0x01, 'X')
                    else:
                        load_UID_auth()  # reload if needed
                        result = check_UID_auth(res['UID'])
                        if result is not None:
                            # add to network
                            # short_addr|UID|alias|description|sleeping|last_seen_ts|RO_values|RW_values|pending_updates
                            (m_alias, m_description, m_default_params) = result
                            network_description['0000'] = {}
                            network_description['0000']['UID'] = res['UID']
                            network_description['0000']['alias'] = m_alias
                            network_description['0000']['description'] = m_description
                            network_description['0000']['sleeping'] = res['sleeping']
                            network_description['0000']['last_seen_ts'] = str(m_ts)
                            network_description['0000']['RO_values'] = {}
                            network_description['0000']['RW_values'] = {}
                            network_description['0000']['pending_updates'] = m_default_params.copy()
                            update_network_description_file()
                            UID_to_short_table[res['UID']] = '0000'
                            alias_to_short_table[m_alias] = '0000'
                            answer = BOOL_TRUE
                        else:
                            log.error("operator UID is not in the authorized_units file!")
                            answer = BOOL_FALSE
                            # will cause reset of the operator
                        log.info("answering to NETWORK_REGISTER")
                        # info message, text mode
                        buff = [0x00, 0x00, 4, NETWORK_REGISTER, answer, random_key0, random_key1]
                        message = ''.join(chr(x) for x in buff)
                        msg = 'S' + message
                        try:
                            dev.write(0x01, msg)
                        except Exception as e:
                            log.warning(e)
                            log.warning('answering network register: USB link disconnected?')
                            connected = False
                            sleep(3)
                            break
                        break
                elif chr(ret[0]) == 'I':
                    # info message, text mode
                    message = ''.join(chr(x) for x in ret)
                    log.info(message[1:])
                else:
                    log.info('NETWORK_REGISTER or INFO expected, message ignored')
                    log.warning('sending RESET command to operator')
                    # tring to reset the operator
                    dev.write(0x01, 'X')

            except Exception as e:
                """exc_type, exc_obj, exc_tb = sys.exc_info()
                fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
                log.error(exc_type, fname, exc_tb.tb_lineno)"""
                if ("timeout error" in e.__str__()) or ("timed out" in e.__str__()):
                    # pass
                    log.info("trying to reset the operator!")
                    # operator is there, but no ready signal, send a RESET command!
                    dev.write(0x01, 'X')
                    pass
                else:
                    log.warning(e)
                    log.warning('USB link disconnected?')
                    connected = False
                    sleep(3)
                    break

        # at this point, the operator has sent NETWORK_REGISTER,
        # and has been added to the network description file
        # it should then send (then receive) UPDATE messages
        # before opening the network to others...

        # note: no message buffer on USB HID, so we can't send a message
        # if the other part is also sending one...
        # => send only if there are no incoming message
        # => use a message buffer for sending messages

        # main loop
        # =========

        pending_messages = []
        pending_db_updates = {}
        while connected is True:

            # while there are messages coming from USB, read them and process them!
            timeout_on_reading = False
            while timeout_on_reading is False:
                try:
                    # receive a 64 byte long (or less) message
                    ret = dev.read(0x81, 64, timeout=30)
                    # ts1 = datetime.datetime.now()
                    # print "+" + str(ts1 - ts0),
                    # ts0 = ts1
                    # bytes sent are string
                    # print ret
                    message = ''.join(chr(x) for x in ret)
                    # print message
                    if chr(ret[0]) == 'M':
                        # M+short_address(2 bytes)+RSSI(1 byte)+payload
                        # this is a binary message, not text
                        m_ts = str(datetime.datetime.now())[0:-7]
                        m_short_address = [ret[1], ret[2]]
                        m_short_address_str = "%02x%02x" % (ret[2], ret[1])
                        # m_rssi = ret[3]
                        m_payload = ret[3:]
                        res = decode_message(m_payload)
                        log.info("message received from %s: %s" % (m_short_address_str, res))
                        # check content
                        if (res['type'] == 'UPDATE'):
                            # - debug - à virer après tests
                            # if m_short_address_str != "0000":
                            # network_description[m_short_address_str]['pending_updates']['Pwr'] = "1"
                            # - debug - à virer après tests
                            # TODO: we should check pending_updates to eventually remove changes matching the current variables/values
                            for item in res['res_RW']:
                                if item in network_description[m_short_address_str]['pending_updates'].keys():
                                    if res['res_RW'][item].lower() == network_description[m_short_address_str]['pending_updates'][item].lower():
                                        # remove the item from the pending updates
                                        print "removing from pending updates (un-necessary):", item, res['res_RW'][item]
                                        network_description[m_short_address_str]['pending_updates'].pop(item, None)
                            # we should merge the dicts and update the network description file
                            network_description[m_short_address_str]['last_seen_ts'] = str(m_ts)
                            network_description[m_short_address_str]['RO_values'] = merge_dicts(network_description[m_short_address_str]['RO_values'], res['res_RO'])
                            network_description[m_short_address_str]['RW_values'] = merge_dicts(network_description[m_short_address_str]['RW_values'], res['res_RW'])
                            update_network_description_file()
                            # add the new values to the pending_db_updates dict
                            if m_short_address_str in pending_db_updates:
                                pending_db_updates[m_short_address_str] = merge_dicts(pending_db_updates[m_short_address_str], merge_dicts(res['res_RO'],res['res_RW']))
                            else:
                                pending_db_updates[m_short_address_str] = merge_dicts(res['res_RO'],res['res_RW'])
                            if (res['ack_required'] is True):
                                # format the field updates for influxdb and write them to the influxdb database
                                influx_json_body[0]['time'] = datetime.datetime.utcnow().isoformat()
                                influx_json_body[0]['tags']['unit'] = network_description[m_short_address_str]['alias']
                                influx_json_body[0]['fields'] = format_for_influxdb(pending_db_updates[m_short_address_str])
                                log.info("writing to influxdb: "+str(influx_json_body))
                                try:
                                    client.write_points(influx_json_body)
                                except Exception as e:                   
                                    print e.__str__()
                                    log.error(e)
                                    log.error("Error reaching infludb on "+str(influxdb_host)+":"+str(influxdb_port))
                                # exit(1)
                                pending_db_updates.pop(m_short_address_str, None)
                                # are there any updates left?
                                if len(network_description[m_short_address_str]['pending_updates'].keys()) > 0:
                                    # send them
                                    nb_left = len(network_description[m_short_address_str]['pending_updates'].keys())
                                    nb_values = 0
                                    encoded_values = []
                                    for key in network_description[m_short_address_str]['pending_updates'].keys():
                                        partial_encode = encode_value(key, network_description[m_short_address_str]['pending_updates'][key], True)
                                        encoded_values = encoded_values + partial_encode
                                        print "sending update for key:", key, "encoded_value:", partial_encode
                                        nb_values = nb_values + 1
                                        nb_left = nb_left - 1
                                        if (nb_values == 3):
                                            # sending UDPATE(s) now...
                                            payload_size = len(encoded_values) + 2
                                            if (nb_left == 0):
                                                buff = [m_short_address[0], m_short_address[1], payload_size, UPDATE, BOOL_TRUE] + encoded_values
                                            else:
                                                buff = [m_short_address[0], m_short_address[1], payload_size, UPDATE, BOOL_FALSE] + encoded_values
                                            message = ''.join(chr(x) for x in buff)
                                            msg = 'S' + message
                                            log.info("sending back UPDATE: S+ %s" % str(buff))
                                            pending_messages.append(msg)
                                            encoded_values = []
                                            nb_values = 0
                                    if nb_values > 0:
                                        # sending UDPATE(s) now...
                                        payload_size = len(encoded_values) + 2
                                        buff = [m_short_address[0], m_short_address[1], payload_size, UPDATE, BOOL_TRUE] + encoded_values
                                        message = ''.join(chr(x) for x in buff)
                                        msg = 'S' + message
                                        log.info("sending back UPDATE: S+ %s" % str(buff))
                                        pending_messages.append(msg)
                                else:
                                    # send an empty UPDATE message as an ack
                                    payload_size = 2
                                    buff = [m_short_address[0], m_short_address[1], payload_size, UPDATE, BOOL_TRUE]
                                    message = ''.join(chr(x) for x in buff)
                                    msg = 'S' + message
                                    log.debug("sending empty UPDATE as ack: S+ %s" % str(buff))
                                    pending_messages.append(msg)
                                    # send a report through the basecamp ZMQ PUB/SUB facility
                                    messagedata = msgpack.packb([network_description[m_short_address_str]['alias'], merge_dicts(network_description[m_short_address_str]['RO_values'], network_description[m_short_address_str]['RW_values'])])
                                    socket_send.send("%s %s" % (zmq_reports_topic, messagedata))
                        elif (res['type'] == 'NETWORK_REGISTER') and (m_short_address_str != "0000"):
                            load_UID_auth()  # reload if needed
                            result = check_UID_auth(res['UID'])
                            # print result
                            if result is not None:  # TODO: we should add a test to check if UID is not already used by another unit
                                # add to network
                                # short_addr|UID|alias|description|last_seen_ts|RO_values|RW_values
                                if res['UID'] in UID_to_short_table.keys():  # remove any old entry for this UID
                                    network_description.pop(UID_to_short_table[res['UID']], None)
                                (m_alias, m_description, m_default_params) = result
                                network_description[m_short_address_str] = {}
                                network_description[m_short_address_str]['UID'] = res['UID']
                                network_description[m_short_address_str]['alias'] = m_alias
                                network_description[m_short_address_str]['description'] = m_description
                                network_description[m_short_address_str]['sleeping'] = res['sleeping']
                                network_description[m_short_address_str]['last_seen_ts'] = str(m_ts)
                                network_description[m_short_address_str]['RO_values'] = {}
                                network_description[m_short_address_str]['RW_values'] = {}
                                network_description[m_short_address_str]['pending_updates'] = m_default_params.copy()
                                update_network_description_file()
                                UID_to_short_table[res['UID']] = m_short_address_str
                                alias_to_short_table[m_alias] = m_short_address_str
                                answer = BOOL_TRUE
                                log.info("answering to NETWORK_REGISTER: authorization OK")
                            else:
                                log.error("UID %s is not in the authorized_units file!" % res['UID'])
                                answer = BOOL_FALSE
                                # may/should cause the reset of the unit
                            buff = [m_short_address[0], m_short_address[1], 4, NETWORK_REGISTER, answer, random_key0, random_key1]
                            # print buff
                            message = ''.join(chr(x) for x in buff)
                            msg = 'S' + message
                            pending_messages.append(msg)
                            """
                            try:
                                dev.write(0x01, msg)
                            except Exception as e:
                                print e
                                log.warning(e)
                                log.warning('USB link disconnected?')
                                connected = False
                                sleep(3)
                                break
                            """
                    elif chr(ret[0]) == 'I':
                        # info message, text mode
                        log.info("operator is saying: "+message[2:])
                    else:
                        log.warning('Info or Message_received command expected, message ignored:')
                        log.info(message[2:])

                except Exception as e:                   
                    if ("timeout error" in e.__str__()) or ("timed out" in e.__str__()):
                        timeout_on_reading = True
                        pass                                      
                    else:
                        print e.__str__()
                        log.warning(e)
                        log.warning('USB link disconnected?')
                        connected = False
                        sleep(3)
                        break

            # now check if we have pending messages to be sent on USB...
            if len(pending_messages) > 0:
                try:
                    log.debug("sending NOW buffered message...")
                    dev.write(0x01, pending_messages[0])
                    pending_messages.pop(0)
                except Exception as e:
                    pass
                    # print "could'nt do it... will try again later!"
                # else
                    # print "ok, done!"
                    # could not send pending message...
                    # incoming USB message blocking the way, or USB disconnected...
                    # log.warning(e)
                    # log.warning('could not send pending message! (T_T)')
                    # connected = False
                    # sleep(3)

            # now check if any incoming zmq order is available on basecamp com' facility
            try:
                string = socket_receive.recv(zmq.NOBLOCK)
            except zmq.ZMQError, e:
                if e.errno == zmq.EAGAIN:
                    pass  # no message was ready
                else:
                    raise  # real error
            else:
                # process message
                topic, messagedata = string.split()
                log.debug("ZQM received: %s %s" % (topic, messagedata))
                if topic == 'basecamp.muta.orders':
                    (alias, values) = msgpack.unpackb(messagedata, use_list=True)
                    print "alias:", alias
                    print "values:", values
                    if alias not in alias_to_short_table.keys():
                        # this could happen if the network is restarted (unit yet to be seen)
                        log.warning('basecamp.muta.orders received with unknown alias: %s - ignored' % alias)
                    else:
                        m_short = alias_to_short_table[m_alias]
                        print "m_short:", m_short
                        m_short_zero = int(m_short[0:2], 10)
                        m_short_one = int(m_short[2:4], 10)
                        # add the changes to the pending list, if the variables are present in the RW dict
                        # we could also ignore this test to force the update
                        # - disabled for debug -
                        for name in values.keys():
                            """
                            if name not in network_description[m_short]['RW_values']:
                                log.warning('basecamp.muta.orders variable name not in RW list: %s - ignored' % name)
                            else:
                                network_description[m_short]['pending_updates'][name] = values[name]
                            """
                            # print "type of value:", type(values[name])
                            network_description[m_short]['pending_updates'][name] = values[name]
                        update_network_description_file()
                        # now we should send the UPDATE request(s) if the device is always-on
                        # else, it will be written to 'pending updates' and sent when the device wakes up and updates
                        if network_description[m_short]['sleeping'] is False:
                            nb_left = len(values.keys())
                            nb_values = 0
                            encoded_values = []
                            for key in values.keys():
                                partial_encode = encode_value(key, values[key], True)
                                encoded_values = encoded_values + partial_encode
                                print "key:", key, "encoded_value:", partial_encode
                                nb_values = nb_values + 1
                                nb_left = nb_left - 1
                                if (nb_values == 3):
                                    # sending UDPATE(s) now...
                                    payload_size = len(encoded_values) + 2
                                    if (nb_left == 0):
                                        buff = [m_short_zero, m_short_one, payload_size, UPDATE, BOOL_TRUE] + encoded_values
                                    else:
                                        buff = [m_short_zero, m_short_one, payload_size, UPDATE, BOOL_FALSE] + encoded_values
                                    message = ''.join(chr(x) for x in buff)
                                    msg = 'S' + message
                                    print "sending: S+", buff
                                    pending_messages.append(msg)
                                    encoded_values = []
                                    nb_values = 0
                            if nb_values > 0:
                                # sending UDPATE(s) now...
                                payload_size = len(encoded_values) + 2
                                buff = [m_short_zero, m_short_one, payload_size, UPDATE, BOOL_TRUE] + encoded_values
                                message = ''.join(chr(x) for x in buff)
                                msg = 'S' + message
                                print "sending: S+", buff
                                pending_messages.append(msg)
                        else:
                            # network device is a sleeping unit, decode and write/merge with pending updates
                            # they will be sent when the device wakes up & updates...
                            print "TODO"
