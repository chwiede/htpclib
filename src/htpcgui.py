#!/usr/bin/env python
import configparser
import logging
import os
import re
import select
import socket
import time
import psutil
import sys
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


def kill_process_recursive(pid):
    parent = psutil.Process(pid)
    for child in parent.children(recursive=True):
        child.kill()
    parent.kill()


def setup_logging():
    """
    setups logging
    :return: void
    """
    logfile = os.path.join(os.path.expanduser('~'), 'htpcgui.log')
    logging.basicConfig(
        filename=logfile,
        filemode='a',
        maxBytes=1*1024*1024,
        backupCount=5,
        level=logging.DEBUG,
        format='%(asctime)s.%(msecs)d %(levelname)s  %(funcName)s: %(message)s',
        datefmt="%Y-%m-%d %H:%M:%S")
    logging.info('***')
    logging.info('*** Starting HTPC UI Controller ***')
    logging.info('***')


def xrandr_query():
    """
    Returns all current available screen resolutions and refresh rate modes as a dictionary.
    This method only works with installs X11.
    """
    pattern_screens = r'([\w-]+)\s+connected\s+(primary|)?.+\n(\s+[x*+.\d\s]+\n)'
    pattern_mode = r'^\s+(\d+)x(\d+)\s+([\d.]+)([ *+]{0,2})'

    # xrandr query command
    command = "xrandr --current"
    output, error, exc = shell_execute(command)

    # find screens
    screens = re.findall(pattern_screens, output, re.MULTILINE)

    # iter screens, find resolutions
    for screen in screens:
        for modeline in screen[2].split('\n'):
            match = re.match(pattern_mode, modeline)
            if match:
                yield {'width': match.group(1),
                       'height': match.group(2),
                       'port': screen[0],
                       'rate': match.group(3),
                       'active': '*' in match.group(4),
                       'preferred': '+' in match.group(4)}

def xrandr_current():
    """
    Gets the current xrandr setting
    :return: dictionary with xrandr setting
    """
    modes = list(xrandr_query())

    if modes:
        for mode in modes:
            if mode['active']:
                return mode

    return None


def xrandr_preferred():
    """
    Gets the preferred xrandr ode
    :return: dictionary with xrandr setting
    """
    modes = list(xrandr_query())

    if modes:
        for mode in xrandr_query():
            if mode['preferred']:
                return mode

        return modes[0]

    return None


def create_acpi_socket(connect=True):
    """
    Creates an acpi socket
    :param connect: if True, socket will be connected
    :return: socket
    """
    result = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    if connect:
        result.connect('/var/run/acpid.socket')

    return result


def get_powerbutton_pressed(acpi_socket, timeout=0.1):
    """
    Determines if powerbutton was pressed
    :param acpi_socket: an connected, listening acpi socket
    :param timeout: timeout for socket select
    :return: boolean
    """
    ready, _, __ = select.select([acpi_socket], [], [], timeout)

    if ready:
        data = acpi_socket.recv(1024)
        for line in data.decode('ascii').split('\n'):
            if 'button/power' in line:
                return True

    return False


def get_gui_initial(settings):
    """
    Returns initial GUI-state: True if not waked up for record, otherwise false
    :param settings: a settings dictionary
    :return: boolean
    """
    logging.debug('try to determine wake up reason')
    if not settings['use_tvheadend']:
        logging.debug('use_tvheadend: False => manually started')
        return True

    from tvhc import tvhclib
    logging.debug('reading timestamp file %s' % settings['wake_persistent'])
    waked_by_rtc, timestamp, boot_time = tvhclib.get_wakedup(settings['wake_persistent'], detail=True)
    logging.debug('waked by rtc: %s (timestamp: %s, boot-time: %s)' % (waked_by_rtc, timestamp, boot_time))
    return not waked_by_rtc


def get_open_htsp_client():
    from tvhc import HtspClient
    result = HtspClient()

    for i in range(5):
        if result.try_open('localhost', 9982):
            return result

        time.sleep(2)

    logging.error('Could not connect to tvheadend. Giving up after 5 tries.')
    return result


def get_record_pending(settings):
    """
    Returns True if a record is currently running or pending
    :param settings: a settings dictionary
    :param client: a tvhc-client
    :return: boolean
    """
    if not settings['use_tvheadend']:
        return False

    from tvhc import tvhclib

    with get_open_htsp_client() as client:
        active_records = list(tvhclib.get_active_records(client))
        next_record = tvhclib.get_next_record(client)

        if active_records:
            logging.debug('active records found.')
            return True

        if next_record is not None:
            logging.debug('next record starting at %s, time is %s' % (next_record['start'], time.time()))
            time_to_next = next_record['start'] - time.time()
            return time_to_next < settings['rec_bridge']

    return False


def get_screen_mode(mode):
    if mode is None:
        return {'port': 'None',
                'resolution': '0x0'}

    return {'port': mode['port'],
            'resolution': '%sx%s' % (mode['width'], mode['height'])}


def current_screen_mode():
    mode = xrandr_current()

    if mode is None:
        mode = xrandr_preferred()

    return get_screen_mode(mode)



class HtpcGui(object):
    """
    htcp gui controller class
    """

    def __init__(self):
        """
        initializes a new instance of HtpcGui
        :return: HtpcGui
        """
        logging.debug('HtcpGui initialized')
        self.settings = {}
        self.acpi_socket = create_acpi_socket()
        self.screen_mode = current_screen_mode()
        self.gui_needed = False
        self.gui_process = None
        self.stay_running = False
        self.last_record_check = 0
        self.last_screen_check = 0

    def load_settings(self):
        """
        load settings for instance
        :return: void
        """
        cp = configparser.ConfigParser()
        cp.read(['/etc/htpc/htpcgui.conf'])
        self.settings = {'wake_persistent': cp.get('Paths', 'wake_persistent'),
                         'gui_load': cp.get('Commands', 'gui_load'),
                         'gui_stop': cp.get('Commands', 'gui_stop'),
                         'shutdown': cp.get('Commands', 'shutdown'),
                         'setup_display': cp.get('Commands', 'setup_display'),
                         'rec_bridge': int(cp.get('Times', 'rec_bridge')),
                         'xrandr_wait': int(cp.get('Times', 'xrandr_wait')),
                         'rec_checking': int(cp.get('Times', 'rec_checking')),
                         'check_resolution': cp.get('Options', 'check_resolution') == 'yes',
                         'use_tvheadend': cp.get('Options', 'use_tvheadend') == 'yes'}

    def power_button_pressed(self):
        """
        determines if power button was pressed
        :return: boolean
        """
        if self.acpi_socket is None:
            return False
        else:
            return get_powerbutton_pressed(self.acpi_socket)

    def screen_resolution_changed(self):
        """
        determines if screen resolution has changed
        :return: boolean
        """

        # leave for short time
        if time.time() - self.last_screen_check < 10:
            return False

        self.last_screen_check = time.time()
        new_mode = current_screen_mode()
        if new_mode != self.screen_mode:
            self.screen_mode = new_mode
            return True
        else:
            return False

    def run(self):
        """
        starts main watchdog loop
        :return: void
        """
        logging.debug('Entering main loop.')
        self.gui_needed = get_gui_initial(self.settings)
        self.stay_running = True
        while self.gui_needed or self.stay_running:
            # just a moment...
            time.sleep(1)

            # power button pressed?
            if self.power_button_pressed():
                logging.debug('power button pressed.')
                self.gui_needed = not self.gui_needed

            # screen resolution changed?
            if self.screen_resolution_changed() and self.settings['check_resolution']:
                logging.debug('resolution has changed - try to set preferred one...')
                self.activate_preferred_resolution()
                pass

            # setup gui
            if self.gui_needed and not self.get_gui_running():
                self.start_gui()
                pass

            if self.get_gui_running() and not self.gui_needed:
                self.stop_gui()
                pass

            # check for records pending, if no gui
            if not self.gui_needed and not self.get_gui_running():
                time_diff = time.time() - self.last_record_check
                if time_diff > self.settings['rec_checking']:
                    self.last_record_check = time.time()
                    self.stay_running = get_record_pending(self.settings)
                    logging.debug('check for pending records: %s' % self.stay_running)

        logging.debug('htpc gui main loop finished.')

    def create_setup_display_command(self):
        mode = xrandr_preferred()
        if mode is None:
            logging.warning('Could not get preferred screen mode!')
            return None
        else:
            preferred = get_screen_mode(mode)
            return 'xrandr --output %s -s %s' % (preferred['port'], preferred['resolution'])

    def activate_preferred_resolution(self):
        """
        stops gui, activates the preferred resolutions, and restarts gui
        :return: void
        """

        # create xrandr command
        if self.settings['setup_display']:
            cmd = self.settings['setup_display']
        else:
            cmd = self.create_setup_display_command()

        # stop gui, if running
        has_stopped = False
        if self.get_gui_running():
            has_stopped = True
            self.stop_gui()
            time.sleep(self.settings['xrandr_wait'])

        # setup screen
        logging.debug('setup screen with cmd: %s' % cmd)
        output, error, exitcode = shell_execute(cmd)
        if exitcode != 0:
            logging.warning('setup screen not successfull! Exit code: %s' % exitcode)
            logging.warning(output)
            logging.warning(error)

        # restart gui, if it was stopped.
        if has_stopped:
            time.sleep(self.settings['xrandr_wait'])
            self.start_gui()

        # save new screen mode. no change should be detected after this method.
        self.screen_mode = current_screen_mode()
        logging.debug('resolution is now %s on %s' % (self.screen_mode['resolution'],
                                                      self.screen_mode['port']))

    def start_gui(self):
        """
        starts the gui
        :return: void
        """
        logging.debug('starting GUI...')
        cmd = self.settings['gui_load']
        self.gui_process = Popen(cmd, shell=True, stdout=PIPE)
        if self.get_gui_running():
            logging.debug('GUI started with command "%s"', cmd)
            logging.debug('GUI running now with PID %s' % self.gui_process.pid)
        else:
            logging.exception('Could not start GUI.')

    def stop_gui(self):
        """
        stops the gui
        :return: void
        """
        if self.gui_process is not None:
            try:
                kill_process_recursive(self.gui_process.pid)
                self.gui_process = None
            finally:
                pass

        if self.get_gui_running() and self.settings['gui_stop']:
            shell_execute(self.settings['gui_stop'])
            self.gui_process = None

    def get_gui_running(self):
        """
        returns gui running state
        :return: boolean
        """
        if self.gui_process is None:
            return False
        elif self.gui_process.poll() is not None:
            return False
        else:
            return True

    def shutdown_computer(self):
        """
        shutdown the computer via settings command
        :return: void
        """
        cmd = self.settings['shutdown']
        if cmd:
            logging.debug('shutdown with command "%s"' % cmd)
            shell_execute(cmd)
        else:
            logging.debug('no shutdown command defined.')


if __name__ == '__main__':
    setup_logging()

    htpcgui = HtpcGui()
    try:
        htpcgui.load_settings()
        htpcgui.run()
        htpcgui.shutdown_computer()
    except:
        print("Unexpected error:", sys.exc_info()[0])
        logging.error("Unexpected error:", sys.exc_info()[0])
        raise
