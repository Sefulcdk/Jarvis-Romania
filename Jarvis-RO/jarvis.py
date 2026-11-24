import asyncio
import datetime
import io
import json
import math
import os
import queue
import random
import re
import subprocess
import threading
import time
import tkinter as tk
import unicodedata
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BLUE = "#63dfff"
BG = "#060e1c"
WAKE = re.compile(r"^(?:(?:hei )?jarvis trezeste[ -]?te|salut jarvis)(?:\s+|$)")
GREETINGS = (
    "Salut, șefule! Te ascult.",
    "La ordine, regele meu!",
    "Servus, șefule! Cu ce te ajut?",
    "Bine ai venit, șefule! Sunt gata.",
)
ROMANIAN_VOICE = "ro-RO-EmilNeural"


async def synthesize_reply(message):
    import edge_tts

    chunks = bytearray()
    async for chunk in edge_tts.Communicate(message, ROMANIAN_VOICE).stream():
        if chunk["type"] == "audio":
            chunks.extend(chunk["data"])
    if not chunks:
        raise RuntimeError("Sinteza vocală nu a returnat sunet.")
    return bytes(chunks)


def play_neural_reply(message):
    import av
    import numpy as np
    import sounddevice

    audio = asyncio.run(asyncio.wait_for(synthesize_reply(message), timeout=12))
    with av.open(io.BytesIO(audio), format="mp3") as source:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        frames = []
        for frame in source.decode(audio=0):
            frames.extend(result.to_ndarray().ravel() for result in resampler.resample(frame))
        frames.extend(result.to_ndarray().ravel() for result in resampler.resample(None))
    if not frames:
        raise RuntimeError("Fluxul vocal nu conține cadre audio.")
    sounddevice.play(np.concatenate(frames), samplerate=48000, blocking=True)


TTS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$tts = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $ro = $tts.GetInstalledVoices() | Where-Object {
        $_.Enabled -and $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq 'ro' -and $_.VoiceInfo.Gender -eq 'Male'
    } | Select-Object -First 1
    if (-not $ro) { Write-Output 'NO_ROMANIAN_VOICE'; exit 3 }
    $tts.SelectVoice($ro.VoiceInfo.Name)
    $tts.SetOutputToDefaultAudioDevice()
    $tts.Speak([Console]::In.ReadToEnd())
} finally {
    $tts.Dispose()
}
"""


def normalize(text):
    return " ".join("".join(c for c in unicodedata.normalize("NFD", text.casefold())
                            if not unicodedata.combining(c)).split())


ONLINE_COMMAND_FORMS = (
    "deschide {target}",
    "pornește {target}",
    "lansează {target}",
    "intră pe {target}",
    "du-mă pe {target}",
    "mergi pe {target}",
    "accesează {target}",
    "mergi la {target}",
    "du-mă la {target}",
    "navighează la {target}",
    "vizitează {target}",
    "deschide site-ul {target}",
    "deschide pagina {target}",
    "te rog deschide {target}",
    "vreau să deschizi {target}",
)
LOCAL_COMMAND_FORMS = (
    "deschide {target}",
    "pornește {target}",
    "lansează {target}",
    "rulează {target}",
    "deschide aplicația {target}",
    "pornește aplicația {target}",
    "lansează aplicația {target}",
    "deschide programul {target}",
    "pornește programul {target}",
    "lansează programul {target}",
    "te rog deschide {target}",
    "te rog pornește {target}",
    "te rog lansează {target}",
    "vreau să deschizi {target}",
    "poți deschide {target}",
)
COMMAND_ENDINGS = (
    "",
    " te rog",
    " te rog frumos",
    " acum",
    " te rog acum",
    " acum te rog",
    " pentru mine",
    " imediat",
    " te rog imediat",
    " chiar acum",
)


def expand_voice_commands(commands, total=9000):
    targets = [
        (phrase[len("deschide "):], args)
        for phrase, args in commands.items()
        if phrase.startswith("deschide ")
    ]
    extras = len(commands) - len(targets)
    target_total = total - extras
    if not targets or target_total < len(targets) or target_total > len(targets) * 150:
        raise ValueError("Numărul de comenzi configurate nu permite generarea exactă a 9.000 de expresii.")

    per_target, remainder = divmod(target_total, len(targets))
    expanded = dict(commands)
    for index, (target, args) in enumerate(targets):
        online = len(args) > 1 and args[0].casefold() == "explorer.exe" and args[1].startswith(
            ("https://", "http://")
        )
        forms = ONLINE_COMMAND_FORMS if online else LOCAL_COMMAND_FORMS
        variants = []
        seen = set()
        for form in forms:
            for ending in COMMAND_ENDINGS:
                phrase = normalize(form.format(target=target) + ending)
                if phrase not in seen:
                    variants.append(phrase)
                    seen.add(phrase)

        wanted = per_target + (index < remainder)
        if len(variants) < wanted:
            raise ValueError(f"Nu sunt suficiente formulări unice pentru «{target}».")
        for phrase in variants[:wanted]:
            if phrase in expanded and expanded[phrase] != args:
                raise ValueError(f"Formulare duplicată cu acțiuni diferite: {phrase}")
            expanded[phrase] = args

    if len(expanded) != total:
        raise ValueError(f"Generarea a produs {len(expanded)} comenzi în loc de {total}.")
    return expanded


def internet_available():
    try:
        with urllib.request.urlopen("https://www.gstatic.com/generate_204", timeout=4) as response:
            return response.status == 204
    except OSError:
        return False


class Jarvis:
    def __init__(self, window):
        self.window = window
        self.events = queue.Queue()
        self.replies = queue.Queue(maxsize=8)
        self.speaking = threading.Event()
        threading.Thread(target=self.play_replies, daemon=True).start()
        self.online = False
        self.awake_until = None
        self.voice_thread = None
        self.animation_start = time.monotonic()
        self.voice_level = 0.0
        self.core_flash = 0.0
        self.commands = {
            "deschide calculatorul": ["calc.exe"],
            "deschide notepad": ["notepad.exe"],
            "deschide exploratorul": ["explorer.exe"],
            "deschide paint": ["mspaint.exe"],
        }
        path = ROOT / "comenzi.json"
        try:
            if path.exists():
                entries = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(entries, dict):
                    raise ValueError("Formatul trebuie să fie un obiect JSON.")
                custom = {}
                for phrase, args in entries.items():
                    key = normalize(phrase)
                    if not key or not isinstance(args, list) or not args or not all(
                        isinstance(arg, str) and arg.strip() for arg in args
                    ):
                        raise ValueError(f"Comandă invalidă: {phrase}")
                    custom[key] = args
                self.commands = expand_voice_commands({**self.commands, **custom})
        except (OSError, ValueError) as error:
            self.events.put(("status", f"Eroare în comenzi.json: {error}"))

        window.title("JARVIS • Asistent în română")
        window.geometry("900x730")
        window.minsize(670, 690)
        window.configure(bg=BG)
        tk.Label(window, text="J A R V I S", font=("Segoe UI", 30, "bold"),
                 bg=BG, fg="#e5f7ff").pack(pady=(20, 0))
        self.connection = tk.Label(window, text="VERIFIC CONEXIUNEA", bg=BG, fg=BLUE)
        self.connection.pack()
        self.canvas = tk.Canvas(window, height=390, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="x", padx=18, pady=(5, 0))
        self.status = tk.Label(window, text="„Jarvis, trezește-te”",
                               bg=BG, fg="#e5f7ff", wraplength=550, font=("Segoe UI", 12))
        self.status.pack(pady=5)
        row = tk.Frame(window, bg=BG)
        row.pack(fill="x", padx=36, pady=10)
        self.entry = tk.Entry(row, bg="#152639", fg="white", insertbackground="white",
                              relief="flat", font=("Segoe UI", 14))
        self.entry.pack(side="left", fill="x", expand=True, ipady=8)
        self.entry.bind("<Return>", self.submit)
        self.button(row, "Execută", self.submit).pack(side="left", padx=(8, 0))
        controls = tk.Frame(window, bg=BG)
        controls.pack()
        self.button(controls, "Activează microfonul", self.start_voice).pack(side="left", padx=4)
        self.button(controls, "Test sunet",
                    lambda: self.reply("Salut, șefule! Sunt Jarvis.")
                    ).pack(side="left", padx=4)
        self.button(controls, "Comenzi", lambda: self.execute("ajutor")).pack(side="left", padx=4)
        self.web = tk.Frame(window, bg=BG)
        self.button(self.web, "🌐 Caută pe Google", lambda: self.open_web("https://www.google.com")).pack(side="left", padx=4)
        self.button(self.web, "▶ YouTube", lambda: self.open_web("https://www.youtube.com")).pack(side="left", padx=4)
        self.entry.focus_set()
        self.window.after(0, self.poll)
        self.window.after(0, self.animate)
        self.window.after(0, self.check_network)
        self.window.after(0, self.start_voice)

    @staticmethod
    def button(parent, title, action):
        return tk.Button(parent, text=title, command=action, bg="#142c41", fg="#e5f7ff",
                         activebackground="#245273", activeforeground="white",
                         relief="flat", padx=12, pady=7, font=("Segoe UI", 10))

    def animate(self):
        canvas = self.canvas
        canvas.delete("all")
        elapsed = time.monotonic() - self.animation_start
        width = canvas.winfo_width()
        if width < 100:
            self.window.after(32, self.animate)
            return
        cx, cy = width / 2, 186
        self.voice_level *= 0.93
        self.core_flash *= 0.955
        active = self.awake_until and datetime.datetime.now() < self.awake_until
        accent = "#ffb973" if active else BLUE
        energy = max(self.voice_level, self.core_flash)

        def point(radius, angle):
            return cx + radius * math.cos(angle), cy - radius * math.sin(angle)

        def ring(radius):
            return cx-radius, cy-radius, cx+radius, cy+radius

        for x in range(10, width, 23):
            canvas.create_line(x, 30, x, 344, fill="#0d1c2d")
        for y in range(30, 345, 23):
            canvas.create_line(10, y, width-10, y, fill="#0d1c2d")
        canvas.create_line(14, cy, width-14, cy, fill="#1b4560", dash=(3, 6))
        canvas.create_line(cx, 15, cx, 355, fill="#1b4560", dash=(3, 6))

        for radius in (71, 89, 108, 130, 151):
            canvas.create_oval(*ring(radius), outline="#1c4057")
        for index in range(72):
            angle = math.tau * index / 72
            major = index % 6 == 0
            canvas.create_line(*point(145 if major else 149, angle), *point(157, angle),
                               fill="#4297b4" if major else "#214961", width=2 if major else 1)
        for radius, speed, count, span in ((140, 18, 3, 65), (119, -31, 4, 47),
                                            (99, 52, 2, 102), (79, -70, 3, 46)):
            for index in range(count):
                angle = (elapsed * speed + 360 * index / count) % 360
                canvas.create_arc(*ring(radius), start=angle, extent=span, style="arc",
                                  outline="#173c56", width=7)
                canvas.create_arc(*ring(radius), start=angle, extent=span, style="arc",
                                  outline=accent, width=2)

        scan = elapsed * 1.45
        for index in range(28):
            angle = scan - index * 0.018
            shade = int(102 - index * 2.8)
            canvas.create_line(*point(86, angle), *point(144, angle),
                               fill=f"#{shade//4:02x}{shade:02x}{shade+23:02x}")
        canvas.create_line(*point(86, scan), *point(146, scan), fill=accent, width=2)

        pulse = 4 * math.sin(elapsed * 3) + 16 * energy
        for radius, color in ((62, "#0a2337"), (55, "#0b2d44"), (47, "#113e55")):
            canvas.create_oval(*ring(radius + pulse / 2), fill=color, outline="#245975")
        for offset in (0, math.pi / 6):
            vertices = [c for i in range(6) for c in point(38 + pulse, i * math.pi / 3 + offset)]
            canvas.create_polygon(*vertices, fill="", outline=accent)
        canvas.create_text(cx, cy-9, text="JARVIS", fill="#e4faff",
                           font=("Segoe UI", 14, "bold"))
        canvas.create_text(cx, cy+13, text="ASCULT" if active else "STANDBY",
                           fill=accent, font=("Consolas", 8))

        for x, label, direction in ((28, "SEMNAL VOCAL", 1),
                                    (width-28, "CONEXIUNE", -1)):
            anchor = "w" if direction == 1 else "e"
            canvas.create_text(x, 99, text=label, fill="#9cd7ef", anchor=anchor,
                               font=("Consolas", 10, "bold"))
            canvas.create_line(x, 113, x+direction*122, 113, fill="#275675")
            if direction == 1:
                for index in range(17):
                    amplitude = 3 + 26 * self.voice_level * abs(math.sin(index * 0.8 - elapsed * 12))
                    bar = x + index * 7
                    canvas.create_line(bar, 139-amplitude, bar, 139+amplitude, fill=accent, width=2)
            else:
                canvas.create_text(x, 139, text="ONLINE" if self.online else "OFFLINE",
                                   fill=accent if self.online else "#8ba5b6", anchor=anchor,
                                   font=("Consolas", 12))
        canvas.create_text(cx, 367, text=datetime.datetime.now().strftime("%H:%M:%S"),
                           fill="#9cd7ef", font=("Consolas", 12))
        self.window.after(32, self.animate)

    def check_network(self):
        def check():
            self.events.put(("network", internet_available()))
        threading.Thread(target=check, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "network":
                    self.online = value
                    self.connection.config(text="● ONLINE" if value else "○ OFFLINE")
                    if value and not self.web.winfo_manager():
                        self.web.pack(pady=12)
                    elif not value:
                        self.web.pack_forget()
                    self.window.after(5000, self.check_network)
                elif kind == "level":
                    self.voice_level = max(self.voice_level, value)
                elif kind == "voice":
                    self.handle_voice(value)
                else:
                    self.status.config(text=value)
        except queue.Empty:
            pass
        self.window.after(80, self.poll)

    def reply(self, message, spoken=None):
        self.status.config(text=message)
        try:
            self.replies.put_nowait(spoken or message)
        except queue.Full:
            pass

    def play_replies(self):
        while True:
            message = self.replies.get()
            self.speaking.set()
            try:
                try:
                    play_neural_reply(message)
                    continue
                except Exception:
                    pass
                result = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", TTS_SCRIPT],
                    input=message.encode("utf-8"), capture_output=True, timeout=25,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if result.returncode == 3:
                    self.events.put(("status", "Vocea Emil necesită internet și instalarea componentelor audio. Vocea masculină română locală nu este disponibilă."))
                elif result.returncode:
                    self.events.put(("status", "Redarea a eșuat. Verifică ieșirea audio implicită și vocile Windows."))
            except (OSError, subprocess.TimeoutExpired):
                self.events.put(("status", "Redarea audio nu a pornit. Verifică setările de sunet Windows."))
            finally:
                time.sleep(0.25)
                self.speaking.clear()

    def start_voice(self):
        if self.voice_thread and self.voice_thread.is_alive():
            return
        self.voice_thread = threading.Thread(target=self.listen, daemon=True)
        self.voice_thread.start()

    def listen(self):
        try:
            import numpy as np
            import sounddevice
            from faster_whisper import WhisperModel

            self.events.put(("status", "Încarc modelul vocal. Prima pornire necesită internet."))
            model = WhisperModel("base", device="cpu", compute_type="int8")
            audio = queue.Queue(maxsize=150)

            last_meter = 0.0

            def capture(data, frames, timing, status):
                nonlocal last_meter
                if self.speaking.is_set():
                    return
                now = time.monotonic()
                if now - last_meter > 0.09:
                    sample = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768
                    rms = float(np.sqrt(np.mean(sample ** 2)))
                    self.events.put(("level", min(1.0, rms * 9)))
                    last_meter = now
                try:
                    audio.put_nowait(bytes(data))
                except queue.Full:
                    pass

            with sounddevice.RawInputStream(samplerate=16000, blocksize=480,
                                            channels=1, dtype="int16", callback=capture):
                self.events.put(("status", "Microfon activ · Spune „Jarvis, trezește-te”"))
                chunks = []
                quiet = 0
                while True:
                    samples = np.frombuffer(audio.get(), dtype=np.int16).astype(np.float32) / 32768
                    speaking = np.sqrt(np.mean(samples ** 2)) > 0.008
                    if not chunks and not speaking:
                        continue
                    chunks.append(samples)
                    quiet = 0 if speaking else quiet + 1
                    if quiet < 20 and len(chunks) < 250:
                        continue
                    segments, _ = model.transcribe(
                        np.concatenate(chunks), language="ro", beam_size=1,
                        condition_on_previous_text=False, vad_filter=True,
                    )
                    phrase = " ".join(segment.text for segment in segments).strip()
                    chunks.clear()
                    quiet = 0
                    if phrase:
                        self.events.put(("voice", phrase))
        except ImportError:
            self.events.put(("status", "Pentru microfon, instalează componentele din Citește-mă."))
        except Exception as error:
            self.events.put(("status", f"Microfon indisponibil: {error}"))

    def activate(self, greeting=False):
        self.core_flash = 1.0
        self.awake_until = datetime.datetime.now() + datetime.timedelta(seconds=15)
        self.window.deiconify()
        self.window.lift()
        if greeting:
            self.reply(random.choice(GREETINGS))
        else:
            self.reply("Te ascult. Spune o comandă.", "Te ascult, șefule.")

    def handle_voice(self, text):
        phrase = normalize(re.sub(r"[,!.?]", " ", text))
        wake = WAKE.match(phrase)
        if wake:
            self.activate(greeting=phrase.startswith("salut jarvis"))
            if phrase[wake.end():]:
                self.awake_until = None
                self.execute(phrase[wake.end():])
        elif self.awake_until and datetime.datetime.now() < self.awake_until:
            self.awake_until = None
            self.execute(text.strip().rstrip(".!?"))

    def submit(self, event=None):
        text = self.entry.get().strip()
        self.entry.delete(0, "end")
        if text:
            self.execute(text)

    def open_web(self, url):
        if not self.online:
            self.reply("Acțiunea necesită internet.")
            return
        opened = webbrowser.open(url)
        self.reply("Am trimis pagina către browser." if opened else "Browserul nu a putut fi deschis.")

    def execute(self, text):
        self.core_flash = 0.65
        text = text.strip()
        phrase = normalize(text.replace(",", " ").rstrip(".!?"))
        if WAKE.fullmatch(phrase):
            self.activate(greeting=phrase.startswith("salut jarvis"))
        elif phrase == "ajutor":
            self.status.config(text=f"{len(self.commands)} lansări configurate · Exemple: deschide calculatorul, spune ora, caută pe internet vremea, deschide site-ul wikipedia.org.")
        elif phrase in {"ora", "spune ora", "cat e ceasul"}:
            self.reply(datetime.datetime.now().strftime("Ora este %H:%M."))
        elif phrase in {"deschide youtube", "deschide google"}:
            site = "youtube" if phrase.endswith("youtube") else "google"
            self.open_web(f"https://www.{site}.com")
        elif (query := re.fullmatch(r"caut[ăa](?:\s+pe\s+internet)?\s+(.+)", text, re.IGNORECASE)):
            self.open_web("https://www.google.com/search?" + urllib.parse.urlencode({"q": query[1]}))
        elif (site := re.fullmatch(r"deschide\s+site-ul\s+(.+)", text, re.IGNORECASE)):
            address = site[1].strip()
            try:
                url = address if "://" in address else "https://" + address
                parsed = urllib.parse.urlsplit(url)
                if parsed.scheme not in {"https", "http"} or not parsed.hostname or not re.fullmatch(r"[^\s]+", url):
                    raise ValueError
                self.open_web(url)
            except ValueError:
                self.status.config(text="Adresa site-ului este invalidă.")
        elif phrase in self.commands:
            try:
                args = [os.path.expandvars(arg) for arg in self.commands[phrase]]
                subprocess.Popen(args, shell=False, cwd=ROOT)
                self.reply(f"Comandă lansată · {text}", "Am lansat comanda.")
            except (OSError, ValueError) as error:
                self.status.config(text=f"Nu am putut lansa aplicația: {error}")
        else:
            self.reply("Comandă necunoscută. Apasă «Comenzi» pentru exemple.", "Nu am recunoscut comanda.")


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Jarvis RO este pregătit pentru Windows.")
    window = tk.Tk()
    Jarvis(window)
    window.mainloop()
