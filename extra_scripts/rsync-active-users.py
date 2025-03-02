"""
rsync home directories of active users only.

When rsyncing home directories of users across disks, it is extremely
helpful to only do so for users who have been recently active. The hub
API allows us to see who those users are, and we can rsync just their
home directories.

An environment variable 'JUPYTERHUB_ADMIN' must be set with an admin token,
obtainable from {hub_url}/hub/token by an admin user.
"""
import argparse
import os
import string
import subprocess
import sys
import time

# Copied from https://github.com/minrk/escapism/blob/d1d406c69b9ab0b14aa562d98a9e198adf9c047a/escapism.py
# this is the library JuptyerHub uses to escape usernames into a form that works for filesystem paths
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
from dateutil.parser import parse

SAFE = set(string.ascii_letters + string.digits)
ESCAPE_CHAR = "_"


def _escape_char(c, escape_char=ESCAPE_CHAR):
    """Escape a single character"""
    buf = []
    for byte in c.encode("utf8"):
        buf.append(escape_char)
        buf.append("%X" % byte)
    return "".join(buf)


def escape(to_escape, safe=SAFE, escape_char=ESCAPE_CHAR, allow_collisions=False):
    """Escape a string so that it only contains characters in a safe set.

    Characters outside the safe list will be escaped with _%x_,
    where %x is the hex value of the character.

    If `allow_collisions` is True, occurrences of `escape_char`
    in the input will not be escaped.

    In this case, `unescape` cannot be used to reverse the transform
    because occurrences of the escape char in the resulting string are ambiguous.
    Only use this mode when:

    1. collisions cannot occur or do not matter, and
    2. unescape will never be called.

    .. versionadded: 1.0
        allow_collisions argument.
        Prior to 1.0, behavior was the same as allow_collisions=False (default).

    """
    if isinstance(to_escape, bytes):
        # always work on text
        to_escape = to_escape.decode("utf8")

    if not isinstance(safe, set):
        safe = set(safe)

    if allow_collisions:
        safe.add(escape_char)
    elif escape_char in safe:
        warnings.warn(
            "Escape character %r cannot be a safe character."
            " Set allow_collisions=True if you want to allow ambiguous escaped strings."
            % escape_char,
            RuntimeWarning,
            stacklevel=2,
        )
        safe.remove(escape_char)

    chars = []
    for c in to_escape:
        if c in safe:
            chars.append(c)
        else:
            chars.append(_escape_char(c, escape_char))
    return "".join(chars)


def get_all_users(hub_url, token):
    url = f"{hub_url}/hub/api/users"
    resp = requests.get(url, headers={"Authorization": f"token {token}"})

    users = resp.json()
    for user in users:
        if user["last_activity"]:
            user["last_activity"] = parse(user.get("last_activity"))
    return users


def rsync(user, src_basedir, dest_basedir, dry_run):
    start_time = time.perf_counter()
    safe_chars = set(string.ascii_lowercase + string.digits)
    homedir = escape(user, safe_chars, "-").lower()
    src_homedir = os.path.join(src_basedir, homedir)
    if not os.path.exists(src_homedir):
        print(f"Directory {src_homedir} does not exist for user {user}, aborting")
        sys.exit(1)
    rsync_cmd = [
        "rsync",
        "-av",
        "--delete",
        "--ignore-errors",
        src_homedir,
        dest_basedir,
    ]
    if not dry_run:
        subprocess.check_output(rsync_cmd)
    return user, time.perf_counter() - start_time


def main():
    argparser = argparse.ArgumentParser()

    argparser.add_argument("hub_url", help="Base URL of the JupyterHub to talk to")
    argparser.add_argument(
        "hours_ago",
        help="How recently should the user have been active to be rsynced",
        type=int,
    )
    argparser.add_argument(
        "src_basedir", help="Base directory containing home directories to be rsynced"
    )
    argparser.add_argument(
        "dest_basedir",
        help="Base directory where home directories should be rsynced to",
    )
    argparser.add_argument(
        "--actually-run-rsync",
        action="store_true",
        help="Actually run rsync, otherwise we just dry-run",
    )
    argparser.add_argument(
        "--concurrency", type=int, help="How many parallel rsyncs to run", default=16
    )

    args = argparser.parse_args()

    if "JUPYTERHUB_TOKEN" not in os.environ:
        print("Could not find JUPYTERHUB_TOKEN in environment.")
        print("Please get an admin user's token from {args.hub_url}/hub/token")
        sys.exit(1)

    time_since = datetime.now(timezone.utc) - timedelta(hours=args.hours_ago)
    users_since = []

    for user in get_all_users(args.hub_url, os.environ["JUPYTERHUB_TOKEN"]):
        if user["last_activity"]:
            if user["last_activity"] >= time_since:
                users_since.append(user["name"])

    pool = ThreadPoolExecutor(max_workers=args.concurrency)
    futures = []
    completed_futures = 0

    for user in users_since:
        # Escaping logic from https://github.com/jupyterhub/kubespawner/blob/0eecad35d8829d8d599be876ee26c192d622e442/kubespawner/spawner.py#L1340
        # tarring is CPU bound, so we can parallelize trivially.
        # FIXME: This should be tuneable, or at least default to some multiple of number of cores on the system

        future = pool.submit(
            rsync,
            user,
            args.src_basedir,
            args.dest_basedir,
            not args.actually_run_rsync,
        )
        futures.append(future)

    for future in as_completed(futures):
        completed_user, duration = future.result()
        completed_futures += 1
        print(
            f"Finished {completed_futures} of {len(users_since)} in {duration:0.3f} - user {completed_user}"
        )

    if not args.actually_run_rsync:
        print("No rsync commands were actually performed")
        print(
            "Check the rsync commands output, and then run this command with `--actually-run-rsync`"
        )


if __name__ == "__main__":
    main()
