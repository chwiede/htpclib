#!/usr/bin/env python

import re
import select
import socket
import time
from subprocess import Popen, PIPE


def shell_execute(command):
    """
    Executes a shell command and returns its exit code.
    
    @type command: string
    @param command: The command to execute    
    """
    process = Popen(command, shell=True, stdout=PIPE)
    output, error = process.communicate()
    exitcode = process.wait()

    return output.decode('utf8'), error, exitcode


def xrandr_query():
    """
    Returns all current available screen resolutions and refresh rate modes as a dictionary.
    This method only works with installs X11.
    """
    pattern_screens = r'(\w+)\s+connected\s+(primary|)?.+\n(\s+[x*+.\d\s]+\n)'
    pattern_mode = r'^\s+(\d+)x(\d+)\s+([\d.]+)([*+]?)'

    # xrandr query command
    command = "xrandr -q"
    output, error, exc = shell_execute(command)

    # find screens
    screens = re.findall(pattern_screens, output, re.MULTILINE)

    # iter screens, find resolutions
    for screen in screens:
        modes = []
        for modeline in screen[2].split('\n'):
            match = re.match(pattern_mode, modeline)
            if match:
                modes.append({'width': match.group(1),
                              'height': match.group(2),
                              'rate': match.group(3),
                              'active': '*' in match.group(4),
                              'preferred': '+' in match.group(4)})

        item = {'name': screen[0],
                'primary': screen[1] == 'primary',
                'modes': modes}

        yield item


def xrandr_current():
    for mode in xrandr_query():
        if mode['active']:
            return mode

    return None


def create_acpi_socket(connect=True):
    acpi_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    if connect:
        acpi_socket.connect('/var/run/acpid.socket')

    return acpi_socket


def check_for_powerbutton(acpi_socket, timeout=0.1):
    ready, _, __ = select.select([acpi_socket], [], [], timeout)

    if ready:
        data = acpi_socket.recv(1024)
        for line in data.decode('ascii').split('\n'):
            if 'button/power' in line:
                return True

    return False


if __name__ == '__main__':
    from tvhc import tvhclib

    WAKE_PERSISTENT_FILE = '/var/tmp/tvhc_wakeup'

    # check if started for record mode
    waked_for_record = tvhclib.get_wakedup(WAKE_PERSISTENT_FILE)

    acpi_socket = create_acpi_socket()

    while 1:
        time.sleep(1)
        if check_for_powerbutton(acpi_socket):
            print("powerbtn")

