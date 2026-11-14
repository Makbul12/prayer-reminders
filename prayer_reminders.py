#!/usr/bin/env python3
"""Short spoken prayer-time reminders on a Google Home / Nest speaker.

Runs on an Android phone in Termux (or any computer on the same Wi-Fi).
It only speaks one short sentence per reminder. It never plays Azan or music.

Commands:
  python prayer_reminders.py run            keep running and announce reminders
  python prayer_reminders.py today          show today's reminder times
  python prayer_reminders.py today 2026-12-25   show another day
  python prayer_reminders.py find           list Google speakers on the Wi-Fi
  python prayer_reminders.py test           speak a test sentence now
  python prayer_reminders.py pause 3        no reminders for 3 days (incl. today)
  python prayer_reminders.py resume         end a pause

Settings: config.json (shared, on GitHub). This phone's speaker: local.json.
"""

import datetime as dt
import functools
import hashlib
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.request

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
LOCAL_PATH = os.path.join(HERE, "local.json")   # this phone only (speaker), not on GitHub
CACHE_DIR = os.path.join(HERE, "cache")
AUDIO_DIR = os.path.join(HERE, "audio")
PAUSE_PATH = os.path.join(HERE, "paused_until.txt")

DEFAULT_CONFIG = {
    # Leave both empty: if only one Google speaker is found, it is picked and saved.
    "speaker_name": "",
    "speaker_ip": "",
    # Bengaluru. method 1 = University of Islamic Sciences, Karachi. school 1 = Hanafi Asr.
    "latitude": 12.9716,
    "longitude": 77.5946,
    "method": 1,
    "school": 1,
    # Shift any time by N minutes to match your masjid timetable, e.g. "Isha": 5
    "adjust_minutes": {"Fajr": 0, "Sunrise": 0, "Dhuhr": 0, "Asr": 0,
                       "Sunset": 0, "Maghrib": 0, "Isha": 0},
    # If India starts Ramadan a day after the calculated Hijri date, set -1.
    "hijri_day_offset": -1,
    "language": "en",
    "accent": "co.in",
    "volume": 0.5,          # 0.0 - 1.0, daytime reminders
    "quiet_volume": 0.3,    # early-morning and night reminders
    "http_port": 8765,
    "reminders": {
        "fajr_start":    {"enabled": True, "text": "Fajr time has started."},
        "fajr_end":      {"enabled": True, "minutes_before": 10,
                          "text": "Fajr ends in {m} minutes."},
        "dhuhr_start":   {"enabled": True, "text": "Dhuhr time has started."},
        "dhuhr_end":     {"enabled": True, "minutes_before": 10,
                          "text": "Dhuhr ends in {m} minutes."},
        "asr_start":     {"enabled": True, "text": "Asr time has started."},
        "asr_end":       {"enabled": True, "minutes_before": 20,
                          "text": "Asr time is ending soon."},
        "maghrib_start": {"enabled": True, "text": "Maghrib time has started."},
        "maghrib_end":   {"enabled": True, "minutes_before": 10,
                          "text": "Maghrib ends in {m} minutes."},
        "isha_start":    {"enabled": True, "text": "Isha time has started."},
        "isha_before_sleep": {"enabled": False, "time": "23:00",
                              "text": "Remember to pray Isha before sleeping."},
        "jumuah_morning": {"enabled": False, "time": "09:00",
                           "text": "It's Jumu'ah today. Remember Surah Al-Kahf."},
        "jummah_soon":   {"enabled": False, "jummah_time": "13:15", "minutes_before": 30,
                          "text": "Jummah prayer in {m} minutes."},
        "sunnah_fast_tomorrow": {"enabled": False, "time": "21:00",
                                 "text": "Tomorrow is a Sunnah fasting day."},
        # Ramadan reminders switch on by themselves only during Ramadan.
        "suhoor_end":    {"enabled": True, "minutes_before": 15,
                          "text": "Suhoor ends in {m} minutes."},
        "iftar":         {"enabled": True, "text": "It's time for Iftar."},
    },
}

QUIET_KEYS = {"fajr_start", "fajr_end", "suhoor_end", "isha_before_sleep",
              "sunnah_fast_tomorrow"}


def log(msg):
    print(f"[{dt.datetime.now(IST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- config

def _merge(defaults, current):
    out = dict(defaults)
    for k, v in current.items():
        if isinstance(v, dict) and isinstance(defaults.get(k), dict):
            out[k] = _merge(defaults[k], v)
        else:
            out[k] = v
    return out


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def save_speaker(name, ip):
    _write_json(LOCAL_PATH, {"speaker_name": name, "speaker_ip": ip})


def load_config():
    if not os.path.exists(CONFIG_PATH):
        _write_json(CONFIG_PATH, DEFAULT_CONFIG)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = _merge(DEFAULT_CONFIG, json.load(f))
    if os.path.exists(LOCAL_PATH):
        with open(LOCAL_PATH, encoding="utf-8") as f:
            cfg = _merge(cfg, json.load(f))
    return cfg


def config_stamp():
    return tuple(os.path.getmtime(p) if os.path.exists(p) else 0
                 for p in (CONFIG_PATH, LOCAL_PATH))


def self_update():
    """Pull new code/settings from GitHub. Restart if the program itself changed."""
    if not os.path.isdir(os.path.join(HERE, ".git")):
        return
    import subprocess
    def head():
        return subprocess.run(["git", "-C", HERE, "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    before = head()
    r = subprocess.run(["git", "-C", HERE, "pull", "--ff-only", "-q"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        log(f"Update check failed: {r.stderr.strip()[:200]}")
        return
    after = head()
    if before != after:
        log(f"Updated from GitHub ({before[:7]} -> {after[:7]}). Restarting.")
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__), "run"])


# ---------------------------------------------------------------- prayer times

def fetch_month(cfg, year, month):
    """Prayer times for a whole month, cached on disk so it works offline."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = f"{year}-{month:02d}_{cfg['latitude']}_{cfg['longitude']}_m{cfg['method']}_s{cfg['school']}"
    path = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    url = (f"https://api.aladhan.com/v1/calendar/{year}/{month}"
           f"?latitude={cfg['latitude']}&longitude={cfg['longitude']}"
           f"&method={cfg['method']}&school={cfg['school']}")
    req = urllib.request.Request(url, headers={"User-Agent": "prayer-reminders/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("code") != 200:
        raise RuntimeError(f"prayer-time service error: {data.get('status')}")
    days = {}
    for d in data["data"]:
        day = int(d["date"]["gregorian"]["date"][:2])
        days[str(day)] = {
            "timings": {k: v.split(" ")[0] for k, v in d["timings"].items()},
            "hijri_month": int(d["date"]["hijri"]["month"]["number"]),
        }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(days, f)
    os.replace(tmp, path)
    return days


def day_info(cfg, date):
    return fetch_month(cfg, date.year, date.month)[str(date.day)]


def is_ramadan(cfg, date):
    shifted = date + dt.timedelta(days=int(cfg.get("hijri_day_offset", 0)))
    return day_info(cfg, shifted)["hijri_month"] == 9


def _at(date, hhmm, delta_min=0):
    h, m = (int(x) for x in hhmm.split(":")[:2])
    return dt.datetime(date.year, date.month, date.day, h, m, tzinfo=IST) + \
        dt.timedelta(minutes=delta_min)


def build_schedule(cfg, date):
    """Return [(time, key, text, volume)] for one day, sorted by time."""
    t = day_info(cfg, date)["timings"]
    adj = cfg.get("adjust_minutes", {})
    p = {name: _at(date, t[name], adj.get(name, 0))
         for name in ("Fajr", "Sunrise", "Dhuhr", "Asr", "Sunset", "Maghrib", "Isha")}
    R = cfg["reminders"]
    events = []

    def add(key, when):
        r = R.get(key)
        if not r or not r.get("enabled"):
            return
        text = r["text"].format(m=r.get("minutes_before", ""))
        vol = cfg["quiet_volume"] if key in QUIET_KEYS else cfg["volume"]
        events.append((when, key, text, vol))

    def before(key, when):
        return when - dt.timedelta(minutes=int(R.get(key, {}).get("minutes_before", 0)))

    ramadan = is_ramadan(cfg, date)

    add("fajr_start", p["Fajr"])
    add("fajr_end", before("fajr_end", p["Sunrise"]))
    add("dhuhr_start", p["Dhuhr"])
    add("dhuhr_end", before("dhuhr_end", p["Asr"]))
    add("asr_start", p["Asr"])
    add("asr_end", before("asr_end", p["Sunset"]))
    if ramadan and R.get("iftar", {}).get("enabled"):
        add("iftar", p["Maghrib"])
    else:
        add("maghrib_start", p["Maghrib"])
    add("maghrib_end", before("maghrib_end", p["Isha"]))
    add("isha_start", p["Isha"])
    if ramadan:
        add("suhoor_end", before("suhoor_end", p["Fajr"]))

    if R.get("isha_before_sleep", {}).get("enabled"):
        add("isha_before_sleep", _at(date, R["isha_before_sleep"]["time"]))
    wd = date.weekday()  # Monday=0 ... Sunday=6
    if wd == 4:  # Friday
        if R.get("jumuah_morning", {}).get("enabled"):
            add("jumuah_morning", _at(date, R["jumuah_morning"]["time"]))
        if R.get("jummah_soon", {}).get("enabled"):
            add("jummah_soon", before("jummah_soon", _at(date, R["jummah_soon"]["jummah_time"])))
    if wd in (6, 2) and R.get("sunnah_fast_tomorrow", {}).get("enabled"):  # Sun, Wed nights
        add("sunnah_fast_tomorrow", _at(date, R["sunnah_fast_tomorrow"]["time"]))

    events.sort(key=lambda e: e[0])
    return events


# ---------------------------------------------------------------- pause

def paused_on(date):
    try:
        with open(PAUSE_PATH, encoding="utf-8") as f:
            until = dt.date.fromisoformat(f.read().strip())
        return date <= until
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- audio

def tts_file(cfg, text):
    """Make (once) and return the file name of an MP3 of the text."""
    os.makedirs(AUDIO_DIR, exist_ok=True)
    h = hashlib.sha1(f"{cfg['language']}|{cfg['accent']}|{text}".encode()).hexdigest()[:16]
    name = h + ".mp3"
    path = os.path.join(AUDIO_DIR, name)
    if not os.path.exists(path):
        from gtts import gTTS
        tmp = path + ".tmp"
        gTTS(text, lang=cfg["language"], tld=cfg["accent"]).save(tmp)
        os.replace(tmp, path)
    return name


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def start_http(port):
    os.makedirs(AUDIO_DIR, exist_ok=True)
    handler = functools.partial(_QuietHandler, directory=AUDIO_DIR)
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def local_ip(target):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        return s.getsockname()[0]
    finally:
        s.close()


# ---------------------------------------------------------------- speaker

def discover(known_ip=""):
    import pychromecast
    hosts = [known_ip] if known_ip else None
    casts, browser = pychromecast.get_chromecasts(known_hosts=hosts, timeout=15)
    return casts, browser


def find_speaker(cfg):
    casts, browser = discover(cfg.get("speaker_ip", ""))
    name, ip = cfg.get("speaker_name", ""), cfg.get("speaker_ip", "")
    chosen = None
    for c in casts:
        if name and c.cast_info.friendly_name == name:
            chosen = c
            break
        if not name and ip and c.cast_info.host == ip:
            chosen = c
            break
    if chosen is None and not name and len(casts) == 1:
        chosen = casts[0]
        cfg["speaker_name"] = chosen.cast_info.friendly_name
        cfg["speaker_ip"] = chosen.cast_info.host
        save_speaker(cfg["speaker_name"], cfg["speaker_ip"])
        log(f"Using speaker '{cfg['speaker_name']}' ({cfg['speaker_ip']}) and saved it.")
    if chosen is None:
        browser.stop_discovery()
        found = ", ".join(c.cast_info.friendly_name for c in casts) or "none"
        raise RuntimeError(f"speaker not found (looking for '{name or ip}'; found: {found})")
    return chosen, browser


def announce(cfg, text, volume):
    fname = tts_file(cfg, text)
    cast, browser = find_speaker(cfg)
    try:
        cast.wait(timeout=20)
        url = f"http://{local_ip(cast.cast_info.host)}:{cfg['http_port']}/{fname}"
        old_volume = cast.status.volume_level if cast.status else None
        if volume is not None:
            cast.set_volume(volume)
            time.sleep(0.5)
        mc = cast.media_controller
        mc.play_media(url, "audio/mpeg")
        mc.block_until_active(timeout=20)
        started, seen_playing = time.time(), False
        while time.time() - started < 30:
            state = mc.status.player_state
            if state in ("PLAYING", "BUFFERING"):
                seen_playing = True
            elif seen_playing:
                break
            time.sleep(0.5)
        time.sleep(0.5)
        cast.quit_app()
        if volume is not None and old_volume is not None:
            time.sleep(1)
            cast.set_volume(old_volume)
    finally:
        try:
            cast.disconnect(timeout=5)
        finally:
            browser.stop_discovery()


def announce_with_retry(cfg, text, volume, tries=3):
    for attempt in range(1, tries + 1):
        try:
            announce(cfg, text, volume)
            log(f"Said: {text}")
            return True
        except Exception as e:  # keep running whatever happens
            log(f"Attempt {attempt} failed: {e}")
            time.sleep(20)
    return False


# ---------------------------------------------------------------- commands

def cmd_run():
    cfg = load_config()
    try:
        start_http(cfg["http_port"])
    except OSError:
        log("Already running (port in use). Exiting so reminders are not doubled.")
        return 1
    log("Prayer reminders started.")
    try:  # make all sentences now, while internet is up
        for r in cfg["reminders"].values():
            tts_file(cfg, r["text"].format(m=r.get("minutes_before", "")))
    except Exception as e:
        log(f"Could not prepare voice files yet ({e}); will retry when needed.")

    fired, schedules, last_fetch_warning, last_update = set(), {}, 0, 0
    while True:
        if time.time() - last_update > 1800:  # check GitHub every 30 minutes
            last_update = time.time()
            try:
                self_update()
            except Exception as e:
                log(f"Update check failed: {e}")
        now = dt.datetime.now(IST)
        today = now.date()
        cfg = load_config()
        cache_key = (today, config_stamp())
        try:
            if cache_key not in schedules:
                schedules = {cache_key: build_schedule(cfg, today)}
                log("Today: " + ", ".join(f"{w:%H:%M} {k}" for w, k, _, _ in schedules[cache_key]))
            events = schedules[cache_key]
            if today.day >= 20:  # fetch next month early, in case internet is down later
                nxt = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
                fetch_month(cfg, nxt.year, nxt.month)
        except Exception as e:
            if time.time() - last_fetch_warning > 3600:
                log(f"Could not get prayer times: {e}")
                last_fetch_warning = time.time()
            time.sleep(60)
            continue

        for when, key, text, vol in events:
            mark = (today, key)
            if mark in fired:
                continue
            if when <= now < when + dt.timedelta(minutes=5):
                fired.add(mark)
                if paused_on(today):
                    log(f"Paused, skipped: {text}")
                else:
                    announce_with_retry(cfg, text, vol)
        fired = {f for f in fired if f[0] == today}
        time.sleep(15)


def cmd_today(arg=None):
    cfg = load_config()
    date = dt.date.fromisoformat(arg) if arg else dt.datetime.now(IST).date()
    print(f"Reminders for {date:%A %d %B %Y}"
          f"{' (Ramadan)' if is_ramadan(cfg, date) else ''}"
          f"{' (PAUSED)' if paused_on(date) else ''}:")
    for when, key, text, vol in build_schedule(cfg, date):
        print(f"  {when:%H:%M}  {text}   [{key}, volume {vol}]")


def cmd_find():
    casts, browser = discover()
    browser.stop_discovery()
    if not casts:
        print("No Google speakers found. Is this phone on the same Wi-Fi as the speaker?")
    for c in casts:
        i = c.cast_info
        print(f"  {i.friendly_name!r}  ip={i.host}  model={i.model_name}")


def cmd_test():
    cfg = load_config()
    try:
        start_http(cfg["http_port"])
    except OSError:
        pass  # the running service already serves the audio folder
    ok = announce_with_retry(cfg, "Assalamu Alaikum. Prayer reminders are working.",
                             cfg["volume"], tries=1)
    return 0 if ok else 1


def cmd_pause(days):
    until = dt.datetime.now(IST).date() + dt.timedelta(days=int(days) - 1)
    with open(PAUSE_PATH, "w", encoding="utf-8") as f:
        f.write(until.isoformat())
    print(f"Reminders paused until the end of {until:%A %d %B}.")


def cmd_resume():
    if os.path.exists(PAUSE_PATH):
        os.remove(PAUSE_PATH)
    print("Reminders resumed.")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        return cmd_run()
    if cmd == "today":
        return cmd_today(argv[2] if len(argv) > 2 else None)
    if cmd == "find":
        return cmd_find()
    if cmd == "test":
        return cmd_test()
    if cmd == "pause":
        return cmd_pause(argv[2] if len(argv) > 2 else 1)
    if cmd == "resume":
        return cmd_resume()
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
