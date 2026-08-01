#!/usr/bin/env python3
"""Forward a TCP connection while adding one-way latency."""

import argparse
import os
import queue
import socket
import sys
import threading
import time


BUFFER_SIZE = 64 * 1024
_END = object()


def _nonnegative_int(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def _port(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return number


def _read_stdin():
    return os.read(0, BUFFER_SIZE)


def _read_socket(connection):
    return connection.recv(BUFFER_SIZE)


def _read_stream(read, pending, delay, stop, errors):
    try:
        while not stop.is_set():
            data = read()
            if not data:
                break
            pending.put((time.monotonic() + delay, data))
    except OSError as error:
        if not stop.is_set():
            errors.append(error)
        stop.set()
    finally:
        pending.put(_END)


def _wait_until(deadline, stop):
    while not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        if stop.wait(min(remaining, 1)):
            return False
    return False


def _write_stdout(data):
    view = memoryview(data)
    while view:
        view = view[os.write(1, view):]


def _write_stream(pending, write, on_eof, stop, errors):
    try:
        while True:
            try:
                item = pending.get(timeout=0.1)
            except queue.Empty:
                if stop.is_set():
                    return
                continue
            if item is _END:
                if not stop.is_set():
                    on_eof()
                return
            deadline, data = item
            if not _wait_until(deadline, stop):
                return
            write(data)
    except OSError as error:
        if not stop.is_set():
            errors.append(error)
        stop.set()


def _shutdown_write(connection, stop, errors):
    try:
        connection.shutdown(socket.SHUT_WR)
    except OSError as error:
        if not stop.is_set():
            errors.append(error)
            stop.set()
        return


def _forward(connection, delay):
    stop = threading.Event()
    errors = []

    # ponytail: unbounded queues avoid turning latency into a bandwidth cap; bound them if bulk-input memory use matters.
    stdin_queue = queue.Queue()
    stdout_queue = queue.Queue()
    threads = [
        threading.Thread(
            target=_read_stream,
            args=(_read_stdin, stdin_queue, delay, stop, errors),
            daemon=True,
        ),
        threading.Thread(
            target=_write_stream,
            args=(
                stdin_queue,
                connection.sendall,
                lambda: _shutdown_write(connection, stop, errors),
                stop,
                errors,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=_read_stream,
            args=(lambda: _read_socket(connection), stdout_queue, delay, stop, errors),
            daemon=True,
        ),
        threading.Thread(
            target=_write_stream,
            args=(stdout_queue, _write_stdout, lambda: stop.set(), stop, errors),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()

    try:
        stop.wait()
    except KeyboardInterrupt:
        stop.set()
    finally:
        connection.close()
        for thread in threads:
            thread.join(timeout=0.2)

    if errors:
        print(f"ssh latency proxy: {errors[0]}", file=sys.stderr)
        return 1
    return 0


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delay-ms", type=_nonnegative_int, default=0)
    parser.add_argument("host")
    parser.add_argument("port", type=_port)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        delay = args.delay_ms / 1000
    except OverflowError:
        print("ssh latency proxy: delay is too large", file=sys.stderr)
        return 1

    try:
        connection = socket.create_connection((args.host, args.port))
    except KeyboardInterrupt:
        return 0
    except OSError as error:
        print(f"ssh latency proxy: {error}", file=sys.stderr)
        return 1

    try:
        return _forward(connection, delay)
    except KeyboardInterrupt:
        connection.close()
        return 0
    except OSError as error:
        connection.close()
        print(f"ssh latency proxy: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
