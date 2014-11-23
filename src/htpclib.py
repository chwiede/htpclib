#!/usr/bin/env python

#import shlex
import re
from pprint import pprint
from subprocess import Popen, PIPE


def shell_execute(command):
    """
    Executes a shell command and returns its exit code.
    
    @type command: string
    @param command: The command to execute    
    """
    #command_args = shlex.split(command)
    process = Popen(command, shell=True, stdout=PIPE)
    output, error = process.communicate()
    exitcode = process.wait()
    
    return (output.decode('utf8'), error, exitcode)


def xrandr_query():
    """
    Returns all current available screen resolutions and refresh rate modes as a dictionary.
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
            if match != None:
                modes.append({'width': match.group(1),
                              'height': match.group(2),
                              'rate': match.group(3),
                              'active': '*' in match.group(4),
                              'preferred': '+' in match.group(4)})
        
        item = {'name': screen[0],
                'primary': screen[1] == 'primary',
                'modes': modes}
        
        yield item

