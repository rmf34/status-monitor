#!/usr/bin/env python3
"""
status-monitor configuration CLI.

Subcommands:
    setup                     Interactive wizard (first-run)
    add gcp <project>         Detect services in a GCP project, append
    add github <owner/name>   Detect components for a GitHub repo, append
    add anthropic             Add or update Anthropic component selection
    remove gcp <project>      Drop a GCP project from config
    remove github <owner/r>   Drop a GitHub repo from config
    remove anthropic          Drop Anthropic block
    list                      Print current config
    refresh                   Re-detect services/components for everything
    doctor                    Sanity-check tools and auth state
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
GCP_MAP_PATH = ROOT / "data" / "gcp_product_map.yaml"

GITHUB_COMPONENTS = [
    "Git Operations", "API Requests", "Webhooks", "Issues",
    "Pull Requests", "Actions", "Packages", "Pages",
    "Codespaces", "Copilot", "Copilot AI Model Providers",
]

ANTHROPIC_COMPONENT_OPTIONS = [
    ("Claude API (api.anthropic.com)",
     "API endpoint - direct Anthropic API integrations"),
    ("Claude Code",
     "Claude Code CLI specifically"),
    ("claude.ai",
     "Web UI"),
    ("Claude Console (platform.claude.com)",
     "Admin / billing console"),
    ("Claude Cowork",
     "Cowork product"),
]
ANTHROPIC_DEFAULTS = ["Claude API (api.anthropic.com)", "Claude Code"]


def _run(args, capture=True, check=True):
    return subprocess.run(args, check=check, capture_output=capture, text=True)


def have(prog: str) -> bool:
    return shutil.which(prog) is not None


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        v = input(f"{prompt}{suffix}: ").strip()
        if v:
            return v
        if default is not None:
            return default


def ask_yn(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        v = input(f"{prompt}{suffix}: ").strip().lower()
        if not v:
            return default
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False


def checklist(items, preselected=None):
    """items: list of (key, label). preselected: iterable of keys.
    Returns selected keys (list)."""
    preselected = set(preselected or [])
    state = [k in preselected for k, _ in items]
    while True:
        print()
        for i, ((_, label), on) in enumerate(zip(items, state)):
            mark = "[x]" if on else "[ ]"
            print(f"  {i + 1:>2}. {mark} {label}")
        n = len(state)
        choice = input(
            f"Press Enter to accept, or numbers 1-{n} to toggle "
            "(comma-separated), 'all', 'none': "
        ).strip().lower()
        if not choice:
            return [k for (k, _), on in zip(items, state) if on]
        if choice == "all":
            if all(state):
                print("    (already all selected; press Enter to confirm)")
            else:
                state = [True] * len(state)
            continue
        if choice == "none":
            if not any(state):
                print("    (already none selected; press Enter to confirm)")
            else:
                state = [False] * len(state)
            continue
        try:
            indices = [int(c.strip()) - 1 for c in choice.split(",")]
        except ValueError:
            print("    (could not parse; try '1,3,5', 'all', 'none', or Enter)")
            continue
        bad = [i + 1 for i in indices if not (0 <= i < len(state))]
        if bad:
            print(f"    (out of range: {','.join(map(str, bad))}; valid is 1..{len(state)})")
            continue
        for idx in indices:
            state[idx] = not state[idx]


# ---- config IO ----

def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
            data.setdefault("projects", [])
            data.setdefault("settings", {"poll_interval_seconds": 60, "show": "per_source"})
            return data
    return {"projects": [], "settings": {"poll_interval_seconds": 60, "show": "per_source"}}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    print(f"  -> wrote {CONFIG_PATH}")


def load_gcp_map() -> dict:
    if not GCP_MAP_PATH.exists():
        print(f"  ! GCP product map not found at {GCP_MAP_PATH}", file=sys.stderr)
        print("    The repo ships this file; check your checkout.", file=sys.stderr)
        sys.exit(1)
    try:
        with open(GCP_MAP_PATH) as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        print(f"  ! could not parse {GCP_MAP_PATH}: {e}", file=sys.stderr)
        sys.exit(1)


def upsert_project(cfg: dict, name: str, type_: str, config: dict) -> None:
    for i, p in enumerate(cfg.get("projects", [])):
        if p.get("name") == name:
            cfg["projects"][i] = {"name": name, "type": type_, "config": config}
            return
    cfg.setdefault("projects", []).append({"name": name, "type": type_, "config": config})


# ---- GCP discovery ----

def gcp_check_auth() -> str | None:
    if not have("gcloud"):
        return None
    try:
        r = _run(["gcloud", "auth", "list",
                 "--filter=status:ACTIVE", "--format=value(account)"], check=False)
        accounts = [a for a in r.stdout.strip().splitlines() if a]
        return accounts[0] if accounts else None
    except FileNotFoundError:
        return None


def gcp_enabled_apis(project: str) -> list[str]:
    r = _run(["gcloud", "services", "list", "--enabled",
             f"--project={project}", "--format=value(config.name)"])
    return [a for a in r.stdout.strip().splitlines() if a]


def gcp_detect_regions(project: str, watched_titles: set[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}

    def stash(title: str, regs: list[str]) -> None:
        regs = sorted({r for r in regs if r})
        if regs:
            out[title] = regs

    if "Cloud Run" in watched_titles:
        r = _run(["gcloud", "run", "services", "list",
                 f"--project={project}", "--format=value(region)"], check=False)
        stash("Cloud Run", r.stdout.strip().splitlines())

    if "Google Cloud SQL" in watched_titles:
        r = _run(["gcloud", "sql", "instances", "list",
                 f"--project={project}", "--format=value(region)"], check=False)
        stash("Google Cloud SQL", r.stdout.strip().splitlines())

    if "Google Compute Engine" in watched_titles:
        r = _run(["gcloud", "compute", "instances", "list",
                 f"--project={project}", "--format=value(zone)"], check=False)
        zones = [z for z in r.stdout.strip().splitlines() if z]
        regs = ["-".join(z.split("-")[:-1]) for z in zones]
        stash("Google Compute Engine", regs)

    if "Google Kubernetes Engine" in watched_titles:
        r = _run(["gcloud", "container", "clusters", "list",
                 f"--project={project}", "--format=value(location)"], check=False)
        stash("Google Kubernetes Engine", r.stdout.strip().splitlines())

    if "Google Cloud Storage" in watched_titles:
        r = _run(["gcloud", "storage", "buckets", "list",
                 f"--project={project}", "--format=value(location)"], check=False)
        stash("Google Cloud Storage",
              [loc.lower() for loc in r.stdout.strip().splitlines()])

    return out


def setup_gcp_project(project_id: str) -> dict | None:
    print(f"\n--- GCP project: {project_id} ---")
    acct = gcp_check_auth()
    if not acct:
        print("  ! gcloud not authenticated. Run: gcloud auth login")
        return None
    print(f"  auth ok ({acct})")

    print("  Listing enabled APIs...")
    try:
        apis = gcp_enabled_apis(project_id)
    except subprocess.CalledProcessError as e:
        print(f"  ! gcloud failed: {e.stderr or e}")
        return None
    print(f"  found {len(apis)} enabled APIs")

    gmap = load_gcp_map()
    detected: dict[str, list[str]] = {}
    unmapped: list[str] = []
    for api in apis:
        title = gmap.get(api)
        if title:
            detected.setdefault(title, []).append(api)
        else:
            unmapped.append(api)
    print(f"  mapped to {len(detected)} status-page products"
          f" ({len(unmapped)} APIs unmapped)")

    if not detected:
        print("  ! no status-page products detected. Skipping.")
        return None

    titles = sorted(detected.keys())
    items = [(t, f"{t}  ({', '.join(sorted(detected[t]))})") for t in titles]
    chosen = checklist(items, preselected=titles)
    if not chosen:
        print("  ! nothing selected. Skipping.")
        return None

    regions_map: dict[str, list[str]] = {}
    if ask_yn("  Auto-detect regions for selected products?", default=True):
        regions_map = gcp_detect_regions(project_id, set(chosen))
        for t in chosen:
            regs = regions_map.get(t)
            if regs:
                print(f"    {t}: {', '.join(regs)}")
            else:
                print(f"    {t}: (no regions detected; will match any)")

    services = []
    for t in chosen:
        regs = regions_map.get(t, [])
        services.append({"name": t, "regions": regs} if regs else {"name": t})

    return {"project_id": project_id, "services": services}


# ---- GitHub discovery ----

def github_check_auth() -> str | None:
    if not have("gh"):
        return None
    r = _run(["gh", "api", "user", "--jq", ".login"], check=False)
    if r.returncode != 0:
        return None
    user = r.stdout.strip()
    return user or None


def gh_api(path: str, *, verbose: bool = False):
    """Returns the parsed JSON, or None on any failure. When `verbose` is
    True, prints `gh`'s stderr so the user can see why the call was
    rejected (404 vs 403 vs rate-limit) rather than getting a silent
    `note: skipped`."""
    r = _run(["gh", "api", path], check=False)
    if r.returncode != 0:
        if verbose:
            err = (r.stderr or "").strip()
            if err:
                print(f"    gh api {path} failed: {err}")
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        if verbose:
            print(f"    gh api {path}: response was not JSON")
        return None


def github_token_scopes() -> str | None:
    """Returns the scopes line from `gh auth status`, or None if unavailable.
    Fine-grained PATs report no classic scopes; we surface that as 'fine-grained'."""
    if not have("gh"):
        return None
    r = _run(["gh", "auth", "status"], check=False)
    text = (r.stdout or "") + (r.stderr or "")
    for line in text.splitlines():
        s = line.strip()
        if "Token scopes" in s:
            after = s.split(":", 1)[1].strip() if ":" in s else ""
            cleaned = after.strip("'\"").lower()
            if not after or cleaned in ("none", ""):
                return "fine-grained or none"
            return after
    return None


def setup_github_repo(repo: str) -> dict | None:
    print(f"\n--- GitHub repo: {repo} ---")
    user = github_check_auth()
    if not user:
        print("  ! gh not authenticated. Run: gh auth login")
        return None
    print(f"  auth ok ({user})")

    info = gh_api(f"repos/{repo}")
    if not info:
        print(f"  ! could not access {repo}. Check spelling and permissions.")
        return None
    print(f"  repo ok (private={info.get('private')}, "
          f"default branch={info.get('default_branch')})")

    detected = {"Git Operations", "API Requests", "Pull Requests"}
    if info.get("has_issues"):
        detected.add("Issues")
    if info.get("has_pages"):
        detected.add("Pages")

    workflows = gh_api(f"repos/{repo}/actions/workflows", verbose=True)
    if workflows is None:
        print("  note: actions autodetect skipped (token may lack 'Actions: Read').")
    elif isinstance(workflows, dict) and workflows.get("total_count", 0) > 0:
        detected.add("Actions")
    hooks = gh_api(f"repos/{repo}/hooks", verbose=True)
    if hooks is None:
        print("  note: webhook autodetect skipped (token may lack 'Webhooks: Read').")
    elif isinstance(hooks, list) and len(hooks) > 0:
        detected.add("Webhooks")

    items = [(c, c + ("  *detected*" if c in detected else "")) for c in GITHUB_COMPONENTS]
    print("  Pick GitHub status components to monitor:")
    chosen = checklist(items, preselected=detected)
    if not chosen:
        print("  ! nothing selected. Skipping.")
        return None
    return {"repo": repo, "components": chosen}


# ---- Anthropic ----

def setup_anthropic() -> dict | None:
    print("\n--- Anthropic ---")
    items = [(k, f"{k}  -  {desc}") for k, desc in ANTHROPIC_COMPONENT_OPTIONS]
    chosen = checklist(items, preselected=ANTHROPIC_DEFAULTS)
    return {"components": chosen} if chosen else None


# ---- subcommands ----

def cmd_setup(_args) -> None:
    print("status-monitor setup")
    print("=" * 60)
    cfg = load_config()

    print("\nWhich sources do you want to configure?")
    sources = checklist(
        [("gcp", "GCP project(s)"),
         ("github", "GitHub repo(s)"),
         ("anthropic", "Anthropic / Claude")],
        preselected=["gcp", "github", "anthropic"],
    )

    if "gcp" in sources:
        while True:
            pid = ask("GCP project ID (blank to stop)").strip()
            if not pid:
                break
            entry = setup_gcp_project(pid)
            if entry:
                upsert_project(cfg, f"gcp:{pid}", "gcp", entry)
            if not ask_yn("  Add another GCP project?", default=False):
                break

    if "github" in sources:
        while True:
            repo = ask("GitHub repo (owner/name; blank to stop)").strip()
            if not repo:
                break
            if "/" not in repo:
                print("  ! expected owner/name. Skipping.")
                continue
            entry = setup_github_repo(repo)
            if entry:
                upsert_project(cfg, f"github:{repo}", "github", entry)
            if not ask_yn("  Add another repo?", default=False):
                break

    if "anthropic" in sources:
        entry = setup_anthropic()
        if entry:
            upsert_project(cfg, "anthropic", "anthropic", entry)

    save_config(cfg)
    print("\nDone. Start the daemon with:")
    print(f"  python3 {ROOT / 'daemon.py'}")
    print("Or enable autostart with: ./install_service.sh")


def cmd_add(args) -> None:
    cfg = load_config()
    if args.kind == "gcp":
        if not args.target:
            print("usage: setup_cli.py add gcp <project-id>"); return
        entry = setup_gcp_project(args.target)
        if entry:
            upsert_project(cfg, f"gcp:{args.target}", "gcp", entry)
    elif args.kind == "github":
        if not args.target or "/" not in args.target:
            print("usage: setup_cli.py add github <owner/repo>"); return
        entry = setup_github_repo(args.target)
        if entry:
            upsert_project(cfg, f"github:{args.target}", "github", entry)
    elif args.kind == "anthropic":
        entry = setup_anthropic()
        if entry:
            upsert_project(cfg, "anthropic", "anthropic", entry)
    save_config(cfg)


def cmd_remove(args) -> None:
    cfg = load_config()
    name = "anthropic" if args.kind == "anthropic" else f"{args.kind}:{args.target}"
    before = len(cfg.get("projects", []))
    cfg["projects"] = [p for p in cfg.get("projects", []) if p.get("name") != name]
    save_config(cfg)
    print(f"removed {before - len(cfg['projects'])} entries")


def cmd_list(_args) -> None:
    cfg = load_config()
    if not cfg.get("projects"):
        print("(no projects configured)")
        return
    for p in cfg["projects"]:
        print(f"- {p['name']}  ({p['type']})")
        c = p.get("config", {})
        if p["type"] == "gcp":
            for s in c.get("services", []):
                regs = ", ".join(s.get("regions", [])) or "any"
                print(f"    {s['name']:48s}  regions: {regs}")
        elif p["type"] == "github":
            print(f"    components: {', '.join(c.get('components', []))}")
        elif p["type"] == "anthropic":
            print(f"    components: {', '.join(c.get('components', []))}")


def cmd_refresh(_args) -> None:
    cfg = load_config()
    new_projects = []
    for p in cfg.get("projects", []):
        if p["type"] == "gcp":
            pid = p["config"]["project_id"]
            print(f"\nrefreshing gcp:{pid}")
            entry = setup_gcp_project(pid)
            new_projects.append(
                {"name": p["name"], "type": "gcp", "config": entry} if entry else p
            )
        elif p["type"] == "github":
            repo = p["config"]["repo"]
            print(f"\nrefreshing github:{repo}")
            entry = setup_github_repo(repo)
            new_projects.append(
                {"name": p["name"], "type": "github", "config": entry} if entry else p
            )
        else:
            new_projects.append(p)
    cfg["projects"] = new_projects
    save_config(cfg)


def cmd_doctor(_args) -> None:
    print("status-monitor doctor")
    print("-" * 50)
    for prog in ("gcloud", "gh", "python3", "notify-send", "xdg-open"):
        print(f"  {prog:14s} {'ok' if have(prog) else 'MISSING'}")
    try:
        import gi  # noqa: F401
        print(f"  python3-gi     ok")
    except ImportError:
        print(f"  python3-gi     MISSING")
    print(f"  config:        {'present' if CONFIG_PATH.exists() else 'MISSING'} ({CONFIG_PATH})")
    print(f"  gcp map:       {'present' if GCP_MAP_PATH.exists() else 'MISSING'}")
    print(f"  gcloud auth:   {gcp_check_auth() or 'NOT AUTHED'}")
    print(f"  gh auth:       {github_check_auth() or 'NOT AUTHED'}")
    print(f"  gh scopes:     {github_token_scopes() or 'unknown'}")


def main() -> None:
    p = argparse.ArgumentParser(prog="setup_cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="interactive wizard")

    p_add = sub.add_parser("add", help="add a project/repo")
    p_add.add_argument("kind", choices=["gcp", "github", "anthropic"])
    p_add.add_argument("target", nargs="?", default="")

    p_rm = sub.add_parser("remove", help="remove a project/repo")
    p_rm.add_argument("kind", choices=["gcp", "github", "anthropic"])
    p_rm.add_argument("target", nargs="?", default="")

    sub.add_parser("list", help="show current config")
    sub.add_parser("refresh", help="re-detect everything")
    sub.add_parser("doctor", help="check tools and auth")

    args = p.parse_args()
    handlers = {
        "setup": cmd_setup, "add": cmd_add, "remove": cmd_remove,
        "list": cmd_list, "refresh": cmd_refresh, "doctor": cmd_doctor,
    }
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
