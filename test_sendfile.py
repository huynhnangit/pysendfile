#!/usr/bin/env python
#
# $Id$
#

import unittest
import os
import sys
import socket
import asyncore
import asynchat
import threading
import errno
import time

import sendfile

PY3 = sys.version_info >= (3,)

def _bytes(x):
    if PY3:
        return bytes(x, 'ascii')
    return x

TESTFN = "$testfile"
DATA = _bytes("12345abcde" * 1024 * 1024)  # 10 Mb
HOST = '127.0.0.1'
LINUX = sys.platform.lower().startswith("linux")


class Handler(asynchat.async_chat):

    def __init__(self, conn):
        asynchat.async_chat.__init__(self, conn)
        self.in_buffer = []
        self.closed = False
        self.push(_bytes("220 ready\r\n"))

    def handle_read(self):
        data = self.recv(4096)
        self.in_buffer.append(data)

    def get_data(self):
        return _bytes('').join(self.in_buffer)

    def handle_close(self):
        self.close()
        self.closed = True

    def handle_error(self):
        raise


class Server(asyncore.dispatcher, threading.Thread):

    handler = Handler

    def __init__(self, address):
        threading.Thread.__init__(self)
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.bind(address)
        self.listen(5)
        self.host, self.port = self.socket.getsockname()[:2]
        self.handler_instance = None
        self._active = False
        self._active_lock = threading.Lock()

    # --- public API

    @property
    def running(self):
        return self._active

    def start(self):
        assert not self.running
        self.__flag = threading.Event()
        threading.Thread.start(self)
        self.__flag.wait()

    def stop(self):
        assert self.running
        self._active = False
        self.join()

    def wait(self):
        # wait for handler connection to be closed, then stop the server
        while not getattr(self.handler_instance, "closed", True):
            time.sleep(0.001)
        self.stop()

    # --- internals

    def run(self):
        self._active = True
        self.__flag.set()
        while self._active and asyncore.socket_map:
            self._active_lock.acquire()
            asyncore.loop(timeout=0.001, count=1)
            self._active_lock.release()
        asyncore.close_all()

    def handle_accept(self):
        conn, addr = self.accept()
        self.handler_instance = self.handler(conn)

    def handle_connect(self):
        self.close()
    handle_read = handle_connect

    def writable(self):
        return 0

    def handle_error(self):
        raise


def sendfile_wrapper(sock, file, offset, nbytes, headers=[], trailers=[]):
    """A higher level wrapper representing how an application is
    supposed to use sendfile().
    """
    while 1:
        try:
            if not LINUX:
                sent, new_offset = sendfile.sendfile(sock, file, offset, nbytes,
                                                     headers, trailers)
            else:
                sent, new_offset = sendfile.sendfile(sock, file, offset, nbytes)
        except OSError as err:
            if err.errno == errno.ECONNRESET:
                # disconnected
                raise
            elif err.errno == errno.EAGAIN:
                # we have to retry send data
                continue
            else:
                raise
        else:
            assert (new_offset - offset) <= sent
            return (sent, new_offset)


class TestSendfile(unittest.TestCase):

    def setUp(self):
        self.server = Server((HOST, 0))
        self.server.start()
        self.client = socket.socket()
        self.client.connect((self.server.host, self.server.port))
        self.client.settimeout(1)
        # synchronize by waiting for "220 ready" response
        self.client.recv(1024)
        self.sockno = self.client.fileno()
        self.file = open(TESTFN, 'rb')
        self.fileno = self.file.fileno()

    def tearDown(self):
        self.file.close()
        self.client.close()
        if self.server.running:
            self.server.stop()

    def test_send_whole_file(self):
        # normal send
        total_sent = 0
        offset = 0
        nbytes = 4096
        while 1:
            sent, offset = sendfile_wrapper(self.sockno, self.fileno, offset, nbytes)
            if sent == 0:
                break
            total_sent += sent
            self.assertTrue(sent <= nbytes)
            self.assertEqual(offset, total_sent)

        self.assertEqual(total_sent, len(DATA))
        self.client.close()
        self.server.wait()
        data = self.server.handler_instance.get_data()
        self.assertEqual(hash(data), hash(DATA))

    def test_send_at_certain_offset(self):
        # start sending a file at a certain offset
        total_sent = 0
        offset = int(len(DATA) / 2)
        nbytes = 4096
        while 1:
            sent, offset = sendfile_wrapper(self.sockno, self.fileno, offset, nbytes)
            if sent == 0:
                break
            total_sent += sent
            self.assertTrue(sent <= nbytes)

        self.client.close()
        self.server.wait()
        data = self.server.handler_instance.get_data()
        expected = DATA[int(len(DATA) / 2):]
        self.assertEqual(total_sent, len(expected))
        self.assertEqual(hash(data), hash(expected))

    def test_offset_overflow(self):
        # specify an offset > file size
        offset = len(DATA) + 4096
        sent, new_offset = sendfile.sendfile(self.sockno, self.fileno, offset, 4096)
        self.assertEqual(sent, 0)
        self.client.close()
        self.server.wait()
        data = self.server.handler_instance.get_data()
        self.assertEqual(data, _bytes(''))

    def test_invalid_offset(self):
        try:
            sendfile.sendfile(self.sockno, self.fileno, -1, 4096)
        except OSError as err:
            self.assertEqual(err.errno, errno.EINVAL)
        else:
            self.fail("exception not raised")

    # --- headers / trailers tests

    if not LINUX:

        def test_headers(self):
            total_sent = 0
            headers = _bytes("x") * 512
            sent, offset = sendfile.sendfile(self.sockno, self.fileno, 0, 4096,
                                             headers=[headers])
            total_sent += sent
            offset = 4096
            nbytes = 4096
            while 1:
                sent, offset = sendfile_wrapper(self.sockno, self.fileno, offset, nbytes)
                if sent == 0:
                    break
                total_sent += sent

            expected_data = headers + DATA
            self.assertEqual(total_sent, len(expected_data))
            self.client.close()
            self.server.wait()
            data = self.server.handler_instance.get_data()
            self.assertEqual(hash(data), hash(expected_data))

        def test_trailers(self):
            total_sent = 0
            trailers = _bytes("x") * 512
            sent, offset = sendfile.sendfile(self.sockno, self.fileno, 0, 4096,
                                             trailers=[trailers])
            total_sent += sent
            offset = 4096
            nbytes = 4096
            while 1:
                sent, offset = sendfile_wrapper(self.sockno, self.fileno, offset, nbytes)
                if sent == 0:
                    break
                total_sent += sent

            expected_data = DATA[:4096] + trailers
            expected_data += DATA[4096:]
            self.assertEqual(total_sent, len(expected_data))
            self.client.close()
            self.server.wait()
            data = self.server.handler_instance.get_data()
            self.assertEqual(hash(data), hash(expected_data))


def test_main():
    tests = [TestSendfile]
    test_suite = unittest.TestSuite()
    for test_class in tests:
        test_suite.addTest(unittest.makeSuite(test_class))
    f = open(TESTFN, "wb")
    f.write(DATA)
    f.close()
    unittest.TextTestRunner(verbosity=2).run(test_suite)
    os.remove(TESTFN)


if __name__ == '__main__':
    test_main()

