import os
import re
import sys
import json
import time
import queue
import threading
from urllib.parse import urlparse
from getpass import getpass
from collections import OrderedDict

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ======================== CONFIG ========================

LOGIN_URL = (
    "https://account.inkr.com/login?"
    "redirect=https%3A%2F%2Faccount.inkr.com%2Fcallback%2Fmy-lists%3FshouldShowWelcomeGift%3Dtrue"
)
HOMEPAGE = "https://comics.inkr.com/"

PRE_SCROLL_STEPS = 6
READER_SCROLL_STEP_PX = 1600
SCROLL_PAUSE_MS = 350
STAGNATION_WINDOW_SEC = 5.0
MAX_SCROLL_SECONDS = 180
EXTRA_LISTEN_MS = 2500
SLEEP_BETWEEN_DL = 0.2

IMG_RE = re.compile(
    r"/(?:(?:p|img)\.(?:jpg|jpeg|png|webp)|\d+\.(?:jpg|jpeg|png))(?:\?|$)",
    re.IGNORECASE,
)

BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

APP_NAME = "INKRipper"
CONFIG_FILENAME = "inkripper_config.json"


# ======================== UTILIDADES ========================

def app_base_dir():
    # Carpeta del ejecutable compilado o del script .py
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def config_path():
    return os.path.join(app_base_dir(), CONFIG_FILENAME)

def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_config(cfg: dict):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def detect_browsers():
    found = []
    for path in BROWSER_CANDIDATES:
        if os.path.exists(path):
            name = "Google Chrome" if path.lower().endswith("chrome.exe") else "Microsoft Edge"
            found.append((name, path))
    return found

def folder_from_chapter_url(url: str) -> str:
    path = urlparse(url).path
    m = re.search(r"/chapter/\d+-chapter-(\d+)", path, re.IGNORECASE)
    if m:
        return f"Capitulo {m.group(1)}"
    tail = os.path.basename(path.rstrip("/")) or "capitulo"
    safe = re.sub(r"[\\/:*?\"<>|]+", "_", tail)
    return safe[:150] or "capitulo"

def find_continue_btn(page):
    try:
        btn = page.get_by_role("button", name="Continue", exact=True)
        if btn.count():
            return btn.first
    except Exception:
        pass
    try:
        btn = page.locator("//button[normalize-space()='Continue']")
        if btn.count():
            return btn.first
    except Exception:
        pass
    return page.locator("//button[contains(., 'Continue') and not(contains(., 'With'))]").first

def wait_until_logged_redirect(page, timeout_ms=90_000):
    deadline = time.time() + (timeout_ms / 1000.0)
    while time.time() < deadline:
        url = page.url or ""
        if "account.inkr.com/login" not in url:
            return True
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except PWTimeout:
            pass
        page.wait_for_timeout(800)
    return False

def do_login_on_page(page, email, password, log_fn):
    log_fn("Abriendo página de login…")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

    if "account.inkr.com/login" not in page.url:
        log_fn("Sesión existente detectada.")
        page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=60_000)
        return True

    # Paso 1: email
    try:
        email_input = (
            page.locator("input[type='email']").first
            if page.locator("input[type='email']").count()
            else page.locator("input[placeholder*='email' i]").first
        )
        email_input.wait_for(timeout=15_000)
        email_input.fill(email)
        find_continue_btn(page).click(timeout=15_000)
    except PWTimeout:
        log_fn("No se encontró el campo de email o el botón 'Continue'.")
        return False

    # Paso 2: password
    try:
        pwd_input = (
            page.locator("input[type='password']").first
            if page.locator("input[type='password']").count()
            else page.locator("input[placeholder*='password' i]").first
        )
        pwd_input.wait_for(timeout=20_000)
        pwd_input.fill(password)
        find_continue_btn(page).click(timeout=15_000)
    except PWTimeout:
        log_fn("No se encontró el campo de contraseña o el botón 'Continue'.")
        return False

    # Esperar redirección (salir de /login)
    log_fn("Esperando redirección de login…")
    ok = wait_until_logged_redirect(page, timeout_ms=90_000)
    if not ok:
        try:
            err = page.locator("text=/incorrect|invalid|try again|wrong/i").first
            if err.count():
                log_fn("El sitio reportó un error de credenciales.")
        except Exception:
            pass
        log_fn("No cambió la URL tras enviar la contraseña.")
        return False

    page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=60_000)
    log_fn("Login completado; sesión aplicada en comics.inkr.com.")
    return True

def click_into_reader_if_needed(page, log_fn):
    page.wait_for_timeout(1200)
    texts = [
        "Read", "Read Now", "Read Chapter", "Start Reading",
        "Continue", "Continue Reading", "Read for Free", "Resume",
        "Read Chapter 1", "Read Chapter"
    ]

    for t in texts:
        try:
            btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(t)}$", re.IGNORECASE), exact=True)
            if btn.count():
                btn.first.click(timeout=3000)
                page.wait_for_load_state("networkidle", timeout=8000)
                log_fn(f"Botón detectado: {t}")
                return True
        except Exception:
            pass

    try:
        btn2 = page.locator("//button[contains(., 'Read') or contains(., 'Continue')]").first
        if btn2.count():
            btn2.click(timeout=3000)
            page.wait_for_load_state("networkidle", timeout=8000)
            log_fn("Botón de lectura detectado (aprox).")
            return True
    except Exception:
        pass
    return False

def collect_all_image_urls(page, chapter_url: str, log_fn):
    seen = OrderedDict()

    def add_url(url: str):
        if not url:
            return
        if IMG_RE.search(url):
            url = url.split("#")[0]
            if url not in seen:
                seen[url] = None
                # log_fn(f"Detectado: {url}")

    def on_request(req):
        add_url(req.url)

    def on_response(resp):
        try:
            add_url(resp.url)
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)

    page.goto(chapter_url, wait_until="domcontentloaded", timeout=60_000)

    for _ in range(PRE_SCROLL_STEPS):
        page.mouse.wheel(0, READER_SCROLL_STEP_PX)
        page.wait_for_timeout(SCROLL_PAUSE_MS)

    click_into_reader_if_needed(page, log_fn)

    start_time = time.time()
    last_new_time = time.time()
    last_count = 0

    while True:
        page.mouse.wheel(0, READER_SCROLL_STEP_PX)
        page.wait_for_timeout(SCROLL_PAUSE_MS)

        cur_count = len(seen)
        if cur_count > last_count:
            last_count = cur_count
            last_new_time = time.time()

        if time.time() - last_new_time > STAGNATION_WINDOW_SEC:
            break
        if time.time() - start_time > MAX_SCROLL_SECONDS:
            log_fn("Tiempo máximo de scroll alcanzado; se detiene la captura.")
            break

    page.wait_for_timeout(EXTRA_LISTEN_MS)

    # DOM final
    try:
        img_srcs = page.eval_on_selector_all(
            "img",
            "els => els.map(e => e.currentSrc || e.src).filter(Boolean)"
        )
        for u in img_srcs:
            add_url(u)
    except Exception:
        pass

    try:
        bg_urls = page.eval_on_selector_all(
            "*",
            """els => els
                .map(e => getComputedStyle(e).backgroundImage)
                .filter(v => v && v.startsWith('url('))
                .map(v => v.slice(4, -1).replace(/^\"|\"$/g, ''))"""
        )
        for u in bg_urls:
            add_url(u)
    except Exception:
        pass

    return list(seen.keys())

def download_images(page, urls, out_dir: str, referer: str, log_fn):
    os.makedirs(out_dir, exist_ok=True)
    total = len(urls)
    if total == 0:
        log_fn("No se capturaron imágenes del lector.")
        return

    pad = max(2, len(str(total)))
    for idx, img_url in enumerate(urls, start=1):
        try:
            resp = page.request.get(img_url, headers={"Referer": referer})
            if resp.status != 200 or "image" not in (resp.headers.get("content-type", "").lower()):
                log_fn(f"Saltando (no imagen / status {resp.status}): {img_url}")
                continue

            ext = "jpg"
            ctype = (resp.headers.get("content-type", "") or "").lower()
            if "webp" in ctype:
                ext = "webp"
            elif "png" in ctype:
                ext = "png"
            elif "jpeg" in ctype or "jpg" in ctype:
                ext = "jpg"

            fname = os.path.join(out_dir, f"{idx:0{pad}d}.{ext}")
            with open(fname, "wb") as f:
                f.write(resp.body())
            log_fn(f"Descargada: {fname}")
            time.sleep(SLEEP_BETWEEN_DL)
        except Exception as e:
            log_fn(f"Error con {img_url}: {e}")

    log_fn("Descarga completada.")


# ======================== GUI (Tkinter) ========================

class INKRipperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("720x520")
        self.resizable(False, False)

        self.queue = queue.Queue()
        self.running = False

        cfg = load_config()
        self.browser_path = tk.StringVar(value=cfg.get("browser_path", ""))

        self.email = tk.StringVar()
        self.password = tk.StringVar()
        self.show_pass = tk.BooleanVar(value=False)
        self.chapter_url = tk.StringVar()
        self.visible = tk.BooleanVar(value=False)

        self._build_ui()
        self.after(100, self._process_log_queue)

    def _build_ui(self):
        pad = {'padx': 10, 'pady': 5}

        # Navegador
        frm_nav = ttk.LabelFrame(self, text="Navegador")
        frm_nav.pack(fill="x", **pad)

        ttk.Label(frm_nav, text="Ruta:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.ent_browser = ttk.Entry(frm_nav, textvariable=self.browser_path, width=80)
        self.ent_browser.grid(row=0, column=1, sticky="we", padx=8, pady=6, columnspan=3)

        ttk.Button(frm_nav, text="Detectar Chrome/Edge", command=self.on_detect).grid(row=1, column=1, sticky="w", padx=8, pady=4)
        ttk.Button(frm_nav, text="Elegir ruta…", command=self.on_browse).grid(row=1, column=2, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(frm_nav, text="Mostrar navegador (debug)", variable=self.visible).grid(row=1, column=3, sticky="e", padx=8, pady=4)

        for i in range(4):
            frm_nav.columnconfigure(i, weight=1)

        # Credenciales
        frm_cred = ttk.LabelFrame(self, text="Credenciales INKR")
        frm_cred.pack(fill="x", **pad)

        ttk.Label(frm_cred, text="Correo:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(frm_cred, textvariable=self.email, width=40).grid(row=0, column=1, sticky="w", padx=8, pady=6)

        ttk.Label(frm_cred, text="Contraseña:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.ent_password = ttk.Entry(frm_cred, textvariable=self.password, width=40, show="*")
        self.ent_password.grid(row=1, column=1, sticky="w", padx=8, pady=6)
        ttk.Checkbutton(frm_cred, text="Mostrar", variable=self.show_pass, command=self._toggle_password).grid(row=1, column=2, sticky="w", padx=8, pady=6)

        # Capítulo
        frm_ch = ttk.LabelFrame(self, text="Capítulo")
        frm_ch.pack(fill="x", **pad)

        ttk.Label(frm_ch, text="URL del capítulo:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        ttk.Entry(frm_ch, textvariable=self.chapter_url, width=80).grid(row=0, column=1, sticky="we", padx=8, pady=6)
        ttk.Button(frm_ch, text="Iniciar", command=self.on_start).grid(row=0, column=2, sticky="e", padx=8, pady=6)

        frm_ch.columnconfigure(1, weight=1)

        # Log
        frm_log = ttk.LabelFrame(self, text="Registro")
        frm_log.pack(fill="both", expand=True, **pad)

        self.txt_log = tk.Text(frm_log, wrap="word", height=16)
        self.txt_log.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        scroll = ttk.Scrollbar(frm_log, command=self.txt_log.yview)
        scroll.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=scroll.set)

    def _toggle_password(self):
        self.ent_password.configure(show="" if self.show_pass.get() else "*")

    def on_detect(self):
        found = detect_browsers()
        if found:
            # Priorizar Chrome si existe
            chrome = [p for (n, p) in found if p.lower().endswith("chrome.exe")]
            if chrome:
                self.browser_path.set(chrome[0])
            else:
                self.browser_path.set(found[0][1])
            self._log(f"Navegador detectado: {self.browser_path.get()}")
        else:
            messagebox.showwarning(APP_NAME, "No se detectó Chrome/Edge. Selecciona la ruta manualmente.")

    def on_browse(self):
        path = filedialog.askopenfilename(
            title="Selecciona chrome.exe o msedge.exe",
            filetypes=[("Ejecutables", "*.exe"), ("Todos", "*.*")]
        )
        if path:
            self.browser_path.set(path)
            self._log(f"Navegador seleccionado: {path}")

    def on_start(self):
        if self.running:
            messagebox.showinfo(APP_NAME, "Ya hay una tarea en ejecución.")
            return

        browser_path = self.browser_path.get().strip()
        email = self.email.get().strip()
        password = self.password.get()  # puede estar vacío si ya hay sesión previa server-side
        chapter_url = self.chapter_url.get().strip()
        visible = self.visible.get()

        if not browser_path or not os.path.exists(browser_path):
            messagebox.showerror(APP_NAME, "Ruta de navegador inválida. Detecta o selecciona una ruta válida.")
            return
        if not email:
            messagebox.showerror(APP_NAME, "Ingresa tu correo de INKR.")
            return
        if not password:
            # Podría existir sesión previa, pero pedimos por si acaso
            if not messagebox.askyesno(APP_NAME, "No ingresaste contraseña. ¿Intentar de todos modos?"):
                return
        if not chapter_url:
            messagebox.showerror(APP_NAME, "Ingresa la URL del capítulo.")
            return

        # Guardar preferencia de navegador
        cfg = load_config()
        cfg["browser_path"] = browser_path
        save_config(cfg)

        # Lanzar en hilo para no congelar la UI
        self.running = True
        t = threading.Thread(target=self._run_task, args=(browser_path, email, password, chapter_url, visible), daemon=True)
        t.start()

    def _run_task(self, browser_path, email, password, chapter_url, visible):
        def log_fn(msg):
            self.queue.put(str(msg))

        out_dir = folder_from_chapter_url(chapter_url)
        log_fn(f"Carpeta destino: {out_dir}")

        try:
            with sync_playwright() as p:
                launch_args = ["--start-minimized"] if visible else []
                try:
                    browser = p.chromium.launch(
                        headless=not visible,
                        executable_path=browser_path,
                        args=launch_args
                    )
                except Exception as e:
                    log_fn(f"No se pudo lanzar el navegador seleccionado.\n{e}")
                    self.running = False
                    return

                context = browser.new_context(
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/129.0.0.0 Safari/537.36")
                )
                page = context.new_page()

                # Login
                ok = do_login_on_page(page, email, password, log_fn)
                if not ok:
                    log_fn("Login fallido o cancelado.")
                    context.close(); browser.close()
                    self.running = False
                    return

                # Capturar
                urls = collect_all_image_urls(page, chapter_url, log_fn)
                log_fn(f"Imágenes detectadas: {len(urls)}")

                # Descargar
                download_images(page, urls, out_dir, chapter_url, log_fn)

                context.close()
                browser.close()
        except Exception as e:
            log_fn(f"Error general: {e}")

        self.running = False
        log_fn("Listo.")

    def _process_log_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                self.txt_log.insert("end", msg + "\n")
                self.txt_log.see("end")
        except queue.Empty:
            pass
        self.after(100, self._process_log_queue)

    def _log(self, msg):
        self.queue.put(str(msg))


if __name__ == "__main__":
    app = INKRipperApp()
    app.mainloop()
