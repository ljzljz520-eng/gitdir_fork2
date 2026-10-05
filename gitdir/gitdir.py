#!/usr/bin/python3
import re
import os
import posixpath
import urllib.request
import signal
import argparse
import json
import sys
from colorama import Fore, Style, init

from . import transaction as tx

init()

# this ANSI code lets us erase the current line
ERASE_LINE = "\x1b[2K"

COLOR_NAME_TO_CODE = {"default": "", "red": Fore.RED, "green": Style.BRIGHT + Fore.GREEN}

USER_AGENT = "Mozilla/5.0"


def print_text(text, color="default", in_place=False, **kwargs):  # type: (str, str, bool, any) -> None
    """
    print text to console, a wrapper to built-in print

    :param text: text to print
    :param color: can be one of "red" or "green", or "default"
    :param in_place: whether to erase previous line and print in place
    :param kwargs: other keywords passed to built-in print
    """
    if in_place:
        print("\r" + ERASE_LINE, end="")
    print(COLOR_NAME_TO_CODE[color] + text + Style.RESET_ALL, **kwargs)


def create_url(url):
    """
    From the given url, produce a URL that is compatible with Github's REST API.
    Can handle blob or tree paths. Returns (api_url, download_dirs, kind).
    """
    repo_only_url = re.compile(r"https:\/\/github\.com\/[a-z\d](?:[a-z\d]|-(?=[a-z\d])){0,38}\/[a-zA-Z0-9]+$")
    re_branch = re.compile("/(tree|blob)/(.+?)/")

    # Check if the given url is a url to a GitHub repo. If it is, tell the
    # user to use 'git clone' to download it
    if re.match(repo_only_url, url):
        print_text("✘ The given url is a complete repository. Use 'git clone' to download the repository",
                   "red", in_place=True)
        sys.exit()

    # extract the branch name from the given url (e.g master)
    branch = re_branch.search(url)
    kind = branch.group(1)
    download_dirs = url[branch.end():]
    api_url = (url[:branch.start()].replace("github.com", "api.github.com/repos", 1) +
              "/contents/" + download_dirs + "?ref=" + branch.group(2))
    return api_url, download_dirs, kind


# ---------------------------------------------------------------------------
# GitHub enumeration (immutable source plan)
# ---------------------------------------------------------------------------


def _next_page_url(link_header):
    # Link: <https://api.github.com/...&page=2>; rel="next", ...
    if not link_header:
        return None
    for part in link_header.split(","):
        sections = part.split(";")
        if len(sections) < 2:
            continue
        if sections[1].strip() == 'rel="next"':
            return sections[0].strip().lstrip("<").rstrip(">")
    return None


def _api_get(url):
    """GET one GitHub API URL; returns (parsed body, next page url)."""
    request = urllib.request.Request(url, headers={"User-agent": USER_AGENT})
    with urllib.request.urlopen(request) as response:
        body = json.loads(response.read().decode("utf-8"))
        next_url = _next_page_url(response.headers.get("Link", ""))
    return body, next_url


def _paginated(url):
    while url:
        body, url = _api_get(url)
        if isinstance(body, dict):
            yield body
            return
        for item in body:
            yield item


def _with_ref(url, ref):
    if ref and "?" not in url:
        return url + "?ref=" + ref
    return url


def enumerate_entries(api_url, download_dirs, kind):
    """
    Walk the contents API *before writing anything* and return the immutable
    source plan: [{path, url, git_sha, size}, ...] with paths relative to the
    requested subtree.
    """
    ref = api_url.rsplit("?ref=", 1)[1] if "?ref=" in api_url else ""
    subtree = download_dirs if kind == "tree" else posixpath.dirname(download_dirs)
    entries = []
    warnings = []

    def walk(page_url, is_root=False):
        for item in _paginated(page_url):
            item_type = item.get("type")
            if item_type == "file":
                if kind == "tree":
                    rel = posixpath.relpath(item["path"], subtree)
                else:
                    rel = posixpath.basename(item["path"])
                entries.append({
                    "path": rel,
                    "url": item.get("download_url"),
                    "git_sha": item.get("sha"),
                    "size": item.get("size"),
                })
            elif item_type == "dir":
                walk(_with_ref(item["url"], ref))
            else:
                warnings.append("%s (%s)" % (item.get("path"), item_type))

    walk(api_url, is_root=True)
    entries.sort(key=lambda e: e["path"])
    return entries, warnings


def http_fetcher(url):
    """Return a binary context manager streaming the given source URL."""
    request = urllib.request.Request(url, headers={"User-agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=60)


# ---------------------------------------------------------------------------
# Legacy direct download (existing-target overwrite policy, kept separate from
# the transaction contract)
# ---------------------------------------------------------------------------


def legacy_download(repo_url, flatten=False, output_dir="./"):
    """ Downloads the files and directories in repo_url. If flatten is specified, the contents of any and all
     sub-directories will be pulled upwards into the root folder. """

    # generate the url which returns the JSON data
    api_url, download_dirs, _kind = create_url(repo_url)

    # To handle file names.
    if not flatten:
        if len(download_dirs.split(".")) == 0:
            dir_out = os.path.join(output_dir, download_dirs)
        else:
            dir_out = os.path.join(output_dir, "/".join(download_dirs.split("/")[:-1]))
    else:
        dir_out = output_dir

    try:
        opener = urllib.request.build_opener()
        opener.addheaders = [('User-agent', USER_AGENT)]
        urllib.request.install_opener(opener)
        response = urllib.request.urlretrieve(api_url)
    except KeyboardInterrupt:
        # when CTRL+C is pressed during the execution of this script,
        # bring the cursor to the beginning, erase the current line, and dont make a new line
        print_text("✘ Got interrupted", "red", in_place=True)
        sys.exit()

    if not flatten:
        # make a directory with the name which is taken from
        # the actual repo
        os.makedirs(dir_out, exist_ok=True)

    # total files count
    total_files = 0

    with open(response[0], "r") as f:
        data = json.load(f)
        # getting the total number of files so that we
        # can use it for the output information later
        total_files += len(data)

        # If the data is a file, download it as one.
        if isinstance(data, dict) and data["type"] == "file":
            try:
                # download the file
                opener = urllib.request.build_opener()
                opener.addheaders = [('User-agent', USER_AGENT)]
                urllib.request.install_opener(opener)
                urllib.request.urlretrieve(data["download_url"], os.path.join(dir_out, data["name"]))
                # bring the cursor to the beginning, erase the current line, and dont make a new line
                print_text("Downloaded: " + Fore.WHITE + "{}".format(data["name"]), "green", in_place=True)

                return total_files
            except KeyboardInterrupt:
                # when CTRL+C is pressed during the execution of this script,
                # bring the cursor to the beginning, erase the current line, and dont make a new line
                print_text("✘ Got interrupted", 'red', in_place=False)
                sys.exit()

        for file in data:
            file_url = file["download_url"]
            file_name = file["name"]
            file_path = file["path"]

            if flatten:
                path = os.path.basename(file_path)
            else:
                path = file_path
            dirname = os.path.dirname(path)

            if dirname != '':
                os.makedirs(os.path.dirname(path), exist_ok=True)
            else:
                pass

            if file_url is not None:
                try:
                    opener = urllib.request.build_opener()
                    opener.addheaders = [('User-agent', USER_AGENT)]
                    urllib.request.install_opener(opener)
                    # download the file
                    urllib.request.urlretrieve(file_url, path)

                    # bring the cursor to the beginning, erase the current line, and dont make a new line
                    print_text("Downloaded: " + Fore.WHITE + "{}".format(file_name), "green", in_place=False, end="\n",
                               flush=True)

                except KeyboardInterrupt:
                    # when CTRL+C is pressed during the execution of this script,
                    # bring the cursor to the beginning, erase the current line, and dont make a new line
                    print_text("✘ Got interrupted", 'red', in_place=False)
                    sys.exit()
            else:
                legacy_download(file["html_url"], flatten, download_dirs)

    return total_files


# ---------------------------------------------------------------------------
# Transactional download
# ---------------------------------------------------------------------------


def target_for(api_parts, output_dir):
    """The reserved target directory for a parsed GitHub URL."""
    _api_url, download_dirs, kind = api_parts
    if kind == "blob":
        leaf = posixpath.dirname(download_dirs)
        return os.path.normpath(os.path.join(output_dir, leaf)) if leaf else os.path.normpath(output_dir)
    return os.path.normpath(os.path.join(output_dir, download_dirs))


def _progress(event, entry):
    name = entry.get("path", "?")
    if event == "downloaded":
        print_text("Downloaded: " + Fore.WHITE + name, "green")
    elif event == "verified":
        print_text("Verified:   " + Fore.WHITE + name, "default")


def _print_scan_header():
    print_text("Scanning for interrupted transactions ...", "default")


def _print_status_line(prefix, status):
    stage = status["stage"]
    target = status.get("target") or status.get("staging")
    manifest_id = status.get("manifest_id") or "-"
    match = status.get("manifest_matches")
    match_note = ""
    if match is True:
        match_note = " [manifest matches request]"
    elif match is False:
        match_note = " [MANIFEST MISMATCH: stale/different request]"
    torn = " [journal tail torn]" if status.get("torn_tail") else ""
    corrupt = (" [corrupt: %s]" % status.get("corrupt_reason")) if status.get("corrupt_reason") else ""
    print_text("%s %s  stage=%s  files=%s/%s  manifest=%s%s%s%s"
               % (prefix, target, stage, status.get("files_done", 0),
                  status.get("expected_files", 0), manifest_id[:16],
                  match_note, torn, corrupt),
               "red" if (match is False or corrupt) else "default")


def startup_scan(output_dir):
    """Read-only report of staging directories left by interrupted processes."""
    bases = sorted({os.path.abspath(output_dir), os.path.abspath(".")})
    found = []
    for base in bases:
        found.extend(tx.discover(base))
    if not found:
        return []
    _print_scan_header()
    for staging in found:
        journal_path = os.path.join(staging, tx.JOURNAL_NAME)
        target = staging
        manifest_id = "-"
        stage = "empty"
        files_done = 0
        if os.path.exists(journal_path):
            records, _last, torn, corrupt = tx.replay_journal(journal_path)
            for rec in records:
                if rec.get("t") == "BEGIN":
                    target = rec.get("target", target)
                    manifest_id = rec.get("manifest_id", "-")
                elif rec.get("t") == "FILE":
                    files_done += 1
                elif rec.get("t") == "PREPARED":
                    stage = tx.STAGE_PREPARED
                elif rec.get("t") == "COMMITTED":
                    stage = tx.STAGE_COMMITTED
                elif rec.get("t") == "CLEANED":
                    stage = tx.STAGE_CLEANED
            if stage in ("empty",) and records:
                stage = tx.STAGE_ACTIVE
            if corrupt:
                stage = tx.STAGE_CORRUPT
            if torn and stage != tx.STAGE_CORRUPT:
                stage = stage + " (torn tail)"
        print_text("• interrupted transaction: target=%s stage=%s files_done=%d manifest=%s"
                   % (target, stage, files_done, str(manifest_id)[:16]),
                   "default")
    return found


def transactional_download(repo_url, output_dir, on_interrupted):
    """
    Reserve the target, stage every verified file in a sibling staging
    directory, then atomically publish. Recovery decisions are deterministic
    and gated on the immutable source manifest.
    """
    api_parts = create_url(repo_url)
    api_url, download_dirs, kind = api_parts
    print_text("Enumerating source plan: " + Fore.WHITE + download_dirs, "default")
    entries, warnings = enumerate_entries(api_url, download_dirs, kind)
    for warning in warnings:
        print_text("• skipped non-regular entry: " + warning, "default")

    target = target_for(api_parts, output_dir)
    staging = tx.staging_path(target)

    if os.path.exists(staging):
        status = tx.report(target, requested_entries=entries)
        _print_status_line("Interrupted transaction found:", status)
        if on_interrupted == "report":
            print_text("✘ Refusing to continue while an interrupted transaction exists. "
                       "Re-run with --on-interrupted resume or --on-interrupted cleanup.",
                       "red")
            return False
        if on_interrupted == "resume":
            try:
                result = tx.resume(target, entries, http_fetcher, progress=_progress)
            except tx.ManifestMismatchError as exc:
                print_text("✘ Stale/mismatched resume rejected: %s" % exc, "red")
                return False
            except tx.CorruptJournalError as exc:
                print_text("✘ Cannot resume, journal corrupt: %s" % exc, "red")
                return False
            except Exception as exc:
                print_text("✘ Resume failed; staged transaction kept for recovery: %s"
                           % exc, "red")
                return False
            print_text("✔ Recovery complete (%s): %s" % (result, target), "green")
            return True
        if on_interrupted == "cleanup":
            try:
                tx.cleanup(target, entries)
            except tx.ManifestMismatchError as exc:
                print_text("✘ Cleanup rejected (manifest mismatch): %s" % exc, "red")
                return False
            except tx.CorruptJournalError as exc:
                print_text("✘ Cleanup rejected (journal corrupt): %s" % exc, "red")
                return False
            print_text("• Stale transaction cleaned; starting fresh", "default")

    if os.path.lexists(target):
        # Existing-target overwrite policy is deliberately separate from the
        # transaction contract; unresolved overwrite confirmation is out of
        # scope, so the legacy direct-write behaviour applies here.
        print_text("• Target already exists (%s); using existing overwrite policy "
                   "(outside the transaction contract)" % target, "default")
        legacy_download(repo_url, flatten=False, output_dir=output_dir)
        return True

    try:
        result = tx.run(target, entries, http_fetcher, progress=_progress)
    except tx.TargetExistsError:
        raise
    except Exception as exc:
        # The staged transaction is intentionally kept: the next invocation
        # can deterministically resume, clean up, or just report it.
        print_text("✘ Download interrupted; staged transaction kept for recovery: %s"
                   % exc, "red")
        return False
    if result == "joined":
        print_text("✔ Already published by a cooperating process: " + target, "green")
    else:
        print_text("✔ Published atomically: " + target, "green")
    return True


def main():
    if sys.platform != 'win32':
        # disbale CTRL+Z
        signal.signal(signal.SIGTSTP, signal.SIG_IGN)

    parser = argparse.ArgumentParser(description="Download directories/folders from GitHub")
    parser.add_argument('urls', nargs="+",
                        help="List of Github directories to download.")
    parser.add_argument('--output_dir', "-d", dest="output_dir", default="./",
                        help="All directories will be downloaded to the specified directory.")

    parser.add_argument('--flatten', '-f', action="store_true",
                        help='Flatten directory structures. Do not create extra directory and download found files to'
                             ' output directory. (default to current directory if not specified)')

    parser.add_argument('--on-interrupted', dest="on_interrupted",
                        choices=("resume", "cleanup", "report"), default="report",
                        help="What to do when an interrupted transaction for a target is found: "
                             "'resume' continues it only if the source manifest matches, "
                             "'cleanup' removes the matching staging directory, "
                             "'report' (default) only reports and skips the target.")
    parser.add_argument('--no-transaction', dest="no_transaction", action="store_true",
                        help="Disable the staging/atomic-publish transaction and download "
                             "directly to the target (legacy behaviour).")

    args = parser.parse_args()

    if not args.no_transaction:
        # Scan even in flatten mode: other (non-flatten) invocations may have
        # left interrupted transactions behind in the same directories.
        startup_scan(args.output_dir)

    ok = True
    for url in args.urls:
        try:
            if args.flatten or args.no_transaction:
                legacy_download(url, args.flatten, args.output_dir)
            else:
                ok = transactional_download(url, args.output_dir, args.on_interrupted) and ok
        except tx.ManifestMismatchError as exc:
            print_text("✘ Stale/mismatched transaction rejected: %s" % exc, "red")
            ok = False
        except tx.TargetExistsError as exc:
            print_text("✘ %s" % exc, "red")
            ok = False
        except tx.TransactionError as exc:
            print_text("✘ Transaction failed (staging kept for recovery): %s" % exc, "red")
            ok = False
        except KeyboardInterrupt:
            print_text("✘ Got interrupted. The staged transaction is durable; re-run with "
                       "--on-interrupted resume (or cleanup|report).", 'red')
            ok = False
            break

    if ok:
        print_text("✔ Download complete", "green", in_place=True)
    else:
        print_text("✘ Download finished with unresolved targets; see messages above.", "red")
        sys.exit(1)


if __name__ == "__main__":
    main()
