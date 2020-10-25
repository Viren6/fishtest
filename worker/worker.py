#!/usr/bin/python
from __future__ import print_function

import json
import math
import multiprocessing
import os
import platform
import random
import re
import signal
import sys
import threading
import time
import traceback
import uuid

import requests

try:
    from ConfigParser import SafeConfigParser

    config = SafeConfigParser()
except ImportError:
    from configparser import ConfigParser  # Python3

    config = ConfigParser()
import base64
import zlib
from datetime import datetime
from optparse import OptionParser
from os import path

from games import run_games
from updater import update

WORKER_VERSION = 90
ALIVE = True
HTTP_TIMEOUT = 15.0


def setup_config_file(config_file):
    """ Config file setup, adds defaults if not existing """
    config.read(config_file)

    mem = 0
    system_type = platform.system().lower()
    try:
        if "linux" in system_type:
            cmd = "free -b"
        elif "windows" in system_type:
            cmd = "wmic computersystem get TotalPhysicalMemory"
        elif "darwin" in system_type:
            cmd = "sysctl hw.memsize"
        else:
            cmd = ""
            print("Unknown system")
        with os.popen(cmd) as proc:
            mem_str = str(proc.readlines())
        mem = int(re.search(r"\d+", mem_str).group())
        print("Memory: " + str(mem))
    except:
        traceback.print_exc()
        pass

    defaults = [
        ("login", "username", ""),
        ("login", "password", ""),
        ("parameters", "protocol", "https"),
        ("parameters", "host", "tests.stockfishchess.org"),
        ("parameters", "port", "443"),
        ("parameters", "concurrency", "3"),
        ("parameters", "max_memory", str(int(mem / 2 / 1024 / 1024))),
        ("parameters", "min_threads", "1"),
    ]

    for v in defaults:
        if not config.has_section(v[0]):
            config.add_section(v[0])
        if not config.has_option(v[0], v[1]):
            config.set(*v)
            with open(config_file, "w") as f:
                config.write(f)

    return config


def on_sigint(signal, frame):
    global ALIVE
    ALIVE = False
    raise Exception("Terminated by signal")

def worker_exit():
    os._exit(1)


rate = None


def get_rate():
    global rate
    try:
        rate = requests.get(
            "https://api.github.com/rate_limit", timeout=HTTP_TIMEOUT
        ).json()["rate"]
    except Exception as e:
        sys.stderr.write("Exception fetching rate_limit:\n")
        print(e, file=sys.stderr)
        rate = {"remaining": 0, "limit": 5000}
        return True
    remaining = rate["remaining"]
    print("API call rate limits:", rate)
    return remaining >= math.sqrt(rate["limit"])


RUN = None
TASK_ID = None

def heartbeat(worker_info, password, remote):
    global ALIVE, RUN, TASK_ID
    print("Start heartbeat")
    payload = {"username": worker_info["username"], "password": password}
    count = 0
    while ALIVE:
        time.sleep(1)
        count += 1
        if count == 60:
            count = 0
            print("Send heartbeat... ", end='')
            payload["run_id"] = str(RUN["_id"]) if RUN else None
            payload["task_id"] = TASK_ID
            req = requests.post(
                remote + "/api/beat",
                data=json.dumps(payload),
                headers={"Content-type": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
            try:
                req = json.loads(req.text)
                print(req)
            except Exception as e:
                sys.stderr.write("Exception from calling heartbeat:\n")
                print(e, file=sys.stderr)


def worker(worker_info, password, remote):
    global ALIVE

    payload = {"worker_info": worker_info, "password": password}

    try:
        print("Fetch task...")
        if not get_rate():
            raise Exception("Near API limit")

        t0 = datetime.utcnow()
        req = requests.post(
            remote + "/api/request_version",
            data=json.dumps(payload),
            headers={"Content-type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        req = json.loads(req.text)

        if "version" not in req:
            print("Incorrect username/password")
            time.sleep(5)
            worker_exit()

        if req["version"] > WORKER_VERSION:
            print("Updating worker version to {}".format(req["version"]))
            update()
        print(
            "Worker version checked successfully in {}s".format(
                (datetime.utcnow() - t0).total_seconds()
            )
        )

        t0 = datetime.utcnow()
        worker_info["rate"] = rate
        req = requests.post(
            remote + "/api/request_task",
            data=json.dumps(payload),
            headers={"Content-type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        req = json.loads(req.text)
    except Exception as e:
        sys.stderr.write("Exception accessing host:\n")
        print(e, file=sys.stderr)
        #    traceback.print_exc()
        time.sleep(random.randint(10, 60))
        return

    print("Task requested in {}s".format((datetime.utcnow() - t0).total_seconds()))
    if "error" in req:
        raise Exception("Error from remote: {}".format(req["error"]))

    # No tasks ready for us yet, just wait...
    if "task_waiting" in req:
        print("No tasks available at this time, waiting...\n")
        # Note that after this sleep we have another ALIVE HTTP_TIMEOUT...
        time.sleep(random.randint(1, 10))
        return

    success = True
    global RUN, TASK_ID
    RUN, TASK_ID = req["run"], req["task_id"]
    try:
        pgn_file = run_games(worker_info, password, remote, RUN, TASK_ID)
    except:
        sys.stderr.write("\nException running games:\n")
        traceback.print_exc()
        success = False
    finally:
        payload = {
            "username": worker_info["username"],
            "password": password,
            "run_id": str(RUN["_id"]),
            "task_id": TASK_ID,
        }
        try:
            requests.post(
                remote + "/api/failed_task",
                data=json.dumps(payload),
                headers={"Content-type": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
        except:
            pass

        TASK_ID = None
        if success and ALIVE:
            sleep = random.randint(1, 10)
            print("Wait {} seconds before upload of PGN...".format(sleep))
            time.sleep(sleep)
            if "spsa" not in RUN["args"]:
                try:
                    with open(pgn_file, "r") as f:
                        data = f.read()
                    # Ignore non utf-8 characters in PGN file
                    if sys.version_info[0] == 2:
                        data = data.decode("utf-8", "ignore").encode(
                            "utf-8"
                        )  # Python 2
                    else:
                        data = bytes(data, "utf-8").decode(
                            "utf-8", "ignore"
                        )  # Python 3
                    payload["pgn"] = base64.b64encode(
                        zlib.compress(data.encode("utf-8"))
                    ).decode()
                    print(
                        "Uploading compressed PGN of {} bytes".format(
                            len(payload["pgn"])
                        )
                    )
                    requests.post(
                        remote + "/api/upload_pgn",
                        data=json.dumps(payload),
                        headers={"Content-type": "application/json"},
                        timeout=HTTP_TIMEOUT,
                    )
                except Exception as e:
                    sys.stderr.write("\nException PGN upload:\n")
                    print(e, file=sys.stderr)
        #          traceback.print_exc()
        try:
            os.remove(pgn_file)
        except:
            pass
        sys.stderr.write("Task exited\n")

    return success


def main():
    worker_dir = path.dirname(path.realpath(__file__))
    print("Worker started in " + worker_dir + " ...\n")

    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGTERM, on_sigint)

    config_file = path.join(worker_dir, "fishtest.cfg")
    config = setup_config_file(config_file)
    parser = OptionParser()
    parser.add_option(
        "-P",
        "--protocol",
        dest="protocol",
        default=config.get("parameters", "protocol"),
    )
    parser.add_option(
        "-n", "--host", dest="host", default=config.get("parameters", "host")
    )
    parser.add_option(
        "-p", "--port", dest="port", default=config.get("parameters", "port")
    )
    parser.add_option(
        "-c",
        "--concurrency",
        dest="concurrency",
        default=config.get("parameters", "concurrency"),
    )
    parser.add_option(
        "-m",
        "--max_memory",
        dest="max_memory",
        default=config.get("parameters", "max_memory"),
    )
    parser.add_option(
        "-t",
        "--min_threads",
        dest="min_threads",
        default=config.get("parameters", "min_threads"),
    )
    (options, args) = parser.parse_args()

    if len(args) != 2:
        # Try to read parameters from the the config file
        username = config.get("login", "username")
        password = config.get("login", "password", raw=True)
        if len(username) != 0 and len(password) != 0:
            args.extend([username, password])
        else:
            sys.stderr.write("{} [username] [password]\n".format(sys.argv[0]))
            worker_exit()

    # Write command line parameters to the config file
    config.set("login", "username", args[0])
    config.set("login", "password", args[1])
    config.set("parameters", "protocol", options.protocol)
    config.set("parameters", "host", options.host)
    config.set("parameters", "port", options.port)
    config.set("parameters", "concurrency", options.concurrency)
    config.set("parameters", "max_memory", options.max_memory)
    config.set("parameters", "min_threads", options.min_threads)
    with open(config_file, "w") as f:
        config.write(f)

    protocol = options.protocol.lower()
    if protocol not in ["http", "https"]:
        sys.stderr.write("Wrong protocol, use https or http\n")
        worker_exit()
    elif protocol == "http" and options.port == "443":
        # Rewrite old port 443 to 80
        options.port = "80"
    elif protocol == "https" and options.port == "80":
        # Rewrite old port 80 to 443
        options.port = "443"
    remote = "{}://{}:{}".format(protocol, options.host, options.port)
    print("Worker version {} connecting to {}".format(WORKER_VERSION, remote))

    try:
        cpu_count = min(int(options.concurrency), multiprocessing.cpu_count() - 1)
    except:
        cpu_count = int(options.concurrency)

    if cpu_count <= 0:
        sys.stderr.write("Not enough CPUs to run fishtest (it requires at least two)\n")
        worker_exit()

    uname = platform.uname()
    worker_info = {
        "uname": uname[0] + " " + uname[2],
        "architecture": platform.architecture(),
        "concurrency": cpu_count,
        "max_memory": int(options.max_memory),
        "min_threads": int(options.min_threads),
        "username": args[0],
        "version": "{}:{}".format(WORKER_VERSION, sys.version_info[0]),
        "unique_key": str(uuid.uuid4()),
    }

    # Start heartbeat
    threading.Thread(target=heartbeat, args=(worker_info, args[1], remote)).start()

    success = True
    global ALIVE
    while ALIVE:
        if path.isfile(path.join(worker_dir, "fish.exit")):
            ALIVE = False
            break
        if not success:
            time.sleep(HTTP_TIMEOUT)
        success = worker(worker_info, args[1], remote)


if __name__ == "__main__":
    main()
