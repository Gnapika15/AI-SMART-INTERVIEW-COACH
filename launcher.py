import os
import subprocess
import threading
import time
import webbrowser

from app import app


HOST = "127.0.0.1"
PORT = 5000
URL = f"http://{HOST}:{PORT}"


def find_chrome_path():
	candidates = [
		os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
		os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
		os.path.join(os.environ.get("LocalAppData", ""), "Google", "Chrome", "Application", "chrome.exe"),
	]

	for candidate in candidates:
		if candidate and os.path.exists(candidate):
			return candidate

	return None


def open_in_chrome(url):
	chrome_path = find_chrome_path()
	if chrome_path:
		subprocess.Popen([chrome_path, url])
		return

	webbrowser.open_new(url)


def run_server():
	app.run(host=HOST, port=PORT, debug=True, use_reloader=False)


if __name__ == "__main__":
	threading.Thread(target=run_server, daemon=True).start()
	time.sleep(1.5)
	open_in_chrome(URL)
