# MUTA_interface

*MUTA: an open source home automation solution for hobbyists and makers*

This repository regroups the collection of python scripts that handle the pc interface between **Basecamp**, the home automation backend/interface, and the **MUTA** units wireless network.

## Installation

We'll use python v2.x, so make sure it is installed on your machine.

1. if pip is not already installed:
  * `sudo apt-get install python-setuptools python-dev build-essential`
  * `sudo easy_install pip`
2. once pip is available, we can install the various python modules required
  * `sudo pip install pyUSB portalocker zmq msgpack-python`
  
## Configuration

* `config.ini` allows you to modify various parameters used by the main interface script (log files, csv file names, zmq topic filters...)
* `authorized_units.csv` list the units that will be authorized to connect to the MUTA network. Any unit not present in this list will be rejected when trying to connect to the MUTA network.

## Running the scripts

1. run the `./zmq_port_forwarder.sh`
2. run the `./operator.py`
*(to be continued)*

## Interface

### sequence

### data types and variables
