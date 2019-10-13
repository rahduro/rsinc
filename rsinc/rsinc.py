# rsinc : two-way / bi-drectional sync for rclone

import argparse
import os
import subprocess
import logging
import glob
import re
from datetime import datetime

import ujson as json
import halo
from pyfiglet import Figlet

from .sync import sync, calc_states
from .rclone import make_dirs, lsl
from .packed import pack, merge, unpack, get_branch, empty
from .classes import Flat
from .colors import grn, ylw, red
from .config import config_cli

from .__init__ import __version__

spin = halo.Halo(spinner="dots", placement="right", color="yellow")
CONFIG_FILE = os.path.expanduser("~/.rsinc/config.json")  # Default config path

custom_fig = Figlet(font="graffiti")
print(custom_fig.renderText("Rsinc"))
print("Copyright 2019 C. J. Williams (CHURCHILL COLLEGE)")
print("This is free software with ABSOLUTELY NO WARRANTY")

# ****************************************************************************
# *                                 Functions                                *
# ****************************************************************************


def qt(string):
    return '"' + string + '"'


def read(file):
    """Reads json do dict and returns dict."""
    with open(file, "r") as fp:
        d = json.load(fp)

    return d


def write(file, d):
    """Writes dict to json"""
    with open(file, "w") as fp:
        json.dump(d, fp, sort_keys=True, indent=2)


STB = (
    "yes",
    "ye",
    "y",
    "1",
    "t",
    "true",
    "",
    "go",
    "please",
    "fire away",
    "punch it",
    "sure",
    "ok",
    "hell yes",
)


def strtobool(string):
    return string.lower() in STB


ESCAPE = {
    "\\": "\\\\",
    ".": "\\.",
    "^": "\\^",
    "$": "\\$",
    "*": "\\*",
    "+": "\\+",
    "|": "\\|",
}


def build_regexs(path, files):
    """
    @brief      Compiles relative regexs.

    @param      path   The path of the current lsl search
    @param      files  List of absolute paths to .rignore files

    @return     List of compiled relative reqexes and list of plain text
                relative regexes.
    """
    regex = []
    plain = []

    for file in files:
        for f_char, p_char in zip(os.path.dirname(file), path):
            if f_char != p_char:
                break
        else:
            if os.path.exists(file):
                base = []
                for char in os.path.dirname(file)[len(path) + 1 :]:
                    base.append(ESCAPE.get(char, char))
                base = "".join(base)

                with open(file, "r") as fp:
                    for line in fp:
                        r = os.path.join(base, line.rstrip())
                        plain.append(r)
                        regex.append(re.compile(r))

    return regex, plain


# ****************************************************************************
# *                               Set-up/Parse                               *
# ****************************************************************************


def formatter(prog):
    return argparse.HelpFormatter(prog, max_help_position=52)


parser = argparse.ArgumentParser(formatter_class=formatter)

parser.add_argument("folders", help="Folders to sync", nargs="*")
parser.add_argument("-d", "--dry", action="store_true", help="Do a dry run")
parser.add_argument(
    "-c", "--clean", action="store_true", help="Clean directories"
)
parser.add_argument(
    "-D", "--default", help="Sync defaults", action="store_true"
)
parser.add_argument(
    "-r", "--recovery", action="store_true", help="Enter recovery mode"
)
parser.add_argument(
    "-a", "--auto", help="Don't ask permissions", action="store_true"
)
parser.add_argument(
    "-p", "--purge", help="Reset history for all folders", action="store_true"
)
parser.add_argument(
    "-i", "--ignore", help="Find .rignore files", action="store_true"
)
parser.add_argument(
    "-v", "--version", action="version", version=f"rsinc version: {__version__}"
)
parser.add_argument(
    "--config", action="store_true", help="Enter interactive CLI configurer"
)
parser.add_argument(
    "--config_path", help="Path to config file (default ~/.rsinc/config.json)"
)
parser.add_argument(
    "args",
    nargs=argparse.REMAINDER,
    help="Global flags to pass to rclone commands",
)

args = parser.parse_args()

dry_run = args.dry
auto = args.auto

# ****************************************************************************
# *                              Configuration                               *
# ****************************************************************************

# Read config and assign variables.
if args.config_path is None:
    config_path = CONFIG_FILE
else:
    config_path = args.config_path

if not os.path.isfile(config_path) or args.config:
    config_cli(config_path)

config = read(config_path)

CASE_INSENSATIVE = config["CASE_INSENSATIVE"]
DEFAULT_DIRS = config["DEFAULT_DIRS"]
LOG_FOLDER = config["LOG_FOLDER"]
HASH_NAME = config["HASH_NAME"]
TEMP_FILE = config["TEMP_FILE"]
MASTER = config["MASTER"]
BASE_R = config["BASE_R"]
BASE_L = config["BASE_L"]

# Set up logging.
logging.basicConfig(
    filename=LOG_FOLDER + datetime.now().strftime("%Y-%m-%d"),
    level=logging.DEBUG,
    datefmt="%H:%M:%S",
    format="%(asctime)s %(levelname)s: %(message)s",
)

# ****************************************************************************
# *                               Main Program                               *
# ****************************************************************************


def main():
    """
    Entry point for 'rsinc' as terminal command.
    """

    recover = args.recovery

    # Decide which folder(s) to sync.
    if args.default:
        tmp = DEFAULT_DIRS
    elif len(args.folders) == 0:
        tmp = [os.getcwd()]
    else:
        tmp = []
        for f in args.folders:
            if os.path.isabs(f):
                tmp.append(os.path.normpath(f))
            else:
                tmp.append(os.path.abspath(f))

    folders = []
    for f in tmp:
        if BASE_L not in f:
            print(ylw("Rejecting:"), f, "not in", BASE_L)
        elif not os.path.isdir(f):
            if strtobool(
                input(
                    ylw("WARN: ")
                    + f"{f} does not exist in local, sync anyway? "
                )
            ):
                folders.append(os.path.relpath(f, BASE_L))
        else:
            folders.append(os.path.relpath(f, BASE_L))

    # Get & read master.
    if args.purge or not os.path.exists(MASTER):
        print(ylw("WARN:"), MASTER, "missing, this must be your first run")
        write(MASTER, [[], [], empty()])

    history, ignores, nest = read(MASTER)
    history = set(history)

    # Find all the ignore files in lcl and save them.
    if args.ignore:
        search = os.path.normpath(BASE_L + "/**/.rignore")
        ignores = glob.glob(search, recursive=True)
        write(MASTER, (history, ignores, nest))

    # Detect crashes.
    if os.path.exists(TEMP_FILE):
        corrupt = read(TEMP_FILE)["folder"]
        if corrupt in folders:
            folders.remove(corrupt)

        folders.insert(0, corrupt)
        recover = True
        print(red("ERROR") + ", detected a crash, recovering", corrupt)
        logging.warning("Detected crash, recovering %s", corrupt)

    # Main loop.
    for folder in folders:
        print("")
        path_lcl = os.path.join(BASE_L, folder)
        path_rmt = os.path.join(BASE_R, folder)

        # Determine if first run.
        if os.path.join(BASE_L, folder) in history:
            print(grn("Have:"), qt(folder) + ", entering sync & merge mode")
        else:
            print(ylw("Don't have:"), qt(folder) + ", entering first_sync mode")
            recover = True

        # Build relative regular expressions
        regexs, plain = build_regexs(path_lcl, ignores)
        print("Ignore:", plain)

        # Scan directories.
        spin.start(("Crawling: ") + qt(folder))

        lcl = lsl(path_lcl, HASH_NAME, regexs)
        rmt = lsl(path_rmt, HASH_NAME, regexs)
        old = Flat("old")

        spin.stop_and_persist(symbol="✔")

        # First run & recover mode.
        if recover:
            print("Running", ylw("recover/first_sync"), "mode")
        else:
            print("Reading last state")
            branch = get_branch(nest, folder)
            unpack(branch, old)

            calc_states(old, lcl)
            calc_states(old, rmt)

        print(grn("Dry pass:"))
        total, new_dirs = sync(
            lcl,
            rmt,
            old,
            recover,
            dry_run=True,
            case=CASE_INSENSATIVE,
            flags=args.args,
        )

        print("Found:", total, "job(s)")
        print("With:", len(new_dirs), "folder(s) to make")

        if not dry_run and (
            auto or total == 0 or strtobool(input("Execute? "))
        ):
            if total != 0 or recover:
                print(grn("Live pass:"))

                write(TEMP_FILE, {"folder": folder})

                make_dirs(new_dirs)
                sync(
                    lcl,
                    rmt,
                    old,
                    recover,
                    total=total,
                    case=CASE_INSENSATIVE,
                    dry_run=dry_run,
                    flags=args.args,
                )

                spin.start(grn("Saving: ") + qt(folder))

                # Get post sync state
                if total == 0:
                    print("Skipping crawl as no jobs")
                    now = lcl
                else:
                    now = lsl(path_lcl, HASH_NAME, regexs)

                # Merge into history.
                history.add(os.path.join(BASE_L, folder))
                history.update(d for d in now.dirs)

                # Merge into nest
                merge(nest, folder, pack(now))
                write(MASTER, (history, ignores, nest))

                subprocess.run(["rm", TEMP_FILE])

                spin.stop_and_persist(symbol="✔")

        if args.clean:
            spin.start(grn("Pruning: ") + qt(folder))
            subprocess.run(["rclone", "rmdirs", path_rmt])
            subprocess.run(["rclone", "rmdirs", path_lcl])
            spin.stop_and_persist(symbol="✔")

        recover = args.recovery

    print("")
    print(grn("All synced!"))
