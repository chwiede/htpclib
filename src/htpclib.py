#!/usr/bin/env python

import re
import select
import socket
import time
import psutil
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
    for screen in xrandr_query():

        if not screen['primary']:
            continue

        for mode in screen['modes']:
            if mode['active']:
                return mode

    return None


def create_acpi_socket(connect=True):
    result = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    if connect:
        result.connect('/var/run/acpid.socket')

    return result


def check_for_powerbutton(acpi_socket, timeout=0.1):
    ready, _, __ = select.select([acpi_socket], [], [], timeout)

    if ready:
        data = acpi_socket.recv(1024)
        for line in data.decode('ascii').split('\n'):
            if 'button/power' in line:
                return True

    return False


def poll_screen_change(last_result, skip=5):
    if last_result is None:
        return {'changed': False,
                'count': 0,
                'mode': xrandr_current()}

    last_result['count'] += 1

    if last_result['count'] >= skip:
        last_result['count'] = 0
        new_setting = xrandr_current()
        last_result['changed'] = last_result['mode'] != new_setting
        last_result['mode'] = new_setting

    return last_result


def get_record_pending(client):
    active_records = list(tvhclib.get_active_records(client))

    if active_records:
        return True

    next_record = tvhclib.get_next_record(client)
    if next_record is not None:
        time_to_next = time.time() - next_record['start']
        return (time_to_next / 60.0) < 45

    return False


def kill_process_recursive(pid):
    parent = psutil.Process(pid)

    for child in parent.children(recursive=True):
        child.kill()

    parent.kill()


if __name__ == '__main__':
    from tvhc import tvhclib
    from tvhc import HtspClient

    WAKE_PERSISTENT_FILE = '/var/tmp/tvhc_wakeup'
    CMD_GUI_LOAD = "xbmc"
    CMD_GUI_STOP = "kill xbmc.bin"

    # check if started for record mode
    shutdown = False
    gui_process = None
    gui_running = False
    gui_needed = not tvhclib.get_wakedup(WAKE_PERSISTENT_FILE)
    screen_state = None

    # create acpi_socket for power button detection
    acpi_socket = create_acpi_socket()

    # connect client
    with HtspClient() as client:
        if not client.try_open('localhost', 9982):
            tvhclib.open_fail(True)

        # enter main loop
        while 1:
            # power button pressed? change mode, or shutdown.
            if check_for_powerbutton(acpi_socket):
                # toggle gui mode
                gui_needed = not gui_needed

                # record active or pending?
                record_pending = get_record_pending(client)

                # decide if shutdown is allowed....
                shutdown = not gui_needed and not record_pending

            # start gui, if watch mode
            if gui_needed and not gui_running:
                gui_process = Popen(CMD_GUI_LOAD, shell=True, stdout=PIPE)
                gui_running = True

            # stop gui, if not needed anymore
            if gui_running and not gui_needed:
                if gui_process is not None:
                    kill_process_recursive(gui_process.pid)

                gui_running = False

            # gui exited?
            if gui_process is not None and gui_process.poll() is not None:
                gui_running = False

            # poll screen state
            screen_state = poll_screen_change(screen_state)
            if screen_state['changed']:
                pass

            # shutdown?
            if shutdown:
                print("SHUTDOWN!")
                break

            # just wait a second...
            time.sleep(1)