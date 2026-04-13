#! /usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import re
import time
import traceback
from ikabot import config
from ikabot.helpers.gui import banner
from ikabot.helpers.pedirInfo import read, chooseCity
from ikabot.helpers.process import set_child_mode
from ikabot.helpers.signals import setInfoSignal
from ikabot.helpers.varios import decodeUnicodeEscape
from ikabot.helpers.botComm import sendToBot

# =========================================================
# CONFIGURATION
# =========================================================

CARGO_PEOPLE   = 40
CARGO_GOLD     = 9000
TRANSPORTERS   = 3 
CAPACITY       = 5
POLL_SECONDS   = 5.0 

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH   = os.path.join(SCRIPT_DIR, "autoColonize.log")

def _log(msg):
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[{}] {}\n".format(ts, msg))
    except Exception:
        pass

# =========================================================
# DATA HELPERS
# =========================================================

def _get_island_id(session, x, y):
    html = session.get("view=worldmap_iso&islandX={}&islandY={}".format(x, y), noIndex=True)
    m = re.search(r"jsonData\s*=\s*'(.*?)';", html)
    if not m: return None
    try:
        j = json.loads(m.group(1), strict=False)
        return str(j["data"][str(x)][str(y)][0])
    except: return None

def _get_fresh_context(session, city_id, island_id, x, y, position):
    """Mantiene la navegación estable para evitar 'Waiting for data'."""
    session.get("view=city&currentCityId={}&ajax=1".format(city_id), noIndex=True)
    url = (
        "view=colonize&islandId={}&position={}"
        "&backgroundView=island&currentIslandId={}"
        "&templateView=colonize&actionRequest=&ajax=1"
    ).format(island_id, position, island_id)
    resp = session.get(url, noIndex=True)
    
    token = ""
    m = re.search(r'"actionRequest"\s*:\s*"([^"]+)"', resp)
    if m: token = m.group(1)
    
    bg = {}
    try:
        data = json.loads(resp, strict=False)
        for item in data:
            if isinstance(item, list) and item[0] == "updateGlobalData":
                bg = item[1].get("backgroundData", {})
    except: pass
    
    return token, bg

def _get_valid_empty_slots(bg, allow_premium):
    empties = []
    cities = (bg or {}).get("cities", []) or []
    for i, c in enumerate(cities):
        if not isinstance(c, dict): continue
        if c.get("type") in ["buildplace", "empty"] or c.get("id") == -1:
            pos = int(c.get("position") or c.get("pos") or (i + 1))
            if pos == 17 and not allow_premium:
                continue
            empties.append(pos)
    return sorted(set(empties))

# =========================================================
# ACCIÓN ATÓMICA (POSICIÓN - 1)
# =========================================================

def _do_colonize(session, origin_id, island_id, x, y, detected_pos):
    """
    Ejecuta la colonización aplicando el desfase de -1 sobre la posición detectada.
    """
    url_base = session.urlBase.replace("index.php?", "index.php")
    
    # Aplicamos el desfase solicitado
    target_pos = detected_pos - 1
    
    # Obtenemos token para la posición real a enviar
    token, _ = _get_fresh_context(session, origin_id, island_id, x, y, target_pos)
    if not token:
        return False

    params = {
        "action": "transportOperations",
        "function": "startColonization",
        "currentCityId": str(origin_id),
        "ajax": "1"
    }

    payload = {
        "islandId": str(island_id),
        "cargo_people": str(CARGO_PEOPLE),
        "cargo_gold": str(CARGO_GOLD),
        "desiredPosition": str(target_pos), 
        "actionRequest": str(token),
        "resource": "0", "tradegood1": "0", "tradegood2": "0",
        "tradegood3": "0", "tradegood4": "0",
        "capacity": str(CAPACITY), "max_capacity": str(CAPACITY),
        "jetPropulsion": "0", "transporters": str(TRANSPORTERS),
        "position": str(target_pos),
        "destinationIslandId": str(island_id),
        "backgroundView": "island", "currentIslandId": str(island_id),
        "templateView": "colonize", "ajax": "1",
    }

    headers = {
        'Referer': f"{url_base}?view=island&islandId={island_id}&xcoord={x}&ycoord={y}",
        'X-Requested-With': 'XMLHttpRequest'
    }

    response = session.s.post(url_base, params=params, data=payload, headers=headers, verify=config.do_ssl_verify)
    return '"type":10' in response.text

# =========================================================
# MONITORING
# =========================================================

def _watch(session, island_id, x, y, origin_id, origin_name, allow_premium):
    print(f"\nMonitoring island [{x}:{y}]... (Ctrl+C to stop)")
    _log(f"Watch started from {origin_name} with offset -1 logic.")

    while True:
        try:
            ts = time.strftime("%H:%M:%S")
            _, bg = _get_fresh_context(session, origin_id, island_id, x, y, 1)
            
            if not bg:
                print(f"\r{ts} - Waiting for data...", end="")
                time.sleep(POLL_SECONDS)
                continue

            valid_empties = _get_valid_empty_slots(bg, allow_premium)

            if valid_empties:
                detected_pos = valid_empties[0]
                print(f"\n{ts} [!] SPACE DETECTED: Slot {detected_pos} is now free!")
                _log(f"Detected free slot at pos {detected_pos}. Applying offset -1...")
                
                # Intentamos colonizar en la posición ajustada
                if _do_colonize(session, origin_id, island_id, x, y, detected_pos):
                    msg = f"SUCCESS: Mission sent to pos {detected_pos-1} (Detected: {detected_pos}) at [{x}:{y}]"
                    print("\n" + msg)
                    _log(msg)
                    try: sendToBot(session, msg)
                    except: pass
                    return 
                else:
                    print(f"\n{ts} - Mission rejected. Possible offset mismatch. Resuming scan...")
            else:
                print(f"\r{ts} - Island full. Watching [{x}:{y}]...", end="", flush=True)
            
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt: break
        except Exception:
            _log(f"Loop Error: {traceback.format_exc()}")
            time.sleep(POLL_SECONDS)

# =========================================================
# MAIN
# =========================================================

def autoColonize(session, event, stdin_fd, predetermined_input):
    try:
        sys.stdin = os.fdopen(stdin_fd)
        config.predetermined_input = predetermined_input
        banner()
        
        print("--- SELECT ORIGIN CITY ---")
        origin_city = chooseCity(session)
        if not origin_city: return
        
        origin_id = origin_city['id']
        origin_name = decodeUnicodeEscape(origin_city['name'])
        
        x = int(read(msg="Target Island X: ", digit=True))
        y = int(read(msg="Target Island Y: ", digit=True))

        print("\nAllow Position 17 (Premium)? [y/N]")
        allow_premium = read().lower() == 'y'

        island_id = _get_island_id(session, x, y)
        if not island_id: 
            print("Error: Island not found.")
            return
        
        print(f"\nOrigin City: {origin_name}")
        print(f"Target Island: [{x}:{y}]")
        
        set_child_mode(session)
        setInfoSignal(session, f"AutoColonize: {origin_name} -> [{x}:{y}]")
        _watch(session, island_id, x, y, origin_id, origin_name, allow_premium)
    except Exception: 
        _log(f"FATAL ERROR:\n{traceback.format_exc()}")
    finally: 
        event.set()

def GoodLisT(session, event, stdin_fd, predetermined_input):
    return autoColonize(session, event, stdin_fd, predetermined_input)
