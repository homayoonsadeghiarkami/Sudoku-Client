from threading import Condition, current_thread
from getpass import getpass
from abc import ABCMeta, abstractmethod
from utils import enum


class OutputClosedException(Exception):
    def __init__(self):
        Exception.__init__(self, "Output PIPE closed!")


class InputClosedException(Exception):
    def __init__(self):
        Exception.__init__(self, "Input PIPE closed!")


class AbstractSyncIO(metaclass=ABCMeta):
    ioclose = enum(IN=0, OUT=1, BOTH=2)

    def __init__(self):
        self.__console_lock = Condition()
        self.__input_lock = False
        self.__output_closed = False
        self.__input_closed = False

    @abstractmethod
    def output(self, msg):
        raise NotImplementedError

    def output_sync(self, msg):
        if self.__output_closed:
            raise OutputClosedException
        with self.__console_lock:
            while self.__input_lock:
                self.__console_lock.wait()
                if self.__output_closed:
                    raise OutputClosedException
            self.output(msg)

    @abstractmethod
    def input(self, prompt='', hidden=False):
        raise NotImplementedError

    def __input_closed_exception_wrap(self, prompt="", hidden=False):
        if self.__input_closed:
            raise InputClosedException
        return self.input(prompt, hidden)

    def input_sync(self, prompt=">> "):
        self.__input_closed_exception_wrap(hidden=True)
        with self.__console_lock:
            self.__input_lock = True
        msg = self.__input_closed_exception_wrap(prompt)
        while len(msg) <= 0:
            msg = self.__input_closed_exception_wrap(prompt)
        with self.__console_lock:
            self.__input_lock = False
            self.__console_lock.notifyAll()
        return msg

    def close(self, pipe=ioclose.BOTH):
        if pipe in [self.ioclose.BOTH, self.ioclose.IN]:
            with self.__console_lock:
                if not self.__input_closed:
                    self.__input_closed = True
        if pipe in [self.ioclose.BOTH, self.ioclose.OUT]:
            with self.__console_lock:
                if not self.__output_closed:
                    self.__output_closed = True


class SyncConsoleAppenderInputReader(AbstractSyncIO):
    def output(self, msg, show_caller=False):
        if show_caller:
            caller = current_thread()
            print("{}: {}".format(caller.name, msg))
        else:
            print(msg)

    def input(self, prompt='', hidden=False):
        if hidden:
            return getpass(prompt)
        return input(prompt)
