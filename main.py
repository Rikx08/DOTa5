"""
Дать пять — LAN-приложение (как жест в Dota).
Жмёшь горячую клавишу -> рассылаешь пакет в локальную сеть (в т.ч. через Radmin VPN) ->
если кто-то ещё нажал в то же окно времени -> у всех участников на экране всплывает
анимация + звук поверх всех окон.

Запуск:  python main.py
Настройки — в файле config.json (горячая клавиша, имя, сеть, звук).
"""

import json
import os
import socket
import sys
import threading
import time
import traceback
import queue
import uuid
import io
import wave
import math
import random
import struct

import tkinter as tk
from tkinter import messagebox

# --- ВАЖНО: exe, собранный с --noconsole, не имеет stdout/stderr (они None).
# Без этой заглушки любой print() сразу же роняет программу без единого
# сообщения об ошибке. Перенаправляем вывод в файл рядом с exe.
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
LOG_PATH = os.path.join(APP_DIR, "error.log")
if sys.stdout is None or sys.stderr is None:
    _log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    sys.stdout = _log
    sys.stderr = _log

try:
    import keyboard  # глобальный хоткей, работает даже когда окно не в фокусе
except ImportError:
    print("Не найден модуль 'keyboard'. Установи: pip install keyboard")
    sys.exit(1)

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False  # не Windows — используем запасной вариант через print/beep



def synth_clap_wav(duration=0.18, sample_rate=44100):
    """Генерирует короткий 'хлопок' в памяти (WAV-байты), без внешних файлов."""
    n_samples = int(duration * sample_rate)
    frames = bytearray()
    for i in range(n_samples):
        t = i / sample_rate
        envelope = math.exp(-t * 32)          # быстрое затухание, как у хлопка
        noise = random.uniform(-1.0, 1.0)     # шумовая составляющая
        tone = math.sin(2 * math.pi * 180 * t) * 0.35  # немного "тела" звука
        sample = (noise * 0.65 + tone) * envelope
        sample = max(-1.0, min(1.0, sample))
        frames += struct.pack("<h", int(sample * 32767))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def synth_tick_wav(duration=0.06, sample_rate=44100, freq=700):
    """Короткий 'тик' — кто-то отправил пять."""
    n_samples = int(duration * sample_rate)
    frames = bytearray()
    for i in range(n_samples):
        t = i / sample_rate
        envelope = math.exp(-t * 60)
        sample = math.sin(2 * math.pi * freq * t) * envelope * 0.5
        frames += struct.pack("<h", int(sample * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def synth_no_match_wav(duration=0.4, sample_rate=44100):
    """Грустное нисходящее 'вwomp' — никто не ответил на пять."""
    n_samples = int(duration * sample_rate)
    frames = bytearray()
    for i in range(n_samples):
        t = i / sample_rate
        freq = 480 - (480 - 150) * (t / duration)
        envelope = math.exp(-t * 4.5)
        sample = math.sin(2 * math.pi * freq * t) * envelope * 0.6
        frames += struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


_CLAP_WAV_BYTES = None
_TICK_WAV_BYTES = None
_NO_MATCH_WAV_BYTES = None


def get_clap_wav_bytes():
    global _CLAP_WAV_BYTES
    if _CLAP_WAV_BYTES is None:
        _CLAP_WAV_BYTES = synth_clap_wav()
    return _CLAP_WAV_BYTES


def get_tick_wav_bytes():
    global _TICK_WAV_BYTES
    if _TICK_WAV_BYTES is None:
        _TICK_WAV_BYTES = synth_tick_wav()
    return _TICK_WAV_BYTES


def get_no_match_wav_bytes():
    global _NO_MATCH_WAV_BYTES
    if _NO_MATCH_WAV_BYTES is None:
        _NO_MATCH_WAV_BYTES = synth_no_match_wav()
    return _NO_MATCH_WAV_BYTES

CONFIG_PATH = os.path.join(APP_DIR, "config.json")
CLIENT_ID = str(uuid.uuid4())

DEFAULT_CONFIG = {
    "username": os.environ.get("USERNAME") or os.environ.get("USER") or "player",
    "hotkey": "ctrl+alt+x",
    "port": 47990,
    "window_ms": 4000,          # окно синхронизации между нажатиями (мс)
    "peers": [],                # можно явно указать IP друзей в Radmin-сети, например ["26.10.20.30"]
    "extra_broadcast": [],      # доп. broadcast-адреса, если авто-определение не сработало
    # Если файла нет рядом с exe — играет синтезированный звук по умолчанию.
    "sound_send": "send.wav",        # звук: кто-то отправил пять
    "sound_match": "match.wav",      # звук: мэтч произошёл (хлопок)
    "sound_no_match": "no_match.wav",  # звук: никто не ответил на пять
    "animation_seconds": 1.2,
    "font_family": "Segoe UI Emoji",
    "cooldown_seconds": 60,      # после успешного мэтча столько секунд нельзя хлопать снова
    "overlay_width": 1920,        # ширина всплывающего окна с текстом (px)
    "overlay_height": 1080,       # высота всплывающего окна с текстом (px)
    "text_color": "#8E0039",     # цвет текста в оверлее, например "#ff6b00"
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # Совместимость со старым конфигом, где был один общий "sound_file"
    if "sound_file" in cfg and "sound_match" not in cfg:
        cfg["sound_match"] = cfg["sound_file"]
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg


CFG = load_config()


# ---------------------------------------------------------------------------
# Сеть: UDP broadcast + прямая рассылка на peers (для Radmin VPN понадёжнее)
# ---------------------------------------------------------------------------

def local_broadcast_addresses():
    addrs = set(["255.255.255.255"])
    try:
        host_ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except Exception:
        host_ips = []
    for ip in host_ips:
        parts = ip.split(".")
        if len(parts) == 4:
            addrs.add(".".join(parts[:3] + ["255"]))
    for extra in CFG.get("extra_broadcast", []):
        addrs.add(extra)
    return addrs


def make_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("0.0.0.0", CFG["port"]))
    return s


def send_five():
    msg = json.dumps({
        "type": "five",
        "user": CFG["username"],
        "ts": time.time(),
        "id": CLIENT_ID,
    }).encode("utf-8")

    targets = list(local_broadcast_addresses()) + list(CFG.get("peers", []))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for addr in targets:
        try:
            sock.sendto(msg, (addr, CFG["port"]))
        except OSError:
            pass
    sock.close()


def listen_loop(event_queue: "queue.Queue"):
    sock = make_socket()
    while True:
        try:
            data, _addr = sock.recvfrom(2048)
            msg = json.loads(data.decode("utf-8"))
            if msg.get("type") == "five" and msg.get("id") != CLIENT_ID:
                event_queue.put(("network", msg["user"], msg["ts"]))
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Логика синхронизации: у кого совпали нажатия за последние window_ms?
# ---------------------------------------------------------------------------

class Matcher:
    def __init__(self, window_ms):
        self.window = window_ms / 1000.0
        self.recent = {}  # username -> timestamp
        self.lock = threading.Lock()

    def register(self, user, ts):
        with self.lock:
            self.recent[user] = ts
            now = time.time()
            active = {u: t for u, t in self.recent.items() if now - t <= self.window}
            self.recent = active
            if len(active) >= 2:
                self.recent = {}  # сброс, чтобы не сработало повторно на те же события
                return list(active.keys())
        return None


# ---------------------------------------------------------------------------
# Звук
# ---------------------------------------------------------------------------

SOUND_CONFIG_KEY = {
    "send": "sound_send",
    "match": "sound_match",
    "no_match": "sound_no_match",
}
SOUND_SYNTH_FN = {
    "send": get_tick_wav_bytes,
    "match": get_clap_wav_bytes,
    "no_match": get_no_match_wav_bytes,
}


def play_sound(kind="send"):
    """kind: 'send' (кто-то отправил пять), 'match' (произошёл хлопок),
    'no_match' (никто не ответил)."""
    if not HAS_WINSOUND:
        print("\a")  # запасной вариант на не-Windows системах
        return

    filename = CFG.get(SOUND_CONFIG_KEY.get(kind, "sound_send"), "")
    sound_path = os.path.join(APP_DIR, filename) if filename else ""
    synth_fn = SOUND_SYNTH_FN.get(kind, get_tick_wav_bytes)

    def _play():
        try:
            if filename and os.path.exists(sound_path):
                print(f"[sound:{kind}] играю файл {sound_path}")
                # Файл на диске — можно асинхронно, поток гарантированно жив всё время.
                winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                print(f"[sound:{kind}] файл '{filename}' не найден по пути {sound_path}, играю синтезированный звук")
                # winsound запрещает SND_MEMORY + SND_ASYNC одновременно (буфер может
                # быть удалён во время игры). Мы и так в отдельном потоке — играем синхронно.
                winsound.PlaySound(synth_fn(), winsound.SND_MEMORY)
        except Exception as e:
            print(f"[sound:{kind}] ОШИБКА воспроизведения:", repr(e))

    threading.Thread(target=_play, daemon=True).start()





# ---------------------------------------------------------------------------
# Оверлей (поверх всех окон, без рамки, полупрозрачный фон)
# ---------------------------------------------------------------------------

class Overlay:
    def __init__(self, root, font_family):
        self.root = root
        self.font_family = font_family

    def _make_window(self, text, start_size, end_size, duration, alpha_peak=1.0):
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        # WS_EX_NOACTIVATE применяем до показа окна, чтобы оно вообще ни разу
        # не стало "активным" и не отняло фокус у игры (из-за чего игра сворачивается).
        self._apply_noactivate(win)
        try:
            win.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        win.config(bg="black")
        try:
            win.attributes("-transparentcolor", "black")  # Windows: делает фон прозрачным
        except tk.TclError:
            pass

        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w = CFG.get("overlay_width", 700)
        h = CFG.get("overlay_height", 320)
        win.geometry(f"{w}x{h}+{(sw - w)//2}+{(sh - h)//3}")

        label = tk.Label(win, text=text, fg=CFG.get("text_color", "#ffffff"), bg="black",
                          font=(self.font_family, start_size),
                          wraplength=w - 40, justify="center")
        label.pack(expand=True, fill="both")

        self._make_clickthrough(win)
        self._topmost_no_activate(win)

        steps = 18
        for i in range(steps + 1):
            t = i / steps
            size = int(start_size + (end_size - start_size) * min(1.0, t * 1.6))
            alpha = alpha_peak * (t if t < 0.25 else (1.0 if t < 0.75 else (1 - t) * 4))
            alpha = max(0.0, min(1.0, alpha))
            win.after(int(duration * 1000 * t), lambda s=size, a=alpha: self._update(win, label, s, a))
        win.after(int(duration * 1000) + 50, win.destroy)

    def _update(self, win, label, size, alpha):
        try:
            label.config(font=(self.font_family, size))
            win.attributes("-alpha", alpha)
        except tk.TclError:
            pass

    def _make_clickthrough(self, win):
        """Чтобы оверлей не мешал кликать по игре под ним (только Windows)."""
        if os.name != "nt":
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT
            )
        except Exception:
            pass

    def _apply_noactivate(self, win):
        """WS_EX_NOACTIVATE — окно никогда не становится 'активным', поэтому
        игра под ним не теряет фокус и не сворачивается (только Windows)."""
        if os.name != "nt":
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)
        except Exception:
            pass

    def _topmost_no_activate(self, win):
        """Ставим окно поверх всех окон через SetWindowPos с SWP_NOACTIVATE —
        в отличие от Tk-атрибута '-topmost', это гарантированно не активирует окно."""
        if os.name != "nt":
            return
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            HWND_TOPMOST = -1
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_NOACTIVATE = 0x0010
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
            )
        except Exception:
            win.attributes("-topmost", True)  # запасной вариант, если win32-вызов не сработал

    def announce(self, name, mine):
        text = "✋ Я дал(а) пять!" if mine else f"✋ {name} дал(а) пять!"
        self._make_window(text, 45, 70, 0.7, alpha_peak=0.9)

    def combo(self, names):
        self._make_window("🙌 " + " + ".join(names), 70, 130, CFG["animation_seconds"], alpha_peak=1.0)

    def notice(self, text):
        self._make_window(text, 32, 40, 0.9, alpha_peak=0.8)


# ---------------------------------------------------------------------------
# Главный цикл
# ---------------------------------------------------------------------------

def main():
    event_queue: "queue.Queue" = queue.Queue()
    matcher = Matcher(CFG["window_ms"])

    threading.Thread(target=listen_loop, args=(event_queue,), daemon=True).start()

    root = tk.Tk()
    root.withdraw()
    overlay = Overlay(root, CFG.get("font_family", "Segoe UI Emoji"))

    cooldown_seconds = CFG.get("cooldown_seconds", 60)
    window_s = CFG["window_ms"] / 1000.0
    state = {"cooldown_until": 0.0, "pending_ts": None, "pending_matched": False}

    def on_hotkey():
        now = time.time()
        remaining = state["cooldown_until"] - now
        if remaining > 0:
            overlay.notice(f"⏳ Подожди {int(remaining) + 1} сек")
            return  # во время кулдауна нельзя ни хлопать, ни отправлять

        state["pending_ts"] = now
        state["pending_matched"] = False
        event_queue.put(("local", CFG["username"], now))
        send_five()

        def check_no_match(my_ts=now):
            if state["pending_ts"] == my_ts and not state["pending_matched"]:
                play_sound("no_match")
                overlay.notice("😶 Никто не ответил")
                state["pending_ts"] = None

        root.after(int(window_s * 1000) + 200, check_no_match)

    keyboard.add_hotkey(CFG["hotkey"], on_hotkey)
    print(f"Готово. Горячая клавиша: {CFG['hotkey']}  |  Имя: {CFG['username']}  |  Порт: {CFG['port']}")
    print(f"HAS_WINSOUND={HAS_WINSOUND}  APP_DIR={APP_DIR}")
    print("Настройки — в config.json. Сверни это окно и играй.")

    def poll_queue():
        try:
            while True:
                kind, user, ts = event_queue.get_nowait()
                # Звук "send" и уведомление проигрываются у всех при КАЖДОМ нажатии,
                # без необходимости совпадения по времени.
                play_sound("send")
                overlay.announce(user, mine=(kind == "local"))
                matched = matcher.register(user, ts)
                if matched:
                    overlay.combo(matched)
                    play_sound("match")
                    if CFG["username"] in matched:
                        state["pending_matched"] = True
                        state["cooldown_until"] = time.time() + cooldown_seconds
        except queue.Empty:
            pass
        root.after(40, poll_queue)

    root.after(40, poll_queue)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        print(tb)
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "Дать пять — ошибка",
                "Приложение упало с ошибкой. Подробности записаны в error.log рядом с exe:\n\n" + tb[:1500],
            )
        except Exception:
            pass