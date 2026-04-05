#!/usr/bin/env python2
# -*- coding: utf-8 -*-

"""
Bar.py - Progress Bar UI Module for Pepper Robot

DESCRIPTION:
    Terminal-based progress bar UI system with multiple concurrent progress bars,
    logging capabilities, and thread-safe updates. Provides visual feedback for
    long-running operations on Pepper robot.

FEATURES:
    - Multiple concurrent progress bars with customizable names and lengths
    - Real-time terminal UI with automatic redrawing
    - Thread-safe updates using locks
    - Log message display alongside progress bars
    - Error tracking with dedicated error bars
    - Optional enable/disable via user input
    - Automatic cleanup on exit

HOW TO USE:
    1. Import the module:
       from Bar import ui

    2. The ui object is automatically created and enabled/disabled based on user input

    3. Create a progress bar:
       my_bar = ui.add_bar(total=100, name="Loading", length=40)

    4. Update progress:
       ui.update(my_bar, label="Processing...", inc=1)

    5. Set progress directly:
       ui.set(my_bar, val=50, label="Halfway done")

    6. Log messages:
       ui.log("This message will appear above progress bars")

    7. Display errors:
       ui.Error(text="Connection", error="Timeout occurred")

RUNNING ON PEPPER:
    This module is imported by other test_app scripts. It is NOT run directly.

    When imported, it will prompt:
    "Enable ProgressUI? (Y/N): "

    - Press Y (or Enter) to enable visual progress bars
    - Press N to disable (only prints will be shown)

DEPENDENCIES:
    - Python 2.7 (Pepper's NAOqi environment)
    - Standard library only: sys, threading, time, atexit

NOTES:
    - Uses ANSI escape codes for terminal control
    - Redirects sys.stdout to capture all print statements
    - Main bar (index 0) tracks overall progress across sub-bars
    - Progress can exceed 100% (displayed as >100%)
"""

import sys, threading, time, atexit

class ProgressUI(object):
    def __init__(self):
        try:
            user_input = raw_input  # Py2
        except NameError:
            user_input = input      # Py3
        resp = user_input("Enable ProgressUI? (Y/N): ").strip().lower()
        self.onoff = (resp == '' or resp in ('y', 'yes'))
        self.logs = []
        self.bars = []
        self.lock = threading.Lock()
        atexit.register(self._cleanup)
        self.mainbar = self.add_bar(15, "", 100)  # Add a main bar for the UI


        
    def is_enabled(self):
        return self.onoff

    def add_bar(self, total, name, length=40):
        if self.onoff:
            bar = {
                'total': float(total),
                'count': 0,
                'name':  name,
                'length': int(length),
                'label': ''
            }
            with self.lock:
                self.bars.append(bar)
                self._redraw()
            return len(self.bars)-1

    def log(self, line):
        with self.lock:
            self.logs.append(str(line))
            self._redraw()

    def update(self, idx, label='', inc=1):
        if self.onoff:
            with self.lock:

                b = self.bars[idx]
                b['count'] += inc
                b['label']  = str(label)
                if b['total'] != 100:
                    if b['count'] < b['total']:
                        a = self.bars[self.mainbar]
                        a['count'] += inc
                        a['label']  = str("")
                self._redraw()

    def set(self, idx, val, label=''):
        with self.lock:
            b = self.bars[idx]
            b['count'] = val
            b['label'] = str(label)
            self._redraw()

    def _clear(self):
        sys.__stdout__.write('\033[2J\033[H')  # clear screen & home

    def _redraw(self):
        out = sys.__stdout__
        self._clear()
        # 1) logs
        for ln in self.logs:
            out.write(ln + '\n')
        out.write('\n')
        # 2) bars
        for b in self.bars:
            raw_frac    = (b['count']/b['total']) if b['total'] else 1.0
            fill_frac   = raw_frac if raw_frac <= 1.0 else 1.0
            filled      = int(fill_frac * b['length'])
            empty       = b['length'] - filled
            barstr      = '#' * filled + '.' * empty
            display_pct = raw_frac * 100
            line = "{:<10s} [{}] {:6.2f}% {}".format(
                b['name'], barstr, display_pct, b['label'])
            out.write(line + '\n')
        out.flush()

    def _cleanup(self):
        # nothing special, but could reset terminal here
        pass
    
    def bar_exists(self, name,error=None):
        key = name.strip().lower()
        return any(b['name'].strip().lower() == key for b in ui.bars)

    def get_bar_index(self, name,error=None):
        key = name.strip().lower()
        for idx, b in enumerate(ui.bars):
            if b['name'].strip().lower() == key:
                return idx
        return None
    
    def Error(self,text="",error=None):
        if self.onoff:
            if not self.bar_exists(text):
                Error = self.add_bar(100, text, 5)
            else:
                Error = self.get_bar_index(text)

            self.update(Error, text+" : "+str(error))
            time.sleep(1)

# now hijack sys.stdout so **all** print statements go into ui.log()
class _StdoutCatcher(object):
    def __init__(self, ui):
        self.ui = ui
        self.buf = ''
    def write(self, data):
        self.buf += data
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n',1)
            self.ui.log(line)
    def flush(self):
        pass

# instantiate a single shared UI
ui = ProgressUI()

if not ui.is_enabled():
    print("ProgressUI is disabled. No ui output will be shown.")
    sys.stdout = sys.__stdout__  # Reset stdout to default
else:
    sys.stdout = _StdoutCatcher(ui)
    print("Progress UI initialized. Use ui.add_bar(), ui.update(), etc. to interact with it.")
  # Add a main bar for the UI
