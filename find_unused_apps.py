#!/usr/bin/env python3
"""
find_unused_apps.py - Unused Application Auditor for Linux (Pop!_OS / Ubuntu / Debian / Flatpak)

Audits installed applications and identifies unused, dormant, or redundant apps by inspecting:
  - Freedesktop Desktop entries and launch binaries
  - Flatpak data timestamps (~/.var/app/<app-id>)
  - User configuration & cache directories (~/.config, ~/.cache, ~/.local/share, ~/.<app>)
  - Document history (~/.local/share/recently-used.xbel)
  - Running processes
  - Shell command history (~/.bash_history, ~/.zsh_history)
  - Package metadata and installed size (dpkg / flatpak)
  - Distinguishes Safe-to-Remove User Apps from Core System / Desktop dependencies
"""

import os
import sys
import glob
import subprocess
import argparse
import json
import re
import shutil
from datetime import datetime

# Color styling for CLI
class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'

    @classmethod
    def disable(cls):
        cls.RED = cls.GREEN = cls.YELLOW = cls.BLUE = ''
        cls.MAGENTA = cls.CYAN = cls.BOLD = cls.DIM = cls.RESET = ''

# Core desktop packages that should NEVER be removed
CRITICAL_CORE_PACKAGES = {
    'cosmic-session', 'cosmic-comp', 'cosmic-files', 'cosmic-term',
    'cosmic-edit', 'cosmic-settings', 'cosmic-applets', 'cosmic-panel',
    'cosmic-store', 'cosmic-launcher', 'cosmic-workspaces', 'cosmic-greeter',
    'cosmic-viewer', 'cosmic-screenshot', 'cosmic-player',
    'pop-desktop', 'ubuntu-desktop', 'system76-driver', 'system76-driver-nvidia',
    'gnome-control-center', 'gdm3', 'xwayland', 'pipewire', 'wireplumber',
    'nvidia-settings', 'popsicle-gtk', 'system-config-printer',
    'gnome-startup-applications', 'gnome-online-accounts-gtk', 'language-selector-gnome',
    'hwloc', 'imagemagick-6.q16', 'gucharmap', 'seahorse', 'vim-common'
}

CORE_SYSTEM_FILE_PATTERNS = [
    r'^com\.system76\.CosmicSettings',
    r'^com\.system76\.CosmicApplet',
    r'^com\.system76\.CosmicPanel',
    r'^com\.system76\.CosmicLauncher',
    r'^com\.system76\.CosmicWorkspaces',
    r'^com\.system76\.CosmicInitialSetup',
    r'^xdg-desktop-portal',
    r'^mimeinfo\.cache',
    r'^org\.freedesktop\.',
    r'^kcm_',
    r'^org\.kde\.kded',
    r'^org\.kde\.kwalletd',
    r'^info\.desktop$',
    r'^im-config\.desktop$',
    r'^ibus-setup',
]

def is_core_desktop_file(filename):
    base = os.path.basename(filename)
    for pat in CORE_SYSTEM_FILE_PATTERNS:
        if re.search(pat, base, re.IGNORECASE):
            return True
    return False

def get_system_dependencies():
    """Returns set of package names that are dependencies or recommends of the desktop metapackage."""
    sys_pkgs = set()
    for meta in ['pop-desktop', 'pop-de-cosmic', 'ubuntu-desktop']:
        try:
            out = subprocess.check_output(['apt-cache', 'depends', meta], text=True, stderr=subprocess.DEVNULL)
            for line in out.splitlines():
                if 'Depends:' in line or 'Recommends:' in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        p = parts[1].strip('<>')
                        sys_pkgs.add(p)
        except Exception:
            pass
    return sys_pkgs

def parse_desktop_file(filepath):
    data = {}
    current_section = None
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('[') and line.endswith(']'):
                    current_section = line[1:-1]
                    continue
                if current_section == 'Desktop Entry' and '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip()
                    val = val.strip()
                    if key not in data:
                        data[key] = val
    except Exception:
        return None
    return data

def get_directory_size_and_mtime(path, max_depth=2, max_files=100):
    if not os.path.exists(path):
        return 0, None
    try:
        if os.path.isfile(path) or os.path.islink(path):
            stat = os.stat(path)
            return stat.st_size, stat.st_mtime

        total_size = 0
        newest_mtime = os.path.getmtime(path)
        base_depth = path.rstrip(os.path.sep).count(os.path.sep)
        files_checked = 0

        for root, dirs, files in os.walk(path):
            current_depth = root.count(os.path.sep) - base_depth
            if current_depth > max_depth:
                del dirs[:]
                continue
            for d in dirs:
                try:
                    mt = os.path.getmtime(os.path.join(root, d))
                    if mt > newest_mtime:
                        newest_mtime = mt
                except Exception:
                    pass
            for f in files:
                fp = os.path.join(root, f)
                try:
                    st = os.stat(fp)
                    total_size += st.st_size
                    if st.st_mtime > newest_mtime:
                        newest_mtime = st.st_mtime
                except Exception:
                    pass
                files_checked += 1
                if files_checked >= max_files:
                    break
            if files_checked >= max_files:
                break
        return total_size, newest_mtime
    except Exception:
        return 0, None

def load_recently_used(user_home):
    recently_used = {}
    xbel = os.path.join(user_home, '.local', 'share', 'recently-used.xbel')
    if os.path.exists(xbel):
        import xml.etree.ElementTree as ET
        try:
            tree = ET.parse(xbel)
            root = tree.getroot()
            for app in root.findall('.//{http://www.freedesktop.org/standards/desktop-bookmarks}application'):
                name = app.attrib.get('name', '').strip().lower()
                mod = app.attrib.get('modified')
                if name and mod:
                    dt = datetime.fromisoformat(mod.replace('Z', '+00:00')).replace(tzinfo=None)
                    if name not in recently_used or dt > recently_used[name]:
                        recently_used[name] = dt
        except Exception:
            pass
    return recently_used

def get_running_processes():
    procs = set()
    try:
        out = subprocess.check_output(['ps', '-eo', 'comm'], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines()[1:]:
            p = line.strip().lower()
            if p:
                procs.add(p)
    except Exception:
        pass
    return procs

def load_shell_history(user_home):
    hist_commands = {}
    for hname in ['.bash_history', '.zsh_history']:
        hpath = os.path.join(user_home, hname)
        if os.path.exists(hpath):
            try:
                with open(hpath, 'r', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            tokens = line.split()
                            if tokens:
                                cmd = os.path.basename(tokens[0]).lower()
                                hist_commands[cmd] = hist_commands.get(cmd, 0) + 1
            except Exception:
                pass
    return hist_commands

_PKG_CACHE = {}
def get_dpkg_package_for_file(filepath):
    if filepath in _PKG_CACHE:
        return _PKG_CACHE[filepath]
    try:
        out = subprocess.check_output(['dpkg', '-S', filepath], text=True, stderr=subprocess.DEVNULL)
        pkg = out.split(':')[0].strip()
        _PKG_CACHE[filepath] = pkg
        return pkg
    except Exception:
        _PKG_CACHE[filepath] = None
        return None

_DPKG_INFO_CACHE = {}
def get_dpkg_info(pkg_name):
    if pkg_name in _DPKG_INFO_CACHE:
        return _DPKG_INFO_CACHE[pkg_name]
    size_bytes = 0
    install_mtime = None
    try:
        out = subprocess.check_output(['dpkg', '-s', pkg_name], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if line.startswith('Installed-Size:'):
                size_kb = int(line.split(':')[1].strip())
                size_bytes = size_kb * 1024
                break
    except Exception:
        pass

    info_lists = glob.glob(f'/var/lib/dpkg/info/{pkg_name}*.list')
    if info_lists:
        try:
            install_mtime = os.path.getmtime(info_lists[0])
        except Exception:
            pass
    _DPKG_INFO_CACHE[pkg_name] = (size_bytes, install_mtime)
    return size_bytes, install_mtime

_FLATPAK_INFO_CACHE = {}
def get_flatpak_info(app_id):
    if app_id in _FLATPAK_INFO_CACHE:
        return _FLATPAK_INFO_CACHE[app_id]
    size_bytes = 0
    try:
        out = subprocess.check_output(['flatpak', 'info', app_id], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if 'Installed:' in line or 'Installed size:' in line or 'Size:' in line:
                m = re.search(r'([\d\.]+)\s*(GB|MB|KB|B)', line, re.IGNORECASE)
                if m:
                    val = float(m.group(1))
                    unit = m.group(2).upper()
                    mult = {'GB': 1024**3, 'MB': 1024**2, 'KB': 1024, 'B': 1}.get(unit, 1)
                    size_bytes = int(val * mult)
                    break
    except Exception:
        pass
    _FLATPAK_INFO_CACHE[app_id] = size_bytes
    return size_bytes

def format_size(bytes_val):
    if not bytes_val or bytes_val <= 0:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.1f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.1f} PB"

def audit_apps(inactivity_days=60, user_home=None):
    if not user_home:
        user_home = os.path.expanduser('~')

    now = datetime.now()
    running_procs = get_running_processes()
    recently_used = load_recently_used(user_home)
    shell_history = load_shell_history(user_home)
    system_dependencies = get_system_dependencies()

    desktop_paths = [
        '/usr/share/applications/*.desktop',
        os.path.join(user_home, '.local', 'share', 'applications', '*.desktop'),
        '/var/lib/flatpak/exports/share/applications/*.desktop',
        os.path.join(user_home, '.local', 'share', 'flatpak', 'exports', 'share', 'applications', '*.desktop'),
    ]

    desktop_files = set()
    for dp in desktop_paths:
        desktop_files.update(glob.glob(dp))

    analyzed = []
    seen_ids = set()

    for df in sorted(desktop_files):
        if is_core_desktop_file(df):
            continue

        entry = parse_desktop_file(df)
        if not entry:
            continue
        if entry.get('Type') != 'Application':
            continue
        if entry.get('NoDisplay', 'false').lower() == 'true':
            continue

        app_name = entry.get('Name', os.path.basename(df).removesuffix('.desktop'))
        exec_cmd = entry.get('Exec', '')
        categories = entry.get('Categories', '')
        is_flatpak = 'flatpak' in df or '/exports/' in df

        flatpak_id = None
        if is_flatpak:
            base_fname = os.path.basename(df)
            flatpak_id = base_fname.removesuffix('.desktop')

        # Prevent duplicate entries for the exact same package/flatpak
        unique_key = (flatpak_id if is_flatpak else (exec_cmd.split()[0] if exec_cmd else app_name))
        if unique_key in seen_ids:
            continue
        seen_ids.add(unique_key)

        # Parse command tokens for checking
        exec_token = ''
        if exec_cmd:
            parts = exec_cmd.split()
            exec_token = os.path.basename(parts[0]).lower()
            if exec_token in ['env', 'pkexec', 'flatpak', 'python3', 'python'] and len(parts) > 1:
                for p in parts[1:]:
                    if not p.startswith('-') and '=' not in p:
                        exec_token = os.path.basename(p).lower()
                        break

        # Check package identity & system dependency status
        deb_pkg = None
        is_core_system = False
        is_system_dep = False

        if is_flatpak:
            is_system_dep = False
        else:
            deb_pkg = get_dpkg_package_for_file(df)
            if not deb_pkg and exec_token:
                which_cmd = shutil.which(exec_token)
                if which_cmd:
                    deb_pkg = get_dpkg_package_for_file(which_cmd)

            if deb_pkg and (deb_pkg in CRITICAL_CORE_PACKAGES or deb_pkg.startswith('cosmic-')):
                is_core_system = True
            elif 'COSMIC' in categories and ('cosmic' in app_name.lower() or 'system76' in df):
                is_core_system = True
            
            if deb_pkg and (deb_pkg in system_dependencies or is_core_system):
                is_system_dep = True

        # Check activity evidence
        activity_timestamps = []

        # 1. Is it currently running?
        is_running = False
        if exec_token and exec_token in running_procs:
            is_running = True
        elif app_name.lower() in running_procs:
            is_running = True
        elif flatpak_id and any(flatpak_id.lower() in p for p in running_procs):
            is_running = True

        if is_running:
            activity_timestamps.append((now, 'Process is currently running'))

        # 2. Flatpak user data check
        user_data_size = 0
        if is_flatpak and flatpak_id:
            var_dir = os.path.join(user_home, '.var', 'app', flatpak_id)
            if os.path.exists(var_dir):
                dsize, dmtime = get_directory_size_and_mtime(var_dir, max_depth=3)
                user_data_size += dsize
                if dmtime:
                    activity_timestamps.append((datetime.fromtimestamp(dmtime), f'Flatpak user data: ~/.var/app/{flatpak_id}'))

        # 3. Document history in recently-used.xbel
        app_name_lower = app_name.lower()
        for xname, xdate in recently_used.items():
            if xname in app_name_lower or (exec_token and (xname in exec_token or exec_token in xname)):
                activity_timestamps.append((xdate, f'Document history ({xname})'))

        # 4. Config, Cache, and Local Share directories
        search_keys = set()
        if exec_token and len(exec_token) >= 3:
            search_keys.add(exec_token)
        clean_name = re.sub(r'[^a-zA-Z0-9]', '', app_name.lower())
        if len(clean_name) >= 3:
            search_keys.add(clean_name)
        if flatpak_id:
            last_part = flatpak_id.split('.')[-1].lower()
            if len(last_part) >= 3:
                search_keys.add(last_part)

        for base_folder in ['.config', '.cache', os.path.join('.local', 'share')]:
            target_base = os.path.join(user_home, base_folder)
            if not os.path.exists(target_base):
                continue
            try:
                for entry_name in os.listdir(target_base):
                    ent_low = entry_name.lower()
                    for sk in search_keys:
                        if ent_low == sk or ent_low == f'{sk}.conf' or ent_low == f'{sk}-common' or ent_low.startswith(f'{sk}.'):
                            fpath = os.path.join(target_base, entry_name)
                            dsize, dmtime = get_directory_size_and_mtime(fpath, max_depth=2)
                            user_data_size += dsize
                            if dmtime:
                                activity_timestamps.append((datetime.fromtimestamp(dmtime), f'Config/Cache: ~/{base_folder}/{entry_name}'))
            except Exception:
                pass

        # 5. Direct dot-directories in home (e.g., ~/.anydesk, ~/.psensor, ~/.wireshark)
        for sk in search_keys:
            dot_path = os.path.join(user_home, f'.{sk}')
            if os.path.exists(dot_path) and dot_path != user_home:
                dsize, dmtime = get_directory_size_and_mtime(dot_path, max_depth=2)
                user_data_size += dsize
                if dmtime:
                    activity_timestamps.append((datetime.fromtimestamp(dmtime), f'Home directory: ~/.{sk}'))

        # Determine package size and install info
        app_size = 0
        uninstall_cmd = ""

        if is_flatpak:
            app_size = get_flatpak_info(flatpak_id)
            uninstall_cmd = f"flatpak uninstall --delete-data -y {flatpak_id}"
        else:
            if deb_pkg:
                dsize, dmtime = get_dpkg_info(deb_pkg)
                app_size = dsize
                if not is_core_system:
                    uninstall_cmd = f"sudo apt remove --purge -y {deb_pkg}"
            else:
                uninstall_cmd = f"rm -f '{df}'"

        total_disk_footprint = app_size + user_data_size

        # Find latest activity
        last_active = None
        evidence_reason = "No user activity recorded"
        if activity_timestamps:
            activity_timestamps.sort(key=lambda x: x[0], reverse=True)
            last_active = activity_timestamps[0][0]
            evidence_reason = activity_timestamps[0][1]

        days_inactive = (now - last_active).days if last_active else None

        # Determine Status
        if is_running:
            status = 'ACTIVE'
            badge = f"{Colors.GREEN}[RUNNING]{Colors.RESET}"
        elif last_active is None:
            status = 'NEVER_USED'
            badge = f"{Colors.RED}[NEVER USED]{Colors.RESET}"
        elif days_inactive > 180:
            status = 'DORMANT'
            badge = f"{Colors.MAGENTA}[DORMANT >6M]{Colors.RESET}"
        elif days_inactive > inactivity_days:
            status = 'INACTIVE'
            badge = f"{Colors.YELLOW}[INACTIVE]{Colors.RESET}"
        else:
            status = 'ACTIVE'
            badge = f"{Colors.GREEN}[ACTIVE]{Colors.RESET}"

        # Category for safe removal
        if is_flatpak:
            app_category = "User Flatpak"
            is_safe_to_remove = True
        elif is_core_system or is_system_dep:
            app_category = "System Default / Core"
            is_safe_to_remove = False
        else:
            app_category = "User Third-Party"
            is_safe_to_remove = True

        analyzed.append({
            'name': app_name,
            'package': flatpak_id if is_flatpak else (deb_pkg or exec_token),
            'type': 'Flatpak' if is_flatpak else 'APT/deb',
            'category': app_category,
            'is_safe_to_remove': is_safe_to_remove,
            'is_core': is_core_system,
            'status': status,
            'badge': badge,
            'last_active': last_active.strftime('%Y-%m-%d') if last_active else 'Never',
            'days_inactive': days_inactive if days_inactive is not None else 99999,
            'days_display': f"{days_inactive}d ago" if days_inactive is not None else "Never",
            'disk_size_bytes': total_disk_footprint,
            'disk_size_fmt': format_size(total_disk_footprint),
            'evidence': evidence_reason,
            'uninstall_cmd': uninstall_cmd,
            'desktop_file': df,
        })

    return analyzed

def main():
    parser = argparse.ArgumentParser(
        description="Audit installed Linux applications and identify unused, dormant, or redundant apps."
    )
    parser.add_argument(
        '-d', '--days',
        type=int,
        default=60,
        help="Inactivity threshold in days (default: 60 days)"
    )
    parser.add_argument(
        '-a', '--all',
        action='store_true',
        help="Display all installed applications, including active ones"
    )
    parser.add_argument(
        '--system',
        action='store_true',
        help="Also list pre-installed system utilities (default: shows user / third-party apps)"
    )
    parser.add_argument(
        '--sort',
        choices=['days', 'size', 'name'],
        default='days',
        help="Sort results by 'days' (default), 'size', or 'name'"
    )
    parser.add_argument(
        '--clean-commands',
        action='store_true',
        help="Print clean copy-pasteable terminal commands to uninstall the unused apps"
    )
    parser.add_argument(
        '--json',
        action='store_true',
        help="Output results as JSON"
    )
    parser.add_argument(
        '--no-color',
        action='store_true',
        help="Disable ANSI color output"
    )

    args = parser.parse_args()

    if args.no_color or not sys.stdout.isatty():
        Colors.disable()

    all_apps = audit_apps(inactivity_days=args.days)

    # Sort results
    if args.sort == 'days':
        all_apps.sort(key=lambda x: x['days_inactive'], reverse=True)
    elif args.sort == 'size':
        all_apps.sort(key=lambda x: x['disk_size_bytes'], reverse=True)
    elif args.sort == 'name':
        all_apps.sort(key=lambda x: x['name'].lower())

    user_apps = [a for a in all_apps if a['is_safe_to_remove']]
    system_apps = [a for a in all_apps if not a['is_safe_to_remove']]

    if not args.all:
        unused_user_apps = [a for a in user_apps if a['status'] in ('NEVER_USED', 'DORMANT', 'INACTIVE')]
        unused_system_apps = [a for a in system_apps if a['status'] in ('NEVER_USED', 'DORMANT', 'INACTIVE')]
    else:
        unused_user_apps = user_apps
        unused_system_apps = system_apps

    if args.json:
        print(json.dumps({
            'user_apps': unused_user_apps,
            'system_apps': unused_system_apps if args.system else []
        }, indent=2))
        return

    # Check for duplicates (e.g. Chrome flatpak vs Chrome deb)
    app_names_seen = {}
    for a in all_apps:
        app_names_seen.setdefault(a['name'].lower(), []).append(a)
    duplicates = [v for k, v in app_names_seen.items() if len(v) > 1]

    # Calculate reclaimable disk space
    user_reclaimable_bytes = sum(a['disk_size_bytes'] for a in unused_user_apps)

    print()
    print(f"{Colors.BOLD}{Colors.CYAN}========================================================================================{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}                  LINUX APPLICATION USAGE AUDITOR (Pop!_OS / COSMIC)                    {Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}========================================================================================{Colors.RESET}")
    print(f"{Colors.BOLD}Threshold:{Colors.RESET} Inactive for > {args.days} days | {Colors.BOLD}User Apps Audited:{Colors.RESET} {len(user_apps)}")
    print(f"{Colors.BOLD}Unused User Apps Detected:{Colors.RESET} {Colors.YELLOW}{len(unused_user_apps)}{Colors.RESET}")
    print(f"{Colors.BOLD}Reclaimable Space (User Apps):{Colors.RESET} {Colors.GREEN}{format_size(user_reclaimable_bytes)}{Colors.RESET}")
    print()

    # Table Header
    col_status = 15
    col_name = 28
    col_type = 10
    col_last = 14
    col_size = 11

    def print_table(title, app_list):
        print(f"{Colors.BOLD}{Colors.BLUE}--- {title} ({len(app_list)}) ---{Colors.RESET}")
        header = f"{'STATUS':<{col_status}} {'APPLICATION':<{col_name}} {'TYPE':<{col_type}} {'LAST USED':<{col_last}} {'SPACE':<{col_size}} {'EVIDENCE / SOURCE'}"
        print(f"{Colors.BOLD}{header}{Colors.RESET}")
        print("-" * 105)
        for a in app_list:
            print(
                f"{a['badge']:<{col_status + 9}} "
                f"{a['name'][:col_name-1]:<{col_name}} "
                f"{a['type']:<{col_type}} "
                f"{a['last_active']:<{col_last}} "
                f"{a['disk_size_fmt']:<{col_size}} "
                f"{Colors.DIM}{a['evidence'][:45]}{Colors.RESET}"
            )
        print()

    # 1. Print Safe-to-Remove User Applications
    print_table("UNUSED USER & THIRD-PARTY APPLICATIONS (Safe to Remove)", unused_user_apps)

    # 2. Print System Apps if requested
    if args.system and unused_system_apps:
        print_table("UNUSED PRE-INSTALLED SYSTEM UTILITIES (Desktop Dependencies)", unused_system_apps)
    elif unused_system_apps:
        print(f"{Colors.DIM}Note: {len(unused_system_apps)} pre-installed system utilities (e.g. Help, Terminal, Disk Utilities) were also inactive.{Colors.RESET}")
        print(f"{Colors.DIM}Use '{Colors.BOLD}python3 {sys.argv[0]} --system{Colors.RESET}{Colors.DIM}' if you want to inspect system utilities.{Colors.RESET}\n")

    # Redundant / Duplicate Section
    if duplicates:
        print(f"{Colors.BOLD}{Colors.YELLOW}[!] REDUNDANT / DUPLICATE INSTALLATIONS DETECTED:{Colors.RESET}")
        for dup_list in duplicates:
            print(f"  • {Colors.BOLD}{dup_list[0]['name']}{Colors.RESET} is installed in multiple formats:")
            for item in dup_list:
                status_color = Colors.GREEN if item['status'] == 'ACTIVE' else Colors.RED
                print(f"    - [{item['type']}] ({item['package']}): Last used {item['last_active']} {status_color}({item['status']}){Colors.RESET} - {item['disk_size_fmt']}")
        print()

    # Clean Commands Section
    if args.clean_commands and unused_user_apps:
        print(f"{Colors.BOLD}{Colors.CYAN}=== RECOMMENDED CLEANUP COMMANDS (COPY & PASTE) ==={Colors.RESET}")
        flatpaks_to_remove = list(dict.fromkeys([a['package'] for a in unused_user_apps if a['type'] == 'Flatpak']))
        debs_to_remove = list(dict.fromkeys([a['package'] for a in unused_user_apps if a['type'] == 'APT/deb' and a['package']]))

        if flatpaks_to_remove:
            print(f"\n{Colors.BOLD}# 1. Remove unused Flatpaks and clear their user data caches:{Colors.RESET}")
            print(f"flatpak uninstall --delete-data -y {' '.join(flatpaks_to_remove)}")

        if debs_to_remove:
            print(f"\n{Colors.BOLD}# 2. Remove unused APT packages and purge configuration files:{Colors.RESET}")
            print(f"sudo apt remove --purge -y {' '.join(debs_to_remove)}")
            print("sudo apt autoremove --purge -y")
        print()
    elif unused_user_apps:
        print(f"{Colors.DIM}Tip: Run with '{Colors.BOLD}python3 {sys.argv[0]} --clean-commands{Colors.RESET}{Colors.DIM}' to generate exact removal commands.{Colors.RESET}\n")

if __name__ == '__main__':
    main()
