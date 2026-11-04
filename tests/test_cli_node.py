import json
import os
import pty
import struct
import termios
import fcntl
import unittest
from unittest.mock import Mock, patch

from cli_node import CLINode


class CLITerminalSizeTests(unittest.TestCase):
    def test_real_terminal_stdin_and_piped_stdout_like_launcher(self):
        master, slave = pty.openpty()
        read_fd, write_fd = os.pipe()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 54, 211, 0, 0))
            with patch('cli_node.sys.stdin') as stdin, patch('cli_node.sys.stdout') as stdout:
                stdin.fileno.return_value = slave
                stdout.fileno.return_value = write_fd
                node = CLINode()
                self.assertEqual(node._get_terminal_size(), (54, 211))
                node.input_conn = Mock()
                node._send_resize()
                wire = node.input_conn.sendall.call_args.args[0]
                self.assertEqual(wire[:1], b'\x00')
                self.assertEqual(json.loads(wire[1:]),
                                 {'type': 'resize', 'rows': 54, 'cols': 211})
        finally:
            for fd in (master, slave, read_fd, write_fd):
                os.close(fd)

    def test_ignores_zero_size_and_tries_other_terminal(self):
        with patch('cli_node.fcntl.ioctl', side_effect=[
            struct.pack('HHHH', 0, 0, 0, 0), struct.pack('HHHH', 30, 120, 0, 0)
        ]):
            self.assertEqual(CLINode()._get_terminal_size(), (30, 120))

    def test_no_terminal_falls_back(self):
        with patch('cli_node.fcntl.ioctl', side_effect=OSError('not a tty')):
            self.assertEqual(CLINode()._get_terminal_size(), (24, 80))


if __name__ == '__main__':
    unittest.main()
