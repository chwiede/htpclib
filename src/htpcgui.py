#!/usr/bin/env python

import re
import select
import socket
import time
import psutil
import logging
import configparser
import sys
import os
from subprocess import Popen, PIPE
from tvhc import tvhclib
from tvhc import HtspClient


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
    pattern_mode = r'^\s+(\d+)x(\d+)\s+([\d.]+)([ *+]{0,2})'

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


def xrandr_preferred():
    for screen in xrandr_query():

        if not screen['primary']:
            continue

        for mode in screen['modes']:
            if mode['preferred']:
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
    last_result['changed'] = False

    if last_result['count'] >= skip:
        last_result['count'] = 0
        new_setting = xrandr_current()

        width_changed = last_result['mode']['width'] != new_setting['width']
        height_changed = last_result['mode']['height'] != new_setting['height']

        last_result['changed'] = width_changed or height_changed
        last_result['mode'] = new_setting

    return last_result


def get_record_pending(client, pending_time=30):
    active_records = list(tvhclib.get_active_records(client))

    if active_records:
        return True

    next_record = tvhclib.get_next_record(client)
    if next_record is not None:
        time_to_next = next_record['start'] - time.time()
        return (time_to_next / 60.0) < pending_time

    return False


def kill_process_recursive(pid):
    parent = psutil.Process(pid)

    for child in parent.children(recursive=True):
        child.kill()

    parent.kill()


def setup_logging():
    logfile = os.path.join(os.path.expanduser('~'), 'htpcgui.log')
    logging.basicConfig(
        filename=logfile,
        filemode='w',
        level=logging.DEBUG,
        format='%(asctime)s.%(msecs)d %(levelname)s %(module)s - %(funcName)s: %(message)s',
        datefmt="%Y-%m-%d %H:%M:%S")


def activate_preferred_screen_mode():
    p_mode = xrandr_preferred()
    if p_mode is not None:
        cmd = 'xrandr -s %sx%s' % (p_mode['width'], p_mode['height'])
        logging.info('activating preferred screen mode: %s' % cmd)
        shell_execute(cmd)
    else:
        logging.info('no preferred mode found')


def gui_stop(gui_stop_command, gui_process):
    if gui_process is not None:
        try:
            logging.info('Stopping GUI via kill-signal')
            kill_process_recursive(gui_process.pid)
            gui_process = None
            logging.info('GUI and child processes stopped.')
        except:
            logging.info('Stopping GUI via command "%s"' % gui_stop_command)
            shell_execute(gui_stop_command)
    elif gui_stop_command:
        logging.info('Stopping GUI via command "%s"' % gui_stop_command)
        shell_execute(gui_stop_command)
    else:
        logging.debug('could not stop GUI process - there is none??')

    return gui_process


def gui_start(gui_load_command):
    logging.info('Start GUI via command "%s"' % gui_load_command)
    gui_process = Popen(gui_load_command, shell=True, stdout=PIPE)
    logging.info('GUI running with PID %s' % gui_process.pid)
    return gui_process


def mainloop(client):

    # load settings
    config = configparser.ConfigParser()
    config.read(['/etc/htpc/htpcgui.conf'])

    logging.info('Loading config')
    try:
        WAKE_PERSISTENT_FILE = config.get('Paths', 'wake_persistent')
        CMD_GUI_LOAD = config.get('Commands', 'gui_load')
        CMD_GUI_STOP = config.get('Commands', 'gui_stop')
        CMD_SHUTDOWN = config.get('Commands', 'shutdown')
        TIME_RECBRIDGE = int(config.get('Times', 'rec_bridge'))
    except Exception as config_error:
        logging.error('Could not load configuration. Please provide /etc/htpc/htpcgui.conf.')
        logging.error('%s' % config_error)
        sys.exit()

    # check if started for record mode
    logging.info('Initialize... ')
    shutdown = False
    gui_process = None
    gui_running = False
    gui_needed = not tvhclib.get_wakedup(WAKE_PERSISTENT_FILE)
    screen_state = None

    initial_mode = "GUI" if gui_needed else "RECORD"
    logging.info('Starting with initial mode: %s' % initial_mode)

    # create acpi socket for power button detection
    acpi_socket = create_acpi_socket()

    # enter main loop
    while 1:
        # power button pressed? change mode, or shutdown.
        if check_for_powerbutton(acpi_socket):
            logging.info('Got PBTN event')

            # toggle gui mode
            gui_needed = not gui_needed
            logging.info('GUI needed is now: %s' % gui_needed)

            # record active or pending?
            record_pending = get_record_pending(client, TIME_RECBRIDGE)
            logging.info('Record active or pending: %s' % record_pending)

            # decide if shutdown is allowed....
            shutdown = not gui_needed and not record_pending

        # poll screen state
        screen_state = poll_screen_change(screen_state)
        if screen_state['changed']:
            logging.info('Screen resolution has changed...')
            gui_stop(CMD_GUI_STOP, gui_process)
            time.sleep(1)
            activate_preferred_screen_mode()
            time.sleep(1)
            screen_state = poll_screen_change(screen_state, 0)
            time.sleep(1)
            gui_stop(CMD_GUI_STOP, gui_process)

        # start gui, if watch mode
        if gui_needed and not gui_running:
            gui_process = gui_start(CMD_GUI_LOAD)
            gui_running = True

        # stop gui, if not needed anymore
        if gui_running and not gui_needed:
            gui_process = gui_stop(CMD_GUI_STOP, gui_process)
            gui_running = False

        # gui exited?
        if gui_process is not None and gui_process.poll() is not None:
            gui_running = False

        # shutdown?
        if shutdown:
            logging.info('Shutdown now via command "%s"' % CMD_SHUTDOWN)
            shell_execute(CMD_SHUTDOWN)
            break

        # just wait a moment...
        time.sleep(2)

if __name__ == '__main__':

    # start logging, say hello
    setup_logging()
    logging.info('*** Starting HTPC UI Controller ***')

    # connect client
    with HtspClient() as client:

        try:
            success = False
            max_tvh_tries = 5
            for i in range(max_tvh_tries):
                logging.info("connect to tvheadend...")
                if client.try_open('localhost', 9982):
                    success = True
                    logging.info("Enter main loop")
                    mainloop(client)
                    break
                else:
                    logging.warning('Could not connect to tvheadend... (%s of %s)' % (i+1, max_tvh_tries))
                    time.sleep(5)

            if not success:
                logging.error('Could not connect to tvheadend after %s tries! Giving up.' % max_tvh_tries)

        except Exception as run_error:
            logging.error('Unexpected error: %s' % run_error)