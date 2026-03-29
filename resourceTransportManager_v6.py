#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import traceback
import time
import datetime
import json
import math
import os
import re
import csv
import tempfile

from ikabot.config import *
from ikabot.helpers.botComm import *
from ikabot.helpers.getJson import getCity, getIsland
from ikabot.helpers.gui import *
from ikabot.helpers.pedirInfo import *
from ikabot.helpers.planRoutes import executeRoutes
from ikabot.helpers.process import set_child_mode
from ikabot.helpers.signals import setInfoSignal
from ikabot.helpers.naval import getAvailableShips, getAvailableFreighters
from ikabot.helpers.varios import addThousandSeparator, getDateTime


# ============================================================================
#  BANNER
# ============================================================================

def print_module_banner(page_title=None):
    print("\n")
    print("\u2554" + "\u2550" * 58 + "\u2557")
    print("\u2551            RESOURCE TRANSPORT MANAGER v6                  \u2551")
    print("\u255a" + "\u2550" * 58 + "\u255d")
    if page_title:
        print(f"\n{page_title}")
        print("\u2500" * 58)
    print("")


# ============================================================================
#  NOTIFICATION CONFIG  (replaces overloaded telegram_enabled)
# ============================================================================

def get_notification_config(telegram_enabled, event):
    if telegram_enabled is False:
        print_module_banner()
        print("Telegram notifications are not configured.")
        print("Do you want to continue without notifications? [Y/n]")
        rta = read(values=["y", "Y", "n", "N", ""])
        if rta.lower() == "n":
            event.set()
            return None
        return {"level": "none", "telegram": False}

    print_module_banner("Notification Preferences")
    print("When do you want to receive Telegram notifications?")
    print("(1) Partial - Summary when each cycle starts + errors")
    print("(2) All - Every individual shipment")
    print("(3) None - No notifications")
    print("(') Back to main menu")
    choice = read(min=1, max=3, digit=True, additionalValues=["'"])
    if choice == "'":
        event.set()
        return None
    levels = {1: "partial", 2: "all", 3: "none"}
    return {"level": levels[choice], "telegram": True}


def should_notify(notif_config, event_type):
    if not notif_config or not notif_config.get("telegram"):
        return False
    level = notif_config.get("level", "none")
    if level == "none":
        return event_type == "error"
    if level == "all":
        return True
    if level == "partial":
        return event_type in ("start", "error", "complete")
    return False


# ============================================================================
#  SHIPMENT LOG
# ============================================================================

LOG_COLUMNS = [
    "Date", "Time", "Account", "Mode", "Source_City", "Source_Island",
    "Dest_City", "Dest_Island", "Dest_Player",
    "Wood", "Wine", "Marble", "Crystal", "Sulphur", "Total_Resources",
    "Ships_Used", "Ship_Type", "Status", "Error", "Next_Shipment",
]

PREFS_FILE = os.path.join(os.path.expanduser("~"), ".ikabot_rtm_prefs.json")


def load_prefs():
    """Load saved preferences (log path, CSV path, etc.)."""
    try:
        if os.path.isfile(PREFS_FILE):
            with open(PREFS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_prefs(prefs):
    """Save preferences to disk."""
    try:
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except Exception:
        pass


def get_log_path(session):
    """Get path to the shared shipment log file.
    Remembers the last-used path so you can just press Enter next time.
    """
    prefs = load_prefs()
    saved = prefs.get("log_path", "")
    fallback = os.path.join(os.path.expanduser("~"), "shipment_log.csv")
    default_path = saved if saved else fallback

    if saved:
        # Already have a saved path — use it silently
        print(f"  Shipment log: {default_path}")
        return default_path
    # First time — ask the user
    print(f"Shipment log file (Enter for default):")
    print(f"  Default: {default_path}")
    print(f"  (All accounts share one file — each row has an Account column)")
    user_path = read(msg="Log path: ", empty=True)
    chosen = user_path.strip() if user_path.strip() else default_path
    prefs["log_path"] = chosen
    save_prefs(prefs)
    return chosen


def log_shipment(log_path, session, mode, source_city, source_island,
                 dest_city, dest_island, dest_player, resources,
                 ships_used, ship_type, status, error_msg=None,
                 next_shipment=None):
    if not log_path:
        return
    try:
        file_exists = os.path.isfile(log_path)
        now = datetime.datetime.now()
        row = {
            "Date": now.strftime("%Y-%m-%d"),
            "Time": now.strftime("%H:%M:%S"),
            "Account": session.username,
            "Mode": mode,
            "Source_City": source_city,
            "Source_Island": source_island,
            "Dest_City": dest_city,
            "Dest_Island": dest_island,
            "Dest_Player": dest_player,
            "Wood": resources[0] if len(resources) > 0 else 0,
            "Wine": resources[1] if len(resources) > 1 else 0,
            "Marble": resources[2] if len(resources) > 2 else 0,
            "Crystal": resources[3] if len(resources) > 3 else 0,
            "Sulphur": resources[4] if len(resources) > 4 else 0,
            "Total_Resources": sum(resources),
            "Ships_Used": ships_used,
            "Ship_Type": ship_type,
            "Status": status,
            "Error": error_msg or "",
            "Next_Shipment": next_shipment or "",
        }
        with open(log_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
    except Exception:
        pass  # never crash the main script for a logging failure


# ============================================================================
#  LOCK FILE  (atomic create — fixes race condition)
# ============================================================================

def get_lock_file_path(session, use_freighters=False):
    ship_type = "freighters" if use_freighters else "merchant_ships"
    safe_server = session.servidor.replace("/", "_").replace("\\", "_")
    safe_username = session.username.replace("/", "_").replace("\\", "_")
    lock_filename = f".ikabot_shared_{ship_type}_{safe_server}_{safe_username}.lock"
    return os.path.join(os.path.expanduser("~"), lock_filename)


def acquire_shipping_lock(session, use_freighters=False, timeout=300):
    lock_file = get_lock_file_path(session, use_freighters)
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                lock_data = json.dumps({
                    "pid": os.getpid(),
                    "timestamp": time.time(),
                    "ship_type": "freighters" if use_freighters else "merchant_ships",
                    "server": session.servidor,
                    "username": session.username,
                })
                os.write(fd, lock_data.encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                with open(lock_file, "r") as f:
                    lock_data = json.load(f)
                    if time.time() - lock_data.get("timestamp", 0) > 600:
                        os.remove(lock_file)
                        continue
            except (json.JSONDecodeError, KeyError, IOError):
                try:
                    os.remove(lock_file)
                except Exception:
                    pass
                continue
        except Exception:
            pass
        time.sleep(5)
    return False


def release_shipping_lock(session, use_freighters=False):
    lock_file = get_lock_file_path(session, use_freighters)
    try:
        if os.path.exists(lock_file):
            try:
                with open(lock_file, "r") as f:
                    lock_data = json.load(f)
                    if lock_data.get("pid") == os.getpid():
                        os.remove(lock_file)
            except Exception:
                try:
                    os.remove(lock_file)
                except Exception:
                    pass
    except Exception:
        pass


# ============================================================================
#  SHIP HELPERS
# ============================================================================

def getShipCapacity(session):
    try:
        html = session.post("view=merchantNavy")
        ship_capacity = int(
            html.split('singleTransporterCapacity":')[1]
            .split(',"singleFreighterCapacity')[0]
        )
        freighter_capacity = int(
            html.split('singleFreighterCapacity":')[1]
            .split(',"draftEffect')[0]
        )
        return ship_capacity, freighter_capacity
    except Exception:
        raise Exception(
            "Could not read ship capacity from game server — "
            "the response format may have changed"
        )


def wait_for_ships(session, useFreighters, status_prefix="", max_wait=3600):
    ship_type = "freighters" if useFreighters else "merchant ships"
    start = time.time()
    while True:
        available = (
            getAvailableFreighters(session) if useFreighters
            else getAvailableShips(session)
        )
        if available > 0:
            session.setStatus(
                f"{status_prefix}Found {available} {ship_type}"
            )
            return available
        elapsed = int(time.time() - start)
        if elapsed > max_wait:
            session.setStatus(
                f"{status_prefix}Timed out waiting for {ship_type} ({elapsed}s)"
            )
            return 0
        session.setStatus(
            f"{status_prefix}Waiting for {ship_type}... ({elapsed}s)"
        )
        time.sleep(120)


# ============================================================================
#  SHARED SEND SHIPMENT  (lock → verify → send → verify → unlock → log)
# ============================================================================

def send_shipment(session, route, useFreighters, notif_config, log_path,
                  mode_name, dest_island_coords="", dest_player="",
                  max_lock_retries=3, next_shipment_str=None):
    origin_city = route[0]
    dest_city = route[1]
    resources = list(route[3:])
    total_cargo = sum(resources)
    ship_type_name = "freighters" if useFreighters else "merchant ships"
    prefix = f"{origin_city['name']} -> {dest_city['name']} | "

    result = {"success": False, "error": None, "ships_used": 0}

    # 1. Wait for ships (with timeout)
    available = wait_for_ships(session, useFreighters, prefix)
    if available == 0:
        result["error"] = f"No {ship_type_name} available (timed out)"
        if should_notify(notif_config, "error"):
            sendToBot(session,
                      f"SHIPMENT SKIPPED\n{prefix}\n{result['error']}")
        log_shipment(log_path, session, mode_name,
                     origin_city["name"], "", dest_city["name"],
                     dest_island_coords, dest_player, resources,
                     0, ship_type_name, "SKIPPED", result["error"],
                     next_shipment_str)
        return result

    # 2. Acquire lock with retries
    lock_acquired = False
    for attempt in range(1, max_lock_retries + 1):
        session.setStatus(
            f"{prefix}Acquiring lock ({attempt}/{max_lock_retries})..."
        )
        if acquire_shipping_lock(session, use_freighters=useFreighters,
                                 timeout=300):
            lock_acquired = True
            break
        if attempt < max_lock_retries:
            if should_notify(notif_config, "error"):
                sendToBot(session,
                          f"Lock attempt {attempt}/{max_lock_retries} "
                          f"failed for {prefix}Retrying in 60s...")
            time.sleep(60)

    if not lock_acquired:
        result["error"] = (
            f"Could not acquire shipping lock after "
            f"{max_lock_retries} attempts"
        )
        if should_notify(notif_config, "error"):
            sendToBot(session,
                      f"SHIPMENT FAILED\n{prefix}\n{result['error']}")
        log_shipment(log_path, session, mode_name,
                     origin_city["name"], "", dest_city["name"],
                     dest_island_coords, dest_player, resources,
                     0, ship_type_name, "FAILED", result["error"],
                     next_shipment_str)
        return result

    # 3. Lock held — ALWAYS release in finally
    try:
        ships_before = (
            getAvailableFreighters(session) if useFreighters
            else getAvailableShips(session)
        )
        if ships_before == 0:
            result["error"] = "Ships became unavailable before sending"
            if should_notify(notif_config, "error"):
                sendToBot(session,
                          f"SHIPMENT DELAYED\n{prefix}\n{result['error']}")
            log_shipment(log_path, session, mode_name,
                         origin_city["name"], "", dest_city["name"],
                         dest_island_coords, dest_player, resources,
                         0, ship_type_name, "DELAYED", result["error"],
                         next_shipment_str)
            return result

        session.setStatus(f"{prefix}Sending resources...")
        executeRoutes(session, [route], useFreighters)

        # If executeRoutes completes without error, the shipment was sent.
        # We do NOT verify by comparing ship counts before/after because
        # ships from earlier shipments can return during sending, making
        # the count unreliable and causing false "failure" reports.
        ship_cap, freighter_cap = getShipCapacity(session)
        capacity = freighter_cap if useFreighters else ship_cap
        ships_needed = math.ceil(total_cargo / capacity) if capacity > 0 else 0

        result["success"] = True
        result["ships_used"] = ships_needed

        res_desc = ", ".join(
            f"{addThousandSeparator(resources[i])} {materials_names[i]}"
            for i in range(len(materials_names))
            if i < len(resources) and resources[i] > 0
        )
        if should_notify(notif_config, "each"):
            sendToBot(session,
                      f"SHIPMENT SENT\nAccount: {session.username}\n"
                      f"From: {origin_city['name']}\n"
                      f"To: {dest_island_coords} {dest_city['name']}\n"
                      f"Ships: {ships_needed} {ship_type_name}\n"
                      f"Sent: {res_desc}")

        log_shipment(log_path, session, mode_name,
                     origin_city["name"], "", dest_city["name"],
                     dest_island_coords, dest_player, resources,
                     ships_needed, ship_type_name, "SENT",
                     next_shipment=next_shipment_str)

    except Exception as e:
        result["error"] = str(e)
        if should_notify(notif_config, "error"):
            sendToBot(session,
                      f"SHIPMENT FAILED\nAccount: {session.username}\n"
                      f"From: {origin_city['name']}\n"
                      f"To: {dest_island_coords} {dest_city['name']}\n"
                      f"Error: {result['error']}")
        log_shipment(log_path, session, mode_name,
                     origin_city["name"], "", dest_city["name"],
                     dest_island_coords, dest_player, resources,
                     0, ship_type_name, "FAILED", result["error"],
                     next_shipment_str)
    finally:
        release_shipping_lock(session, use_freighters=useFreighters)

    return result


# ============================================================================
#  UTILITY FUNCTIONS
# ============================================================================

def normalize_text(value):
    return str(value or "").strip().lower()


def parse_run_column_datetime(column_name):
    if not column_name.startswith("Run_"):
        return None
    try:
        return datetime.datetime.strptime(column_name[4:], "%Y-%m-%d_%H-%M-%S")
    except Exception:
        return None


def build_run_column_name(dt=None):
    if dt is None:
        dt = datetime.datetime.now()
    return f"Run_{dt.strftime('%Y-%m-%d_%H-%M-%S')}"


def write_csv_atomic(csv_path, fieldnames, rows):
    directory = os.path.dirname(os.path.abspath(csv_path)) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_massdist_", suffix=".csv", dir=directory
    )
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as tmp_file:
            writer = csv.DictWriter(tmp_file, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        os.replace(tmp_path, csv_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        raise


def ensure_run_columns(fieldnames, rows, max_slots=7):
    run_columns = [c for c in fieldnames if c.startswith("Run_")]
    missing = max_slots - len(run_columns)
    if missing > 0:
        for i in range(missing):
            old_dt = datetime.datetime(1970, 1, 1, 0, 0, i)
            col = build_run_column_name(old_dt)
            while col in fieldnames:
                old_dt += datetime.timedelta(seconds=1)
                col = build_run_column_name(old_dt)
            fieldnames.append(col)
            run_columns.append(col)
            for row in rows:
                row[col] = ""
    return fieldnames, run_columns


def get_city_location_token(city_data):
    location_keys = [
        "position", "cityLocation", "location", "slot",
        "islandPosition", "citySlot", "index", "nr", "number",
    ]
    for key in location_keys:
        if key in city_data and str(city_data.get(key, "")).strip() != "":
            return str(city_data.get(key)).strip()
    return None


def choose_run_slot(session, event, rows, run_columns):
    print_module_banner("Mass Distribution - Run Slot")
    print("Choose how to start:")
    print("(1) Start fresh (reuses the OLDEST run slot)")
    print("(2) Resume from existing run slot")
    print("(') Back to main menu")
    mode = read(min=1, max=2, digit=True, additionalValues=["'"])
    if mode == "'":
        event.set()
        return None, None

    total_rows = len(rows)
    completion = {
        col: sum(1 for r in rows if normalize_text(r.get(col, "")) == "x")
        for col in run_columns
    }

    if mode == 1:
        dated_cols = []
        for col in run_columns:
            dt = parse_run_column_datetime(col)
            if dt is None:
                dt = datetime.datetime(1970, 1, 1)
            dated_cols.append((dt, col))
        dated_cols.sort(key=lambda t: t[0])
        oldest_col = dated_cols[0][1]
        new_col = build_run_column_name()
        while new_col in run_columns:
            new_col = build_run_column_name(
                datetime.datetime.now() + datetime.timedelta(seconds=1)
            )

        print(f"Fresh run will reuse oldest slot: {oldest_col}")
        print(f"New slot name: {new_col}")
        print("Proceed? [Y/n]")
        confirm = read(
            values=["y", "Y", "n", "N", "", "'"], additionalValues=["'"]
        )
        if confirm == "'" or confirm.lower() == "n":
            event.set()
            return None, None

        for i, col in enumerate(run_columns):
            if col == oldest_col:
                run_columns[i] = new_col
                break
        for row in rows:
            row[new_col] = ""
            if oldest_col in row:
                del row[oldest_col]
        return mode, new_col

    # Resume mode
    print("")
    print("Select run slot to resume:")
    for i, col in enumerate(run_columns):
        done = completion.get(col, 0)
        print(f"({i + 1}) {col[4:]}  [{done}/{total_rows} sent]")
    print("(') Back to main menu")
    choice = read(
        min=1, max=len(run_columns), digit=True, additionalValues=["'"]
    )
    if choice == "'":
        event.set()
        return None, None
    return mode, run_columns[choice - 1]


# ============================================================================
#  RESOURCE INPUT
# ============================================================================

def readResourceAmount(resource_name):
    while True:
        user_input = read(
            msg=f"{resource_name}: ", empty=True,
            additionalValues=["'", "="]
        )
        if user_input == "'":
            return "EXIT"
        if user_input == "=":
            return "RESTART"
        if user_input == "":
            return None
        cleaned = user_input.replace(",", "").replace(" ", "")
        if cleaned.isdigit():
            amount = int(cleaned)
            if amount > 0:
                print(f"  -> Set to: {addThousandSeparator(amount)}")
            return amount
        print("  Please enter a number, 0, leave blank, or press ' to exit")


def get_resource_config(send_mode=2):
    while True:
        config = []
        restart = False
        for i, resource in enumerate(materials_names):
            amount = readResourceAmount(resource)
            if amount == "EXIT":
                return None
            if amount == "RESTART":
                print("\nRestarting resource configuration...\n")
                restart = True
                break
            if send_mode == 2 and amount is None:
                amount = 0
            config.append(amount)
        if not restart:
            return config


def get_dest_minimums():
    print("")
    print("Set minimum thresholds for destination?")
    print("(Only send if destination has LESS than the minimum)")
    print("(1) Yes - set minimums per resource")
    print("(2) No - send regardless")
    choice = read(min=1, max=2, digit=True)
    if choice == 2:
        return None
    print("")
    print("Enter minimum for each resource (blank = no minimum):")
    minimums = []
    for resource in materials_names:
        amount = readResourceAmount(f"Min {resource}")
        if amount in ("EXIT", "RESTART"):
            return None
        minimums.append(amount if amount is not None else 0)
    return minimums


def apply_dest_minimums(sendable, dest_current, minimum):
    if minimum is None or minimum == 0:
        return sendable
    if dest_current >= minimum:
        return 0
    needed = minimum - dest_current
    return min(sendable, needed)


# ============================================================================
#  DRY RUN PREVIEW
# ============================================================================

def run_dry_preview(routes_info, mode_name):
    print_module_banner(f"DRY RUN PREVIEW - {mode_name}")
    print(f"  {'#':<4} {'From':<18} {'To':<18}", end="")
    for res in materials_names:
        print(f" {res:>9}", end="")
    print("")
    print(f"  {'--':<4} {'-'*18:<18} {'-'*18:<18}", end="")
    for _ in materials_names:
        print(f" {'-'*9:>9}", end="")
    print("")

    totals = [0] * len(materials_names)
    for idx, info in enumerate(routes_info):
        src = info["source"][:18] if len(info["source"]) <= 18 else info["source"][:15] + "..."
        dst = info["dest"][:18] if len(info["dest"]) <= 18 else info["dest"][:15] + "..."
        resources = info["resources"]
        print(f"  {idx+1:<4} {src:<18} {dst:<18}", end="")
        for i in range(len(materials_names)):
            val = resources[i] if i < len(resources) else 0
            totals[i] += val
            if val > 0:
                print(f" {addThousandSeparator(val):>9}", end="")
            else:
                print(f" {'0':>9}", end="")
        print("")

    print(f"  {'--':<4} {'-'*18:<18} {'-'*18:<18}", end="")
    for _ in materials_names:
        print(f" {'-'*9:>9}", end="")
    print("")
    print(f"  {'':4} {'':18} {'TOTAL':<18}", end="")
    for i in range(len(materials_names)):
        print(f" {addThousandSeparator(totals[i]):>9}", end="")
    print("\n")
    print("  --- DRY RUN: No resources were sent. ---\n")


# ============================================================================
#  MAIN ENTRY POINT
# ============================================================================

def resourceTransportManager(session, event, stdin_fd, predetermined_input):
    sys.stdin = os.fdopen(stdin_fd)
    config.predetermined_input = predetermined_input

    try:
        telegram_enabled = checkTelegramData(session)

        print_module_banner("Shipment Log Setup")
        log_path = get_log_path(session)

        print_module_banner("Shipping Mode Selection")
        print("Select shipping mode:")
        print("(1) Consolidate: Multiple cities -> One destination")
        print("(2) Distribute: One city -> Multiple destinations")
        print("(3) Even Distribution: Balance resources across cities")
        print("(4) Auto Send: Request resources, auto-collect from all")
        print("(5) Mass Distribution: Send from CSV file")
        print("(') Back to main menu")
        shipping_mode = read(min=1, max=5, digit=True, additionalValues=["'"])
        if shipping_mode == "'":
            event.set()
            return

        if shipping_mode == 1:
            consolidateMode(session, event, stdin_fd, predetermined_input,
                            telegram_enabled, log_path)
        elif shipping_mode == 2:
            distributeMode(session, event, stdin_fd, predetermined_input,
                           telegram_enabled, log_path)
        elif shipping_mode == 3:
            evenDistributionMode(session, event, stdin_fd,
                                 predetermined_input, telegram_enabled,
                                 log_path)
        elif shipping_mode == 4:
            autoSendMode(session, event, stdin_fd, predetermined_input,
                         telegram_enabled, log_path)
        else:
            massDistributionMode(session, event, stdin_fd,
                                 predetermined_input, telegram_enabled,
                                 log_path)

    except KeyboardInterrupt:
        event.set()
        return


# ============================================================================
#  MODE 1: CONSOLIDATE  (many sources -> one destination)
# ============================================================================

def consolidateMode(session, event, stdin_fd, predetermined_input,
                    telegram_enabled, log_path):
    try:
        print_module_banner("Ship Type Selection")
        print("What type of ships do you want to use?")
        print("(1) Merchant ships")
        print("(2) Freighters")
        print("(') Back to main menu")
        shiptype = read(min=1, max=2, digit=True, additionalValues=["'"])
        if shiptype == "'":
            event.set()
            return
        useFreighters = (shiptype == 2)

        print_module_banner("Source City Selection")
        print("Select source city option:")
        print("(1) Single city")
        print("(2) Multiple cities")
        print("(') Back to main menu")
        source_option = read(min=1, max=2, digit=True, additionalValues=["'"])
        if source_option == "'":
            event.set()
            return

        origin_cities = []
        if source_option == 1:
            print_module_banner("Single Source City")
            print("Select source city:")
            origin_city = chooseCity(session)
            origin_cities.append(origin_city)
        else:
            print_module_banner("Multiple Source Cities")
            source_msg = "Select source cities (cities to send resources from):"
            source_city_ids, _ = ignoreCities(session, msg=source_msg)
            if not source_city_ids:
                print("No cities selected!")
                enter()
                event.set()
                return
            for city_id in source_city_ids:
                html = session.get(city_url + city_id)
                city = getCity(html)
                origin_cities.append(city)

        print_module_banner("Sending Mode Selection")
        source_summary = ", ".join(c["name"] for c in origin_cities)
        print(f"Source cities: {source_summary}")
        print("")
        print("Choose sending mode:")
        print("(1) Send ALL EXCEPT a reserve amount (keep X, send rest)")
        print("(2) Send SPECIFIC amounts (send exactly X)")
        print("(') Back to main menu")
        send_mode = read(min=1, max=2, digit=True, additionalValues=["'"])
        if send_mode == "'":
            event.set()
            return

        print_module_banner("Resource Configuration")
        print(f"Source cities: {source_summary}\n")
        if send_mode == 1:
            print("Configure resource reserves (KEEP mode):")
            print("(Number = keep that amount, 0 = send ALL, blank = IGNORE)")
            print("(Commas optional: 6000 or 6,000)")
            print("(= restart | ' exit)\n")
        else:
            print("Configure resource amounts to send:")
            print("(Number = send that amount, 0 or blank = skip)")
            print("(Commas optional: 6000 or 6,000)")
            print("(= restart | ' exit)\n")
            if len(origin_cities) == 1:
                html = session.get(city_url + str(origin_cities[0]["id"]))
                cdata = getCity(html)
                for i, res in enumerate(materials_names):
                    avail = cdata["availableResources"][i]
                    print(f"  {res}: {addThousandSeparator(avail)} available")
                print("")

        resource_config = get_resource_config(send_mode)
        if resource_config is None:
            event.set()
            return

        # --- Destination ---
        print_module_banner("Destination Selection")
        print(f"Source cities: {source_summary}\n")
        print("Select destination type:")
        print("(1) Internal city (your cities)")
        print("(2) External city (island coordinates)")
        print("(') Back to main menu")
        dest_type = read(min=1, max=2, digit=True, additionalValues=["'"])
        if dest_type == "'":
            event.set()
            return

        if dest_type == 2:
            # External city
            coords_done = False
            while not coords_done:
                print_module_banner("Island Coordinates")
                print("Enter destination island coordinates:")
                print("(' exit | = restart)\n")
                x_coord = read(msg="X coordinate: ", digit=True,
                               additionalValues=["'", "="])
                if x_coord == "'":
                    event.set()
                    return
                if x_coord == "=":
                    continue
                y_coord = read(msg="Y coordinate: ", digit=True,
                               additionalValues=["'", "="])
                if y_coord == "'":
                    event.set()
                    return
                if y_coord == "=":
                    continue

                html = session.get(
                    f"view=island&xcoord={x_coord}&ycoord={y_coord}"
                )
                island = getIsland(html)
                cities_on_island = [
                    c for c in island["cities"] if c["type"] == "city"
                ]
                if not cities_on_island:
                    print(f"No cities on island [{x_coord}:{y_coord}]!")
                    continue

                print(f"\nIsland: {island['name']} [{island['x']}:{island['y']}]")
                print(f"Resource: {materials_names[int(island['tradegood'])]}\n")
                print("Select destination city:")
                print(f"    {'City Name':<20} {'Player':<15}")
                print(f"    {'-'*20} {'-'*15}")
                for i, c in enumerate(cities_on_island):
                    cn = c.get("name", "?")[:20]
                    pn = c.get("Name", "?")[:15]
                    print(f"({i+1:>2}) {cn:<20} {pn:<15}")
                print("(') Back | (=) Restart\n")
                cc = read(min=0, max=len(cities_on_island),
                          additionalValues=["'", "="])
                if cc == "'" or cc == 0:
                    event.set()
                    return
                if cc == "=":
                    continue

                dest_data = cities_on_island[cc - 1]
                dest_id = dest_data["id"]
                html = session.get(city_url + str(dest_id))
                destination_city = getCity(html)
                destination_city["isOwnCity"] = (
                    dest_data.get("state", "") == ""
                    and dest_data.get("Name", "") == session.username
                )
                dest_player = dest_data.get("Name", "Unknown")

                print(f"\nSelected: {destination_city['name']} ({dest_player})")
                print("Confirm? [Y/n] (= restart)")
                conf = read(values=["y", "Y", "n", "N", "", "="])
                if conf == "=":
                    continue
                if conf.lower() == "n":
                    continue
                coords_done = True
        else:
            # Internal city
            print_module_banner("Internal City Selection")
            print("Select destination city:\n")
            destination_city = chooseCity(session)
            html = session.get(city_url + str(destination_city["id"]))
            destination_city = getCity(html)
            island_id = destination_city["islandId"]
            html = session.get(island_url + island_id)
            island = getIsland(html)
            destination_city["isOwnCity"] = True
            dest_player = session.username

        # Auto-exclude destination from sources
        before = len(origin_cities)
        origin_cities = [
            c for c in origin_cities if c["id"] != destination_city["id"]
        ]
        if before - len(origin_cities) > 0:
            print(f"\nExcluded destination '{destination_city['name']}' from sources")
        if not origin_cities:
            print("Error: No source cities remaining!")
            enter()
            event.set()
            return

        # Resource minimums for internal destinations
        dest_minimums = None
        if destination_city.get("isOwnCity", False):
            dest_minimums = get_dest_minimums()

        # Notifications
        notif_config = get_notification_config(telegram_enabled, event)
        if notif_config is None:
            return

        # Schedule
        print_module_banner("Schedule Configuration")
        interval_confirmed = False
        while not interval_confirmed:
            print("How often to send (in hours)?")
            print("(0 = one-time | 1+ = recurring)")
            print("(') Back to main menu")
            interval_hours = read(min=0, digit=True, additionalValues=["'"])
            if interval_hours == "'":
                event.set()
                return
            print("")
            if interval_hours == 0:
                print("Mode: One-time shipment")
            else:
                print(f"Interval: Every {interval_hours} hour(s)")
            print("(1) Confirm  (2) Re-enter")
            if read(min=1, max=2, digit=True) == 1:
                interval_confirmed = True

        # --- Calculate preview ---
        total_send = [0] * len(materials_names)
        preview_routes = []
        for oc in origin_cities:
            html = session.get(city_url + str(oc["id"]))
            odata = getCity(html)
            to_send = [0] * len(materials_names)
            for i in range(len(materials_names)):
                if resource_config[i] is None:
                    continue
                avail = odata["availableResources"][i]
                if send_mode == 1:
                    s = avail if resource_config[i] == 0 else max(0, avail - resource_config[i])
                else:
                    s = 0 if resource_config[i] == 0 else min(resource_config[i], avail)
                if destination_city.get("isOwnCity", False):
                    s = min(s, destination_city["freeSpaceForResources"][i])
                if dest_minimums:
                    s = apply_dest_minimums(
                        s, destination_city["availableResources"][i],
                        dest_minimums[i]
                    )
                to_send[i] = s
                total_send[i] += s
            if sum(to_send) > 0:
                preview_routes.append({
                    "source": odata["name"],
                    "dest": destination_city["name"],
                    "resources": to_send,
                })

        # Final confirmation with dry-run option
        while True:
            print_module_banner("Configuration Summary")
            ship_label = "Freighters" if useFreighters else "Merchant ships"
            print(f"  Ship type: {ship_label}")
            mode_label = "Keep reserves" if send_mode == 1 else "Send specific"
            print(f"  Mode: {mode_label}")
            print(f"  Sources ({len(origin_cities)}): {source_summary}")
            print(f"  Destination: {destination_city['name']}")
            if dest_minimums:
                print(f"  Dest minimums: {dest_minimums}")
            print(f"  Interval: {'One-time' if interval_hours == 0 else f'{interval_hours}h'}")
            print(f"  Total: {addThousandSeparator(sum(total_send))} resources\n")
            print("(Y) Proceed  (D) Dry run preview  (N) Cancel")
            rta = read(values=["y", "Y", "n", "N", "d", "D", ""])
            if rta.lower() == "n":
                event.set()
                return
            if rta.lower() == "d":
                run_dry_preview(preview_routes, "Consolidate")
                print("Press Enter to continue...")
                enter()
                continue
            break

        enter()

    except KeyboardInterrupt:
        event.set()
        return

    set_child_mode(session)
    event.set()

    info = (
        f"\nAuto-send from {source_summary} to "
        f"{destination_city['name']} every {interval_hours}h\n"
    )
    setInfoSignal(session, info)
    try:
        do_it(session, origin_cities, destination_city, island,
              interval_hours, resource_config, useFreighters, send_mode,
              notif_config, dest_minimums, log_path)
    except Exception:
        sendToBot(session, f"Error in:\n{info}\nCause:\n{traceback.format_exc()}")
    finally:
        session.logout()


# ============================================================================
#  MODE 1 EXECUTION: do_it (Consolidate)
# ============================================================================

def do_it(session, origin_cities, destination_city, island,
          interval_hours, resource_config, useFreighters, send_mode,
          notif_config, dest_minimums, log_path):

    total_shipments = 0
    consecutive_failures = 0
    first_run = True
    next_run_time = datetime.datetime.now()

    while True:
        now = datetime.datetime.now()
        if not first_run and now < next_run_time:
            sleep_secs = max(0, (next_run_time - now).total_seconds())
            time.sleep(min(sleep_secs, 60))
            continue

        # Refresh destination
        html = session.get(city_url + str(destination_city["id"]))
        destination_city = getCity(html)

        # Start notification
        if should_notify(notif_config, "start"):
            src_names = ", ".join(c["name"] for c in origin_cities)
            sendToBot(session,
                      f"SHIPMENT CYCLE STARTING\n"
                      f"Account: {session.username}\n"
                      f"From: {src_names}\n"
                      f"To: [{island['x']}:{island['y']}] "
                      f"{destination_city['name']}")

        for oc in origin_cities:
            html = session.get(city_url + str(oc["id"]))
            oc_fresh = getCity(html)

            toSend = [0] * len(materials_names)
            total = 0
            for i in range(len(materials_names)):
                if resource_config[i] is None:
                    continue
                avail = oc_fresh["availableResources"][i]
                if send_mode == 1:
                    s = avail if resource_config[i] == 0 else max(0, avail - resource_config[i])
                else:
                    s = 0 if resource_config[i] == 0 else min(resource_config[i], avail)
                if destination_city.get("isOwnCity", False):
                    s = min(s, destination_city["freeSpaceForResources"][i])
                if dest_minimums:
                    s = apply_dest_minimums(
                        s, destination_city["availableResources"][i],
                        dest_minimums[i]
                    )
                toSend[i] = s
                total += s

            if total > 0:
                route = (oc_fresh, destination_city, island["id"], *toSend)
                coords = f"[{island['x']}:{island['y']}]"
                result = send_shipment(
                    session, route, useFreighters, notif_config, log_path,
                    "Consolidate", coords
                )
                if result["success"]:
                    total_shipments += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= 3 and should_notify(notif_config, "error"):
                        sendToBot(session,
                                  f"WARNING: {consecutive_failures} consecutive failures")

        if interval_hours == 0:
            src_names = ", ".join(c["name"] for c in origin_cities)
            session.setStatus(
                f"One-time shipment done: {src_names} -> "
                f"{destination_city['name']}"
            )
            return

        next_run_time = datetime.datetime.now() + datetime.timedelta(
            hours=interval_hours
        )
        session.setStatus(
            f"Shipments: {total_shipments} | "
            f"Next: {getDateTime(next_run_time.timestamp())}"
        )
        first_run = False
        sleep_secs = max(0, (next_run_time - datetime.datetime.now()).total_seconds())
        time.sleep(sleep_secs)


# ============================================================================
#  MODE 2: DISTRIBUTE  (one source -> many destinations)
# ============================================================================

def distributeMode(session, event, stdin_fd, predetermined_input,
                   telegram_enabled, log_path):
    try:
        print_module_banner("Ship Type Selection")
        print("What type of ships do you want to use?")
        print("(1) Merchant ships")
        print("(2) Freighters")
        print("(') Back to main menu")
        shiptype = read(min=1, max=2, digit=True, additionalValues=["'"])
        if shiptype == "'":
            event.set()
            return
        useFreighters = (shiptype == 2)

        print_module_banner("Source City Selection")
        print("Select source city:\n")
        origin_city = chooseCity(session)

        banner()
        print(f"Source city: {origin_city['name']}")
        print("Note: Source city auto-excluded from destinations\n")
        dest_msg = "Select destination cities (cities to receive resources):"
        dest_ids, _ = ignoreCities(session, msg=dest_msg)

        src_id = str(origin_city["id"])
        if src_id in dest_ids:
            dest_ids.remove(src_id)
            print(f"Removed {origin_city['name']} from destinations (source)")

        if not dest_ids:
            print("No valid destination cities!")
            enter()
            event.set()
            return

        destination_cities = []
        for cid in dest_ids:
            html = session.get(city_url + cid)
            destination_cities.append(getCity(html))

        banner()
        dest_summary = ", ".join(c["name"] for c in destination_cities)
        print(f"Source: {origin_city['name']}")
        print(f"Destinations: {dest_summary}\n")
        print("Resources to send to EACH destination:")
        print("(Number = amount, 0 or blank = skip)")
        print("(= restart | ' exit)\n")

        resource_config = get_resource_config(send_mode=2)
        if resource_config is None:
            event.set()
            return

        # Destination minimums (all internal)
        dest_minimums = get_dest_minimums()

        # Notifications
        notif_config = get_notification_config(telegram_enabled, event)
        if notif_config is None:
            return

        # Schedule
        print_module_banner("Schedule Configuration")
        interval_confirmed = False
        while not interval_confirmed:
            print("How often to send (in hours)?")
            print("(0 = one-time | 1+ = recurring)")
            print("(') Back to main menu")
            interval_hours = read(min=0, digit=True, additionalValues=["'"])
            if interval_hours == "'":
                event.set()
                return
            print("")
            if interval_hours == 0:
                print("Mode: One-time shipment")
            else:
                print(f"Interval: Every {interval_hours} hour(s)")
            print("(1) Confirm  (2) Re-enter")
            if read(min=1, max=2, digit=True) == 1:
                interval_confirmed = True

        # Preview
        total_needed = [a * len(destination_cities) for a in resource_config]
        grand = sum(total_needed)
        preview_routes = []
        for dc in destination_cities:
            preview_routes.append({
                "source": origin_city["name"],
                "dest": dc["name"],
                "resources": resource_config,
            })

        while True:
            print_module_banner("Configuration Summary")
            ship_label = "Freighters" if useFreighters else "Merchant ships"
            print(f"  Ship type: {ship_label}")
            print(f"  Source: {origin_city['name']}")
            print(f"  Destinations ({len(destination_cities)}): {dest_summary}")
            if dest_minimums:
                print(f"  Dest minimums: {dest_minimums}")
            print(f"  Total resources: {addThousandSeparator(grand)}")
            int_label = "One-time" if interval_hours == 0 else f"{interval_hours}h"
            print(f"  Interval: {int_label}\n")
            print("(Y) Proceed  (D) Dry run preview  (N) Cancel")
            rta = read(values=["y", "Y", "n", "N", "d", "D", ""])
            if rta.lower() == "n":
                event.set()
                return
            if rta.lower() == "d":
                run_dry_preview(preview_routes, "Distribute")
                print("Press Enter to continue...")
                enter()
                continue
            break

        enter()

    except KeyboardInterrupt:
        event.set()
        return

    set_child_mode(session)
    event.set()

    info = (
        f"\nDistribute from {origin_city['name']} to "
        f"{len(destination_cities)} cities every {interval_hours}h\n"
    )
    setInfoSignal(session, info)
    try:
        do_it_distribute(session, origin_city, destination_cities,
                         interval_hours, resource_config, useFreighters,
                         notif_config, dest_minimums, log_path)
    except Exception:
        sendToBot(session, f"Error in:\n{info}\nCause:\n{traceback.format_exc()}")
    finally:
        session.logout()


# ============================================================================
#  MODE 2 EXECUTION: do_it_distribute
# ============================================================================

def do_it_distribute(session, origin_city, destination_cities,
                     interval_hours, resource_config, useFreighters,
                     notif_config, dest_minimums, log_path):

    total_shipments = 0
    consecutive_failures = 0
    first_run = True
    next_run_time = datetime.datetime.now()

    while True:
        now = datetime.datetime.now()
        if not first_run and now < next_run_time:
            sleep_secs = max(0, (next_run_time - now).total_seconds())
            time.sleep(min(sleep_secs, 60))
            continue

        # Refresh origin
        html = session.get(city_url + str(origin_city["id"]))
        origin_fresh = getCity(html)
        origin_island_id = origin_fresh["islandId"]
        html_isl = session.get(island_url + str(origin_island_id))
        origin_island = getIsland(html_isl)

        if should_notify(notif_config, "start"):
            dest_names = ", ".join(c["name"] for c in destination_cities)
            sendToBot(session,
                      f"DISTRIBUTION CYCLE STARTING\n"
                      f"Account: {session.username}\n"
                      f"From: {origin_fresh['name']}\n"
                      f"To: {len(destination_cities)} cities ({dest_names})")

        for dc in destination_cities:
            html = session.get(city_url + str(dc["id"]))
            dc_fresh = getCity(html)
            dest_isl_id = dc_fresh["islandId"]
            html_di = session.get(island_url + str(dest_isl_id))
            dest_island = getIsland(html_di)

            toSend = [0] * len(materials_names)
            total = 0
            for i in range(len(materials_names)):
                if resource_config[i] == 0:
                    continue
                avail = origin_fresh["availableResources"][i]
                s = min(resource_config[i], avail)
                dest_space = dc_fresh.get("freeSpaceForResources", [0]*5)
                if i < len(dest_space):
                    s = min(s, dest_space[i])
                if dest_minimums:
                    s = apply_dest_minimums(
                        s, dc_fresh["availableResources"][i],
                        dest_minimums[i]
                    )
                toSend[i] = s
                total += s

            if total > 0:
                route = (origin_fresh, dc_fresh, dest_island["id"], *toSend)
                coords = f"[{dest_island['x']}:{dest_island['y']}]"
                result = send_shipment(
                    session, route, useFreighters, notif_config, log_path,
                    "Distribute", coords
                )
                if result["success"]:
                    total_shipments += 1
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= 3 and should_notify(notif_config, "error"):
                        sendToBot(session,
                                  f"WARNING: {consecutive_failures} consecutive failures")

        if interval_hours == 0:
            session.setStatus(
                f"One-time distribution done: {origin_fresh['name']}"
            )
            return

        next_run_time = datetime.datetime.now() + datetime.timedelta(
            hours=interval_hours
        )
        session.setStatus(
            f"{origin_fresh['name']} -> {len(destination_cities)} cities | "
            f"Shipments: {total_shipments} | "
            f"Next: {getDateTime(next_run_time.timestamp())}"
        )
        first_run = False
        sleep_secs = max(0, (next_run_time - datetime.datetime.now()).total_seconds())
        time.sleep(sleep_secs)


# ============================================================================
#  MODE 3: EVEN DISTRIBUTION  (balance resources across cities)
#  Now supports MULTI-RESOURCE balancing
# ============================================================================

def evenDistributionMode(session, event, stdin_fd, predetermined_input,
                         telegram_enabled, log_path):
    try:
        # Select resources (multi-select)
        print_module_banner("Resource Selection")
        print("Select which resource(s) to balance across cities:")
        for i, res in enumerate(materials_names):
            print(f"({i+1}) {res}")
        print("")
        print("Enter one or more numbers (comma-separated, e.g. 1,3,5):")
        print("(') Back to main menu")
        raw = read(msg="Resources: ", additionalValues=["'"])
        if raw == "'":
            event.set()
            return

        resource_indices = []
        for part in str(raw).split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(materials_names):
                    resource_indices.append(idx)
        resource_indices = list(dict.fromkeys(resource_indices))  # dedupe
        if not resource_indices:
            print("No valid resources selected!")
            enter()
            event.set()
            return

        selected_names = ", ".join(materials_names[i] for i in resource_indices)
        print(f"\nBalancing: {selected_names}")

        # Ship type
        print_module_banner("Ship Type Selection")
        print(f"Balancing: {selected_names}\n")
        print("What type of ships?")
        print("(1) Merchant ships")
        print("(2) Freighters")
        print("(') Back to main menu")
        shiptype = read(min=1, max=2, digit=True, additionalValues=["'"])
        if shiptype == "'":
            event.set()
            return
        useFreighters = (shiptype == 2)

        # Select cities
        print_module_banner("City Selection")
        print(f"Balancing: {selected_names}\n")
        print("Select cities to EXCLUDE from balancing:")
        excluded_ids, _ = ignoreCities(session, msg="Select cities to EXCLUDE:")

        html = session.get()
        city_ids = re.findall(r'<option value="(\d+)" class="cityowntown"', html)
        all_cities = []
        for cid in city_ids:
            if cid not in excluded_ids:
                html_c = session.get(city_url + cid)
                all_cities.append(getCity(html_c))

        if not all_cities:
            print("No cities available for balancing!")
            enter()
            event.set()
            return

        # Calculate plan for each resource
        all_shipments = {}  # resource_index -> list of shipments
        for res_idx in resource_indices:
            res_name = materials_names[res_idx]
            total = sum(c["availableResources"][res_idx] for c in all_cities)
            target = total // len(all_cities)

            shipments = []
            for city in all_cities:
                current = city["availableResources"][res_idx]
                diff = current - target
                if diff > 0:
                    shipments.append({"from": city, "amount": diff, "type": "sender"})
                elif diff < 0:
                    shipments.append({"to": city, "amount": abs(diff), "type": "receiver"})

            all_shipments[res_idx] = shipments

            print(f"\n  {res_name}: total={addThousandSeparator(total)}, "
                  f"target/city={addThousandSeparator(target)}")
            senders = [s for s in shipments if s["type"] == "sender"]
            receivers = [s for s in shipments if s["type"] == "receiver"]
            for s in senders:
                print(f"    SEND: {s['from']['name']} -> {addThousandSeparator(s['amount'])}")
            for r in receivers:
                print(f"    RECV: {r['to']['name']} <- {addThousandSeparator(r['amount'])}")

        # Build preview routes
        preview_routes = []
        for res_idx in resource_indices:
            shipments = all_shipments[res_idx]
            senders = [s for s in shipments if s["type"] == "sender"]
            receivers = [s for s in shipments if s["type"] == "receiver"]
            si, ri = 0, 0
            s_rem = senders[si]["amount"] if senders else 0
            r_rem = receivers[ri]["amount"] if receivers else 0
            while si < len(senders) and ri < len(receivers):
                amt = min(s_rem, r_rem)
                if amt > 0:
                    res_list = [0] * len(materials_names)
                    res_list[res_idx] = amt
                    preview_routes.append({
                        "source": senders[si]["from"]["name"],
                        "dest": receivers[ri]["to"]["name"],
                        "resources": res_list,
                    })
                s_rem -= amt
                r_rem -= amt
                if s_rem == 0:
                    si += 1
                    if si < len(senders):
                        s_rem = senders[si]["amount"]
                if r_rem == 0:
                    ri += 1
                    if ri < len(receivers):
                        r_rem = receivers[ri]["amount"]

        if not preview_routes:
            print("\nAll cities are already balanced! Nothing to do.")
            enter()
            event.set()
            return

        # Confirmation with dry run
        while True:
            print(f"\n{len(preview_routes)} shipment(s) planned.")
            print("(1) Confirm - Start balancing")
            print("(D) Dry run - preview shipments")
            print("(2) Cancel")
            choice = read(values=["1", "2", "d", "D"])
            if choice == "2":
                event.set()
                return
            if choice.lower() == "d":
                run_dry_preview(preview_routes, "Even Distribution")
                print("Press Enter to continue...")
                enter()
                continue
            break

        # Notifications
        notif_config = get_notification_config(telegram_enabled, event)
        if notif_config is None:
            return

        enter()

    except KeyboardInterrupt:
        event.set()
        return

    set_child_mode(session)
    event.set()

    info = f"\nBalancing {selected_names} across {len(all_cities)} cities\n"
    setInfoSignal(session, info)

    try:
        # Execute sequentially per resource
        for res_idx in resource_indices:
            res_name = materials_names[res_idx]
            shipments = all_shipments[res_idx]
            do_even_distribution(session, shipments, res_idx, res_name,
                                 useFreighters, notif_config, log_path)
    except Exception:
        sendToBot(session, f"Error in:\n{info}\nCause:\n{traceback.format_exc()}")
    finally:
        session.logout()


# ============================================================================
#  MODE 3 EXECUTION: do_even_distribution
# ============================================================================

def do_even_distribution(session, shipments, resource_index, resource_name,
                         useFreighters, notif_config, log_path):
    senders = [s for s in shipments if s["type"] == "sender"]
    receivers = [s for s in shipments if s["type"] == "receiver"]

    # FIX: check for empty lists
    if not senders or not receivers:
        msg = f"{resource_name}: Already balanced (no shipments needed)"
        if should_notify(notif_config, "complete"):
            sendToBot(session, msg)
        return

    if should_notify(notif_config, "start"):
        plan_lines = []
        si, ri = 0, 0
        s_rem, r_rem = senders[0]["amount"], receivers[0]["amount"]
        while si < len(senders) and ri < len(receivers):
            amt = min(s_rem, r_rem)
            if amt > 0:
                plan_lines.append(
                    f"{senders[si]['from']['name']} -> "
                    f"{receivers[ri]['to']['name']}: "
                    f"{addThousandSeparator(amt)} {resource_name}"
                )
            s_rem -= amt
            r_rem -= amt
            if s_rem == 0:
                si += 1
                s_rem = senders[si]["amount"] if si < len(senders) else 0
            if r_rem == 0:
                ri += 1
                r_rem = receivers[ri]["amount"] if ri < len(receivers) else 0
        sendToBot(session,
                  f"BALANCING PLAN\nResource: {resource_name}\n\n"
                  + "\n".join(plan_lines))

    # Execute
    si, ri = 0, 0
    s_rem = senders[0]["amount"]
    r_rem = receivers[0]["amount"]

    while si < len(senders) and ri < len(receivers):
        sender = senders[si]
        receiver = receivers[ri]
        amount = min(s_rem, r_rem)

        if amount > 0:
            toSend = [0] * len(materials_names)
            toSend[resource_index] = amount

            dest_isl_id = receiver["to"]["islandId"]
            html = session.get(island_url + str(dest_isl_id))
            dest_island = getIsland(html)

            route = (sender["from"], receiver["to"],
                     dest_island["id"], *toSend)
            coords = f"[{dest_island['x']}:{dest_island['y']}]"

            result = send_shipment(
                session, route, useFreighters, notif_config, log_path,
                "Even Distribution", coords
            )

            s_rem -= amount
            r_rem -= amount

        if s_rem == 0:
            si += 1
            if si < len(senders):
                s_rem = senders[si]["amount"]
        if r_rem == 0:
            ri += 1
            if ri < len(receivers):
                r_rem = receivers[ri]["amount"]

    if should_notify(notif_config, "complete"):
        sendToBot(session,
                  f"BALANCING COMPLETE\nResource: {resource_name}")


# ============================================================================
#  MODE 4: AUTO SEND  (request resources, auto-collect from all cities)
# ============================================================================

def autoSendMode(session, event, stdin_fd, predetermined_input,
                 telegram_enabled, log_path):
    try:
        print_module_banner("Auto Send")
        print("What type of ships?")
        print("(1) Merchant ships")
        print("(2) Freighters")
        print("(') Back to main menu")
        shiptype = read(min=1, max=2, digit=True, additionalValues=["'"])
        if shiptype == "'":
            event.set()
            return
        useFreighters = (shiptype == 2)

        while True:
            print_module_banner("Auto Send")
            print("Select the city to send resources TO:\n")
            destination_city = chooseCity(session)

            html = session.get(island_url + destination_city["islandId"])
            destination_island = getIsland(html)

            print_module_banner("Auto Send")
            print("Scanning cities...")
            html = session.get()
            city_ids = re.findall(
                r'<option value="(\d+)" class="cityowntown"', html
            )

            suppliers = []
            totals = [0] * len(materials_names)
            for cid in city_ids:
                if str(cid) == str(destination_city["id"]):
                    continue
                html_c = session.get(city_url + str(cid))
                cdata = getCity(html_c)
                suppliers.append(cdata)
                for i in range(len(materials_names)):
                    totals[i] += cdata["availableResources"][i]

            if not suppliers:
                print("No supplier cities available!")
                enter()
                event.set()
                return

            print_module_banner("Auto Send")
            print(f"  Destination: {destination_city['name']} "
                  f"[{destination_island['x']}:{destination_island['y']}]\n")
            print("  Available resources (excluding destination):")
            for i, res in enumerate(materials_names):
                print(f"    {res:<12} {addThousandSeparator(totals[i]):>12}")
            print("")

            while True:
                print("  Enter how much of each resource to collect:")
                print("  (blank = skip, ' = exit, = = restart)\n")

                requested = [0] * len(materials_names)
                restart = False
                for i, res in enumerate(materials_names):
                    result = readResourceAmount(res)
                    if result == "EXIT":
                        event.set()
                        return
                    if result == "RESTART":
                        restart = True
                        break
                    requested[i] = result if result and result > 0 else 0

                if restart:
                    break

                if sum(requested) == 0:
                    print("\n  No resources requested.")
                    enter()
                    event.set()
                    return

                over = [
                    f"    {materials_names[i]}: requested "
                    f"{addThousandSeparator(requested[i])}, "
                    f"available {addThousandSeparator(totals[i])}"
                    for i in range(len(materials_names))
                    if requested[i] > totals[i]
                ]
                if over:
                    print("\n  ERROR: Exceeds available:")
                    for line in over:
                        print(line)
                    print("\n  Re-enter amounts.\n")
                    continue

                routes = allocate_from_suppliers(
                    requested, suppliers, destination_city, destination_island
                )
                if routes is None:
                    print("\n  ERROR: Could not allocate across suppliers.")
                    enter()
                    event.set()
                    return

                ship_cap, freighter_cap = getShipCapacity(session)
                capacity = freighter_cap if useFreighters else ship_cap

                choice = render_auto_send_review(
                    destination_city, destination_island, routes,
                    useFreighters, capacity
                )

                if choice == "C":
                    event.set()
                    return
                elif choice == "E":
                    break
                elif choice == "D":
                    # Dry run
                    preview = []
                    for route in routes:
                        preview.append({
                            "source": route[0]["name"],
                            "dest": route[1]["name"],
                            "resources": list(route[3:]),
                        })
                    run_dry_preview(preview, "Auto Send")
                    print("Press Enter to continue...")
                    enter()
                    continue
                else:
                    # Notifications
                    notif_config = get_notification_config(
                        telegram_enabled, event
                    )
                    if notif_config is None:
                        return

                    set_child_mode(session)
                    event.set()
                    info = f"\nAuto Send to {destination_city['name']}\n"
                    setInfoSignal(session, info)
                    try:
                        do_it_auto_send(session, routes, useFreighters,
                                        notif_config, log_path)
                    except Exception:
                        sendToBot(session,
                                  f"Error:\n{info}\n{traceback.format_exc()}")
                    finally:
                        session.logout()
                    return

    except KeyboardInterrupt:
        event.set()
        return


def allocate_from_suppliers(requested, suppliers, destination_city,
                            destination_island):
    remaining = list(requested)
    routes = []
    for supplier in suppliers:
        to_send = [0] * len(materials_names)
        has_cargo = False
        for i in range(len(materials_names)):
            if remaining[i] <= 0:
                continue
            can_give = supplier["availableResources"][i]
            give = min(remaining[i], can_give)
            to_send[i] = give
            remaining[i] -= give
            if give > 0:
                has_cargo = True
        if has_cargo:
            route = (supplier, destination_city,
                     destination_island["id"], *to_send)
            routes.append(route)
        if all(r <= 0 for r in remaining):
            break
    if any(r > 0 for r in remaining):
        return None
    return routes


def render_auto_send_review(destination_city, destination_island, routes,
                            useFreighters, capacity):
    ship_type_name = "Freighters" if useFreighters else "Merchant ships"
    print_module_banner("Auto Send - Review")
    print(f"  Destination: {destination_city['name']} "
          f"[{destination_island['x']}:{destination_island['y']}]")
    print(f"  Ship type:   {ship_type_name} "
          f"(capacity: {addThousandSeparator(capacity)})\n")
    print("  Planned Shipments:")
    print(f"  {'#':<4} {'From':<18}", end="")
    for res in materials_names:
        print(f" {res:>9}", end="")
    print(f" {'Ships':>7}")
    print(f"  {'--':<4} {'-'*18:<18}", end="")
    for _ in materials_names:
        print(f" {'-'*9:>9}", end="")
    print(f" {'-'*7:>7}")

    grand = [0] * len(materials_names)
    total_ships = 0
    for idx, route in enumerate(routes):
        origin = route[0]
        amounts = route[3:]
        cargo = sum(amounts)
        ships = math.ceil(cargo / capacity) if capacity > 0 else 0
        name = origin["name"][:18] if len(origin["name"]) <= 18 else origin["name"][:15] + "..."
        print(f"  {idx+1:<4} {name:<18}", end="")
        for i in range(len(materials_names)):
            val = amounts[i] if i < len(amounts) else 0
            grand[i] += val
            if val > 0:
                print(f" {addThousandSeparator(val):>9}", end="")
            else:
                print(f" {'0':>9}", end="")
        print(f" {ships:>7}")
        total_ships += ships

    print(f"  {'--':<4} {'-'*18:<18}", end="")
    for _ in materials_names:
        print(f" {'-'*9:>9}", end="")
    print(f" {'-'*7:>7}")
    print(f"  {'':4} {'TOTAL':<18}", end="")
    for i in range(len(materials_names)):
        print(f" {addThousandSeparator(grand[i]):>9}", end="")
    print(f" {total_ships:>7}\n")

    print("  (Y) Proceed")
    print("  (D) Dry run preview")
    print("  (E) Edit - re-select")
    print("  (C) Cancel")
    choice = read(values=["y", "Y", "e", "E", "c", "C", "d", "D", ""])
    if choice == "" or choice.upper() == "Y":
        return "Y"
    return choice.upper()


# ============================================================================
#  MODE 4 EXECUTION: do_it_auto_send
# ============================================================================

def do_it_auto_send(session, routes, useFreighters, notif_config, log_path):
    total_routes = len(routes)
    completed = 0

    print(f"\n--- Auto Send: {total_routes} shipments ---\n")

    for idx, route in enumerate(routes):
        origin_city = route[0]
        dest_city = route[1]
        amounts = route[3:]
        res_desc = ", ".join(
            f"{addThousandSeparator(amounts[i])} {materials_names[i]}"
            for i in range(len(materials_names))
            if i < len(amounts) and amounts[i] > 0
        )

        print(f"  [{idx+1}/{total_routes}] {origin_city['name']} -> "
              f"{dest_city['name']}")
        print(f"    Resources: {res_desc}")

        result = send_shipment(
            session, route, useFreighters, notif_config, log_path,
            "Auto Send"
        )

        if result["success"]:
            completed += 1
            print(f"    SUCCESS ({completed}/{total_routes})")
        else:
            print(f"    FAILED: {result['error']}")
            if not result["success"] and result["error"] and "lock" in result["error"].lower():
                break  # Stop on lock failures

    print(f"\n--- Auto Send complete: {completed}/{total_routes} ---\n")
    if should_notify(notif_config, "complete"):
        sendToBot(session,
                  f"AUTO SEND COMPLETE\nAccount: {session.username}\n"
                  f"Shipments: {completed}/{total_routes}")


# ============================================================================
#  MODE 5: MASS DISTRIBUTION  (CSV-driven bulk sends)
# ============================================================================

def massDistributionMode(session, event, stdin_fd, predetermined_input,
                         telegram_enabled, log_path):
    try:
        print_module_banner("Mass Distribution")
        prefs = load_prefs()
        saved_csv = prefs.get("csv_path", "")
        if saved_csv:
            print(f"CSV file (Enter to reuse last: {saved_csv}):")
        else:
            print("Enter the full path to your CSV file:")
        print("(Columns: X, Y, Player, City, City_Location, "
              "Wood, Wine, Marble, Crystal, Sulphur, Hours)")
        print("(') Back to main menu\n")
        csv_input = read(msg="CSV path: ", empty=True, additionalValues=["'"])
        if csv_input == "'":
            event.set()
            return
        csv_path = csv_input.strip() if csv_input.strip() else saved_csv
        if not csv_path:
            print("No CSV path provided.")
            enter()
            event.set()
            return
        # Save for next time
        prefs["csv_path"] = csv_path
        save_prefs(prefs)

        if not os.path.isfile(csv_path):
            print(f"File not found: {csv_path}")
            enter()
            event.set()
            return

        try:
            rows = []
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                required = {
                    "X", "Y", "Player", "City", "City_Location",
                    "Wood", "Wine", "Marble", "Crystal", "Sulphur", "Hours",
                }
                missing = required - set(fieldnames)
                if missing:
                    print(f"CSV missing columns: {', '.join(sorted(missing))}")
                    enter()
                    event.set()
                    return
                for row in reader:
                    rows.append(row)
        except Exception as e:
            print(f"Error reading CSV: {e}")
            enter()
            event.set()
            return

        if not rows:
            print("CSV has no data rows.")
            enter()
            event.set()
            return

        try:
            interval_hours = int(str(rows[0].get("Hours", "")).strip())
        except Exception:
            print("Invalid Hours value in CSV row 1.")
            enter()
            event.set()
            return

        if interval_hours <= 0:
            print("Hours must be >= 1 for Mass Distribution.")
            enter()
            event.set()
            return

        fieldnames, run_columns = ensure_run_columns(fieldnames, rows)

        mode, run_column = choose_run_slot(session, event, rows, run_columns)
        if run_column is None:
            return

        fieldnames_no_runs = [c for c in fieldnames if not c.startswith("Run_")]
        fieldnames = fieldnames_no_runs + run_columns
        backup_path = f"{csv_path}.bak"
        try:
            write_csv_atomic(csv_path, fieldnames, rows)
            write_csv_atomic(backup_path, fieldnames, rows)
        except Exception as e:
            print(f"Error preparing CSV: {e}")
            enter()
            event.set()
            return

        print_module_banner("Mass Distribution")
        print(f"CSV loaded: {len(rows)} rows")
        print(f"Interval: every {interval_hours} hour(s)")
        print(f"Run slot: {run_column[4:]}\n")
        print("What type of ships?")
        print("(1) Merchant ships")
        print("(2) Freighters")
        print("(') Back to main menu")
        shiptype = read(min=1, max=2, digit=True, additionalValues=["'"])
        if shiptype == "'":
            event.set()
            return
        useFreighters = (shiptype == 2)

        print_module_banner("Mass Distribution")
        print("Select the SOURCE city:\n")
        source_city = chooseCity(session)

        # Notifications
        notif_config = get_notification_config(telegram_enabled, event)
        if notif_config is None:
            return

        # Final confirmation with dry run
        while True:
            print_module_banner("Mass Distribution - Summary")
            print(f"  Source: {source_city['name']}")
            ship_label = "Freighters" if useFreighters else "Merchant ships"
            print(f"  Ships: {ship_label}")
            print(f"  CSV rows: {len(rows)}")
            print(f"  Interval: every {interval_hours}h")
            print(f"  Run slot: {run_column[4:]}\n")
            print("(Y) Proceed  (D) Dry run scan  (N) Cancel")
            rta = read(values=["y", "Y", "n", "N", "d", "D", "", "'"],
                       additionalValues=["'"])
            if rta == "'" or rta.lower() == "n":
                event.set()
                return
            if rta.lower() == "d":
                # Quick scan CSV for preview
                preview = _scan_csv_for_preview(
                    session, rows, run_column, source_city
                )
                if preview:
                    run_dry_preview(preview, "Mass Distribution")
                else:
                    print("  No valid routes found in scan.")
                print("Press Enter to continue...")
                enter()
                continue
            break

        enter()

    except KeyboardInterrupt:
        event.set()
        return

    set_child_mode(session)
    event.set()

    info = (
        f"\nMass Distribution from {source_city['name']} "
        f"every {interval_hours}h\n"
    )
    setInfoSignal(session, info)

    try:
        do_it_mass_distribution(session, csv_path, source_city,
                                useFreighters, interval_hours,
                                notif_config, run_column, log_path)
    except Exception:
        sendToBot(session, f"Error:\n{info}\n{traceback.format_exc()}")
    finally:
        session.logout()


def _scan_csv_for_preview(session, rows, run_column, source_city):
    """Quick scan for dry-run preview. Returns list of route info dicts."""
    csv_res_cols = ["Wood", "Wine", "Marble", "Crystal", "Sulphur"]
    preview = []
    for row in rows:
        if normalize_text(row.get(run_column, "")) == "x":
            continue
        resources = []
        for col in csv_res_cols:
            val = row.get(col, "0").strip()
            resources.append(int(val) if val.isdigit() else 0)
        if sum(resources) == 0:
            continue
        city_name = row.get("City", "?")
        player = row.get("Player", "?")
        preview.append({
            "source": source_city["name"],
            "dest": f"{city_name} ({player})",
            "resources": resources,
        })
    return preview


# ============================================================================
#  MODE 5 EXECUTION: do_it_mass_distribution
# ============================================================================

def do_it_mass_distribution(session, csv_path, source_city, useFreighters,
                            interval_hours, notif_config, run_column,
                            log_path):
    ship_type_name = "freighters" if useFreighters else "merchant ships"
    csv_resource_cols = ["Wood", "Wine", "Marble", "Crystal", "Sulphur"]

    while True:
        # Read CSV
        try:
            rows = []
            with open(csv_path, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                for row in reader:
                    rows.append(row)
        except Exception as e:
            if should_notify(notif_config, "error"):
                sendToBot(session,
                          f"MASS DIST ERROR\nCould not read CSV: {e}")
            time.sleep(3600)
            continue

        # Ensure run column exists
        if run_column not in (fieldnames or []):
            fieldnames, run_columns = ensure_run_columns(fieldnames, rows)
            if run_column not in run_columns:
                run_column = run_columns[0]
            try:
                write_csv_atomic(csv_path, fieldnames, rows)
            except Exception:
                time.sleep(3600)
                continue

        mismatches = []
        routes = []

        session.setStatus(
            f"[PROCESSING] Mass Distribution | "
            f"Scanning {len(rows)} rows..."
        )

        for row_num, row in enumerate(rows, start=1):
            try:
                if normalize_text(row.get(run_column, "")) == "x":
                    continue

                x = row["X"].strip()
                y = row["Y"].strip()
                expected_player = row["Player"].strip()
                expected_city = row["City"].strip()
                expected_location = str(row["City_Location"].strip())

                html = session.get(
                    f"view=island&xcoord={x}&ycoord={y}"
                )
                island = getIsland(html)
                cities_on_island = [
                    c for c in island["cities"]
                    if c.get("type") == "city"
                ]

                # Match city (case-insensitive)
                matched_city = None
                candidates = []
                exp_city_n = normalize_text(expected_city)
                exp_player_n = normalize_text(expected_player)

                for c in cities_on_island:
                    cn = normalize_text(c.get("name", ""))
                    pn = normalize_text(c.get("Name", ""))
                    if cn == exp_city_n and pn == exp_player_n:
                        candidates.append(c)

                if candidates:
                    exp_loc = normalize_text(expected_location)
                    if exp_loc:
                        for c in candidates:
                            loc = get_city_location_token(c)
                            if normalize_text(loc) == exp_loc:
                                matched_city = c
                                break
                    if matched_city is None and len(candidates) == 1:
                        matched_city = candidates[0]

                if matched_city is None:
                    mismatches.append(
                        f"Row {row_num}: [{x}:{y}] "
                        f"{expected_player}/{expected_city}"
                    )
                    continue

                resources = []
                for col in csv_resource_cols:
                    val = row.get(col, "0").strip()
                    resources.append(int(val) if val.isdigit() else 0)

                if sum(resources) == 0:
                    continue

                dest_html = session.get(city_url + str(matched_city["id"]))
                dest_city = getCity(dest_html)

                route = (source_city, dest_city, island["id"], *resources)
                routes.append((
                    row_num, route, resources, dest_city["name"],
                    expected_player, x, y
                ))

            except Exception as e:
                mismatches.append(
                    f"Row {row_num}: Error: {e}"
                )

        if mismatches and should_notify(notif_config, "error"):
            sendToBot(session,
                      f"MASS DIST MISMATCHES\n"
                      + "\n".join(mismatches))

        if not routes:
            if should_notify(notif_config, "error"):
                sendToBot(session, "MASS DIST: No valid routes found")
        else:
            if should_notify(notif_config, "start"):
                sendToBot(session,
                          f"MASS DIST SCHEDULED\n"
                          f"Source: {source_city['name']}\n"
                          f"{len(routes)} shipment(s)")

            completed = 0
            total = len(routes)

            for idx, (row_num, route, resources, dest_name,
                      player, rx, ry) in enumerate(routes):
                res_desc = ", ".join(
                    f"{addThousandSeparator(resources[i])} "
                    f"{materials_names[i]}"
                    for i in range(len(materials_names))
                    if resources[i] > 0
                )
                print(f"\n  [{idx+1}/{total}] {source_city['name']} -> "
                      f"{dest_name} ({player}) [{rx}:{ry}]")

                session.setStatus(
                    f"[SENDING] Mass Dist [{idx+1}/{total}] "
                    f"-> {dest_name}"
                )

                coords = f"[{rx}:{ry}]"
                result = send_shipment(
                    session, route, useFreighters, notif_config,
                    log_path, "Mass Distribution", coords, player
                )

                if result["success"]:
                    completed += 1
                    print(f"    SUCCESS ({completed}/{total})")
                    # Checkpoint
                    rows[row_num - 1][run_column] = "X"
                    try:
                        write_csv_atomic(csv_path, fieldnames, rows)
                    except Exception as we:
                        print(f"    WARNING: checkpoint write failed: {we}")
                else:
                    print(f"    FAILED: {result['error']}")

            print(f"\n--- Mass Dist: {completed}/{total} sent ---")
            if should_notify(notif_config, "complete"):
                run_done = sum(
                    1 for r in rows
                    if normalize_text(r.get(run_column, "")) == "x"
                )
                sendToBot(session,
                          f"MASS DIST COMPLETE\n"
                          f"Source: {source_city['name']}\n"
                          f"Slot: {run_column[4:]}\n"
                          f"Cycle: {completed}/{total}\n"
                          f"Progress: {run_done}/{len(rows)}")

        # Schedule next cycle
        next_run = datetime.datetime.now() + datetime.timedelta(
            hours=interval_hours
        )
        session.setStatus(
            f"[WAITING] Mass Dist | Next: "
            f"{getDateTime(next_run.timestamp())}"
        )
        sleep_secs = max(
            0, (next_run - datetime.datetime.now()).total_seconds()
        )
        time.sleep(sleep_secs)
