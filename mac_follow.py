"""Keep the Mac release in step with Windows. Runs every 30 minutes on Morgan's Mac (launchd agent
com.morgan.lookhere-mac-follow, installed by `python3 mac_follow.py --install`).

When the newest GitHub release has the Windows installer but no Mac dmg, it builds the Mac app from
that exact tag in its own clone (~/dev/lookhere-release, with its own venv; never the working copy),
then signs, notarises and self-tests it (`build.py --mac --test --publish`), adds the dmg to the
release, and turns the notes' "Mac: ... follows shortly" line into a download line. It sends a macOS
notification when it publishes, and when a version has failed 3 times (each try is 30 min apart, so
a network blip just retries). Log: ~/dev/lookhere-follow/follow.log, one build log per version.

    python3 mac_follow.py              check once (what launchd runs)
    python3 mac_follow.py --dry-run    build, sign, notarise and self-test the newest tag, don't publish
    python3 mac_follow.py --install    copy this script to ~/dev/lookhere-follow and load the agent
"""
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time

REPO = "younggovita/lookhere"
HOME = os.path.expanduser("~")
CLONE = os.path.join(HOME, "dev", "lookhere-release")
TOOL = os.path.join(HOME, "dev", "lookhere-follow")
STATE = os.path.join(TOOL, "state.json")
LOG = os.path.join(TOOL, "follow.log")
LABEL = "com.morgan.lookhere-mac-follow"
AGENT = os.path.join(HOME, "Library", "LaunchAgents", LABEL + ".plist")
MAX_TRIES = 3
PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
MAC_LINE = "Mac: download Lookhere-%s-mac.dmg below. Installed copies offer the update by themselves."


def log(msg):
    os.makedirs(TOOL, exist_ok=True)
    with open(LOG, "a") as f:
        f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))


def notify(title, text):
    subprocess.run(["osascript", "-e", "display notification %s with title %s" % (json.dumps(text), json.dumps(title))])


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw).stdout


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE, "w") as f:
        json.dump(state, f, indent=1)


def fix_notes(tag, version):
    """The PC writes "Mac: the X build follows shortly; until then ..." Replace it with the download line."""
    body = json.loads(run(["gh", "release", "view", tag, "--repo", REPO, "--json", "body"]))["body"] or ""
    lines = body.splitlines()
    new = [MAC_LINE % version if l.startswith("Mac:") and re.search(r"follows|on its way|until then", l) else l
           for l in lines]
    if new != lines:
        run(["gh", "release", "edit", tag, "--repo", REPO, "--notes", "\n".join(new)])
        log("%s: release notes' Mac line updated" % tag)


def screen_ready():
    """The self-test records the screen, which only works while the display is awake and unlocked."""
    code = ("import Quartz; d = Quartz.CGSessionCopyCurrentDictionary() or {}; "
            "print(int(not Quartz.CGDisplayIsAsleep(Quartz.CGMainDisplayID()) "
            "and not d.get('CGSSessionScreenIsLocked', 0)))")
    p = subprocess.run([os.path.join(CLONE, "venv", "bin", "python"), "-c", code], capture_output=True, text=True)
    return p.stdout.strip() == "1"


def build(tag, version, publish):
    """Check out the tag in the release clone and run build.py there. Returns the build log path."""
    run(["git", "-C", CLONE, "fetch", "--tags", "--force", "--quiet", "origin"])
    ref = tag
    if run(["git", "-C", CLONE, "show", tag + ":VERSION"]).strip() != version:
        # The tag was made before the version bump was pushed (it happened with v0.3.5): build
        # main instead when main is at that version and the tag is in its history.
        main_version = run(["git", "-C", CLONE, "show", "origin/main:VERSION"]).strip()
        ancestor = subprocess.run(["git", "-C", CLONE, "merge-base", "--is-ancestor", tag, "origin/main"]).returncode == 0
        if main_version != version or not ancestor:
            raise RuntimeError("VERSION at %s isn't %s, and main is at %s" % (tag, version, main_version))
        ref = "origin/main"
        log("%s: tag predates the version bump, building origin/main (VERSION %s)" % (tag, version))
    run(["git", "-C", CLONE, "checkout", "--force", "--quiet", "--detach", ref])
    run(["git", "-C", CLONE, "clean", "-fdxq", "-e", "venv"])
    # The app comes from the release; the build tools from current main (signing and notarising fixes
    # apply to every release, including ones tagged before them)
    run(["git", "-C", CLONE, "checkout", "origin/main", "--", "build.py", "build_mac.py", "make_licenses.py",
         "mac-entitlements.plist"])
    build_log = os.path.join(TOOL, "build-%s.log" % version)
    cmd = [os.path.join(CLONE, "venv", "bin", "python"), "build.py", "--mac", "--test"] + (["--publish"] if publish else [])
    with open(build_log, "w") as out:
        p = subprocess.run(cmd, cwd=CLONE, stdout=out, stderr=subprocess.STDOUT, timeout=3 * 3600)
    if p.returncode != 0:
        raise RuntimeError("build.py exited %d, see %s" % (p.returncode, build_log))
    return build_log


def check(dry_run=False):
    rel = json.loads(run(["gh", "api", "repos/%s/releases/latest" % REPO]))
    tag, names = rel["tag_name"], [a["name"] for a in rel.get("assets", [])]
    version = tag.lstrip("vV")
    has_mac = any(n.endswith("-mac.dmg") for n in names)
    has_win = any(n.endswith(".exe") for n in names)
    if dry_run:
        log("%s: dry run (build, sign, notarise, self-test; no publish)" % tag)
        while not screen_ready():  # wait (up to a day) for Morgan to be at the Mac
            time.sleep(60)
        try:
            build(tag, version, publish=False)
        except Exception as e:
            notify("Lookhere Mac auto-release: dry run failed", str(e)[:120])
            raise
        log("%s: dry run passed" % tag)
        notify("Lookhere Mac auto-release works", "Dry run of %s built, notarised and self-tested." % tag)
        return
    if has_mac or not has_win:
        return  # in step, or nothing for the Mac to follow yet
    state = load_state()
    tries = state.get(version, 0)
    if tries == "done" or tries >= MAX_TRIES:
        return
    if not screen_ready():
        return  # asleep or locked: try again in 30 minutes (not counted as a failure)
    log("%s: Windows is out, Mac isn't: building (try %d of %d)" % (tag, tries + 1, MAX_TRIES))
    try:
        build(tag, version, publish=True)
        names = [a["name"] for a in json.loads(run(["gh", "release", "view", tag, "--repo", REPO, "--json", "assets"]))["assets"]]
        if "Lookhere-%s-mac.dmg" % version not in names:
            raise RuntimeError("the dmg isn't on the release after publishing")
        fix_notes(tag, version)
        state[version] = "done"
        save_state(state)
        log("%s: Mac dmg published" % tag)
        notify("Lookhere %s for Mac is out" % version, "Signed, notarised, self-tested and added to the release.")
    except Exception as e:
        state[version] = tries + 1
        save_state(state)
        log("%s: try %d failed: %s" % (tag, tries + 1, e))
        if tries + 1 >= MAX_TRIES:
            notify("Lookhere %s for Mac failed" % version, "3 tries failed. Log: ~/dev/lookhere-follow/follow.log")


def install():
    """Copy this script next to its state and load a launchd agent that runs it every 30 minutes."""
    os.makedirs(TOOL, exist_ok=True)
    target = os.path.join(TOOL, "mac_follow.py")
    shutil.copyfile(os.path.abspath(__file__), target)
    import plistlib
    with open(AGENT, "wb") as f:
        plistlib.dump({"Label": LABEL,
                       "ProgramArguments": ["/usr/bin/python3", "-I", target],
                       "StartInterval": 1800, "RunAtLoad": True, "ProcessType": "Background",
                       "EnvironmentVariables": {"PATH": PATH},
                       "StandardOutPath": os.path.join(TOOL, "launchd.out"),
                       "StandardErrorPath": os.path.join(TOOL, "launchd.out")}, f)
    uid = str(os.getuid())
    subprocess.run(["launchctl", "bootout", "gui/%s/%s" % (uid, LABEL)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", "gui/" + uid, AGENT], check=True)
    print("installed %s, running %s every 30 minutes" % (AGENT, target))


def main():
    if "--install" in sys.argv:
        return install()
    os.makedirs(TOOL, exist_ok=True)
    lock = open(os.path.join(TOOL, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # a build from the previous run is still going
    try:
        check(dry_run="--dry-run" in sys.argv)
    except Exception as e:
        log("check failed: %r" % e)
        if "--dry-run" in sys.argv:
            raise


if __name__ == "__main__":
    main()
